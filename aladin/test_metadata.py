"""产物元数据剥离：无损、只去生成信息、解析失败时原样返回。"""
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix='aladin-meta-')
os.environ['ALADIN_DATA'] = _TMP
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aladin.testing import configure, reset  # noqa: E402

configure()

from PIL import Image, PngImagePlugin  # noqa: E402

from aladin import cleanup, db, metadata, settings  # noqa: E402


def png_with_prompt(text='{"prompt": "secret words"}', color='red') -> bytes:
    info = PngImagePlugin.PngInfo()
    info.add_text('prompt', text)
    info.add_itxt('workflow', text)
    buffer = io.BytesIO()
    Image.new('RGB', (8, 8), color).save(buffer, 'PNG', pnginfo=info)
    return buffer.getvalue()


def ebml(element_id: bytes, payload: bytes) -> bytes:
    return element_id + bytes([0x01]) + len(payload).to_bytes(7, 'big') + payload


def webm_with_tags() -> bytes:
    header = ebml(b'\x1a\x45\xdf\xa3', b'\x42\x82\x84webm')
    info = ebml(b'\x15\x49\xa9\x66', b'\x00' * 4)
    tags = ebml(b'\x12\x54\xc3\x67', b'{"class_type": "SaveVideo", "text": "secret words"}')
    cluster = ebml(b'\x1f\x43\xb6\x75', b'\x11' * 32)
    return header + ebml(b'\x18\x53\x80\x67', info + tags + cluster)


class StripTest(unittest.TestCase):
    def test_png_drops_text_chunks_and_keeps_pixels(self):
        data = png_with_prompt()
        clean = metadata.strip(data, 'image-01.png')
        self.assertNotIn(b'secret words', clean)
        self.assertLess(len(clean), len(data))
        with Image.open(io.BytesIO(clean)) as image, Image.open(io.BytesIO(data)) as original:
            self.assertEqual(image.text, {})
            self.assertEqual(image.tobytes(), original.tobytes())
        self.assertEqual(metadata.strip(clean, 'x.png'), clean, '已经干净的文件不变')

    def test_webm_tags_become_same_length_void(self):
        data = webm_with_tags()
        clean = metadata.strip(data, 'video-01.webm')
        self.assertNotIn(b'secret words', clean)
        self.assertEqual(len(clean), len(data), '等长替换：后面元素的偏移不能变')
        self.assertEqual(clean[-40:], data[-40:], 'Cluster 原样保留')
        self.assertIn(b'\xec\x01', clean)

    def test_unparsable_input_is_returned_unchanged(self):
        for data, name in [(b'not a png', 'a.png'), (b'\x89PNG\r\n\x1a\n\x00\x00\xff\xff', 'b.png'),
                           (b'\x1a\x45\xdf\xa3\xff', 'c.webm'), (b'abc', 'd.jpg')]:
            self.assertEqual(metadata.strip(data, name), data, name)


class StripExistingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.init()
        settings.ensure_dirs()

    def setUp(self):
        reset()

    def test_rewrites_files_and_keeps_ledger_in_sync(self):
        import hashlib
        job_id = db.create_job('p', {'images': 1, 'seed': 1}, {'key': 'k'}, 'a' * 64)
        directory = settings.JOBS_DIR / job_id
        directory.mkdir(parents=True, exist_ok=True)
        data = png_with_prompt()
        (directory / 'image-01.png').write_bytes(data)
        sha = hashlib.sha256(data).hexdigest()
        db.add_artifact(job_id, 'image-01.png', f'jobs/{job_id}/image-01.png', sha, len(data), 1, 'p')
        from aladin import gallery
        gallery.promote(job_id, 'image-01.png')

        self.assertEqual(cleanup.strip_existing_metadata(dry_run=True), 2)
        self.assertEqual((directory / 'image-01.png').read_bytes(), data, 'dry-run 不改文件')
        self.assertEqual(cleanup.strip_existing_metadata(), 2)
        clean = (directory / 'image-01.png').read_bytes()
        self.assertNotIn(b'secret words', clean)
        artifact = db.artifact(job_id, 'image-01.png')
        self.assertEqual(artifact['sha256'], hashlib.sha256(clean).hexdigest())
        self.assertTrue(db.in_gallery(artifact['sha256']), '产物与收藏同步更新，仍能对上')
        self.assertEqual(cleanup.strip_existing_metadata(), 0, '重复执行无事可做')


if __name__ == '__main__':
    unittest.main()
