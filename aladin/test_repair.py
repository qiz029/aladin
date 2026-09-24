import io
import unittest
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch
from PIL import Image
import request
from aladin import worker, params


def png(image):
    buffer = io.BytesIO()
    image.save(buffer, 'PNG')
    return buffer.getvalue()


class RepairTest(unittest.TestCase):
    def test_execute_saves_composited_receipt_and_reuses_it(self):
        source = png(Image.new('RGB', (128,128), 'blue'))
        args = dict(prompt='repair wrist', images=1, seed=2, width=None, height=None,
                    steps=20, cfg=1, sampler='euler', scheduler='simple', mode='edit',
                    input_sha256=worker.digest(source), region=[.25,.25,.75,.75])
        payload = request.build(key=request.storage_key(**args), **args)
        history = {'test-prompt': {'outputs': {'save0': {'images': [{}]}}}}
        generated = png(Image.new('RGB',(512,512),'red'))
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(worker, 'ensure_weights'), \
             patch.object(worker, 'write_input', side_effect=lambda data, sha: sha+'.png'), \
             patch.object(worker, 'start_server', return_value=(MagicMock(), None)) as server, \
             patch.object(worker, 'watch_async', return_value=MagicMock()), \
             patch.object(worker, 'submit'), \
             patch.object(worker.uuid, 'uuid4', return_value='test-prompt'), \
             patch.object(worker, 'wait_for_history', return_value=history), \
             patch.object(worker, 'fetch_image', return_value=generated):
            record = worker.execute(payload, directory, source)
            data = (Path(directory)/payload['key']/'image-01.png').read_bytes()
            image = Image.open(io.BytesIO(data))
            self.assertEqual(image.size,(128,128))
            self.assertEqual(image.getpixel((0,0)),(0,0,255))
            self.assertEqual(image.getpixel((64,64)),(255,0,0))
            self.assertEqual(record['images'][0]['sha256'],worker.digest(data))
            self.assertEqual(record['images'][0]['params']['region'],args['region'])
            self.assertEqual(worker.execute(payload,directory,source),record)
            server.assert_called_once()

    def test_region_contract_and_idempotency(self):
        args = dict(prompt='repair wrist', images=1, seed=2, width=None, height=None,
                    steps=20, cfg=1, sampler='euler', scheduler='simple', mode='edit', input_sha256='a'*64)
        region = [.2, .3, .6, .7]
        key = request.storage_key(**args, region=region)
        self.assertNotEqual(key, request.storage_key(**args))
        self.assertNotEqual(key, request.storage_key(**args, region=[.3,.3,.6,.7]))
        built = request.build(key=key, **args, region=region)
        self.assertEqual(worker.validate(built)['region'], region)
        for value in ([0,0,0,1], [0,0,2,1], [False,0,1,1], [0,0,float('nan'),1], 'bad', [0,1]):
            with self.subTest(value=value), self.assertRaises(ValueError):
                worker.validate(dict(built, region=value))
        with self.assertRaises(ValueError):
            request.repair_region(region, 'img2img')

    def test_composite_preserves_all_pixels_outside_selection(self):
        for mode in ('RGB', 'RGBA'):
            original = Image.new(mode, (137,193), (10,80,130,90) if mode == 'RGBA' else (10,80,130))
            crop, state = worker.repair_source(png(original), [.2,.3,.7,.8])
            generated = png(Image.new('RGB', (512,512), 'red'))
            result = Image.open(io.BytesIO(worker.repair_composite(generated, state)))
            self.assertEqual(result.size, original.size)
            box = state[1]
            for y in range(original.height):
                for x in range(original.width):
                    if not (box[0] <= x < box[2] and box[1] <= y < box[3]):
                        self.assertEqual(result.getpixel((x,y)), original.getpixel((x,y)))
            self.assertNotEqual(result.getpixel((50,100)), original.getpixel((50,100)))

    def test_preflight_rejects_bad_or_tiny_input(self):
        region = [0,0,.5,.5]
        self.assertTrue(params.repair_input_error(b'not a PNG', region))
        self.assertTrue(params.repair_input_error(png(Image.new('RGB',(8,8))), region))
        self.assertEqual(params.repair_input_error(png(Image.new('RGB',(64,64))), region), '')

    def test_exif_orientation_and_edge_region(self):
        image = Image.new('RGB',(90,60),'blue')
        exif = image.getexif(); exif[274] = 6
        stream = io.BytesIO(); image.save(stream,'JPEG',exif=exif)
        crop, state = worker.repair_source(stream.getvalue(), [0,0,1,1])
        self.assertEqual(state[0].size, (60,90))
        result = Image.open(io.BytesIO(worker.repair_composite(crop, state)))
        self.assertEqual(result.size, (60,90))


if __name__ == '__main__':
    unittest.main()
