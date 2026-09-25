"""web 层验证：不碰 Modal，用 TestClient 走真实路由。

覆盖：应用入口、参数校验、任务页、历史、图库增删、SSE 收尾。
"""
from __future__ import annotations

import base64
import json
import os
import sys
import tempfile
import types
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from unittest import mock
from pathlib import Path

# 必须早于 aladin 的导入，settings 在导入期读取该环境变量
_TMP = tempfile.mkdtemp(prefix='aladin-test-')
os.environ['ALADIN_DATA'] = _TMP
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 必须在 import aladin 业务模块之前：settings 在导入期读 DATABASE_URL
from aladin.testing import configure, reset  # noqa: E402

configure()

from fastapi.testclient import TestClient  # noqa: E402

from aladin import billing, db, settings, web  # noqa: E402


class WebTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.init()
        settings.ensure_dirs()
        cls.client = TestClient(web.app)

    def setUp(self):
        reset()   # 每个用例从空表开始

    def test_region_repair_form_api_parity_and_invalid_input(self):
        import io
        import json
        from PIL import Image
        buffer = io.BytesIO()
        Image.new('RGB', (128,128), 'blue').save(buffer, 'PNG')
        data = buffer.getvalue()
        region = [.2,.2,.8,.8]
        fields = dict(mode='edit', prompt='repair hand', region=json.dumps(region), seed='7')
        response = self.client.post('/apps/edit/jobs', data=fields,
                                    files={'file':('source.png',data,'image/png')}, follow_redirects=False)
        self.assertEqual(response.status_code, 303, response.text)
        job_id = response.headers['location'].split('/')[-1]
        response = self.client.post('/api/v1/edits/base64', json=dict(mode='edit', prompt='repair hand',
            region=region, seed=7, image_base64=base64.b64encode(data).decode()))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['id'], job_id)
        self.assertEqual(response.json()['generation_request']['region'], region)
        self.assertFalse(response.json()['created'])
        response = self.client.post('/api/v1/edits', data=dict(fields, region='[0,0,.5,.5]'),
                                    files={'file':('source.png',data,'image/png')})
        self.assertEqual(response.status_code, 422)  # strict JSON, no .5 shorthand
        response = self.client.post('/api/v1/edits', data=dict(fields, region='[0,0,0.5,0.5]'),
                                    files={'file':('source.png',data,'image/png')})
        self.assertEqual(response.status_code, 202, response.text)
        self.assertNotEqual(response.json()['id'], job_id)
        response = self.client.post('/api/v1/edits', data=fields,
                                    files={'file':('bad.png',b'not an image','image/png')})
        self.assertEqual(response.status_code, 422)
        response = self.client.post('/api/v1/edits/base64', json=dict(mode='edit', prompt='repair hand',
            region=[False,0,1,1], image_base64=base64.b64encode(data).decode()))
        self.assertEqual(response.status_code, 422)

    def test_review_persists_and_exports_with_comparison(self):
        job = self.client.post('/api/v1/images', json=dict(prompt='person', images=2)).json()
        job_id = job['id']
        db.add_artifact(job_id, 'image-01.png', 'unused.png', 'a'*64, 10, 0, 'person', job['params'])
        path = f'/api/v1/jobs/{job_id}/artifacts/image-01.png/review'
        body = dict(anatomy='fail', matches_request='pass', preferred=False, notes='left wrist disconnected')
        response = self.client.put(path, json=body)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['source'], 'human')
        exported = self.client.get(f'/jobs/{job_id}/review.json').json()
        self.assertEqual(exported['artifacts'][0]['review']['notes'], body['notes'])
        self.assertEqual(exported['generation_request'], job['generation_request'])
        page = self.client.get(f'/jobs/{job_id}/compare')
        self.assertEqual(page.status_code, 200)
        self.assertIn('left wrist disconnected', page.text)
        self.assertEqual(self.client.put(path, json=dict(body, anatomy='perfect')).status_code, 422)
        self.assertEqual(self.client.get(f'/jobs/{job_id}/compare?other=missing').status_code, 404)
        suite = self.client.get('/api/v1/benchmarks/people').json()
        self.assertEqual(len(suite['cases']), 8)

    def test_model_defaults_idempotency_and_reuse_over_http(self):
        for model, cfg in [('anima-base-1.0', 4.5), ('pony-realism-2.2', 6.5)]:
            body = dict(prompt='a ceramic teapot', model=model, size='portrait', seed=3)
            response = self.client.post('/api/v1/images', json=body)
            self.assertEqual(response.status_code, 202)
            job = response.json()
            self.assertEqual(job['params']['cfg'], cfg)
            self.assertEqual(job['params']['width'], 832)
            duplicate = self.client.post('/api/v1/images', json=body).json()
            self.assertEqual(duplicate['id'], job['id'])
            self.assertFalse(duplicate['created'])
            db.add_artifact(job['id'], 'image-01.png', 'unused.png', 'a'*64, 10,
                            0, body['prompt'], job['params'])
            page = self.client.get(f"/jobs/{job['id']}/artifacts/image-01.png/reuse")
            self.assertEqual(page.status_code, 200)
            self.assertIn(f'value="{model}" selected', page.text)
            self.assertIn('832×1216', page.text)

    def test_omitted_seed_is_random_and_recorded(self):
        """不填种子 = 随机：同一提示词连点两次得到两个任务，真实种子写进 params。"""
        first = self.client.post('/api/v1/images', json={'prompt': 'random seed'}).json()
        second = self.client.post('/api/v1/images', json={'prompt': 'random seed'}).json()
        self.assertTrue(first['created'] and second['created'])
        self.assertNotEqual(first['params']['seed'], second['params']['seed'])
        self.assertEqual(first['generation_request']['seed'], first['params']['seed'])
        response = self.client.post('/apps/image/jobs', data=dict(prompt='random seed', seed=''),
                                    follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        response = self.client.post('/apps/image/jobs', data=dict(prompt='x', seed='-1'),
                                    follow_redirects=False)
        self.assertEqual(response.status_code, 400)

    def test_model_form_can_clear_default_negative(self):
        response = self.client.post('/apps/image/jobs', data=dict(
            prompt='a blue teapot', model='pony-realism-2.2', negative='',
            negative_provided='true'), follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        job = db.job(response.headers['location'].split('/')[-1])
        self.assertEqual(job['params']['negative'], '')
        self.assertEqual(job['params']['cfg'], 6.5)

    def test_index_lists_image_app(self):
        response = self.client.get('/')
        self.assertEqual(response.status_code, 200)
        self.assertIn('生成图片', response.text)

    def test_image_page_renders_form_and_sections(self):
        response = self.client.get('/apps/image')
        self.assertEqual(response.status_code, 200)
        for marker in ('提示词', '生成几张', '尺寸', '高级参数', '最近生成'):
            self.assertIn(marker, response.text)

    def test_form_rejects_out_of_range_values(self):
        # 步数上限 40、尺寸必须是 8 的倍数——都应被本地拦下，不发往 Modal
        response = self.client.post('/apps/image/jobs', data={
            'prompt': 'x', 'images': 1, 'steps': 99, 'width': 1024, 'height': 1024,
            'cfg': 1.0, 'sampler': 'euler', 'scheduler': 'simple', 'seed': 0,
            'negative': ''}, follow_redirects=False)
        self.assertEqual(response.status_code, 400)
        self.assertIn('步数', response.json()['detail'])

        # 尺寸改成预设后，裸宽高不再接受；未知预设键要被拦下
        response = self.client.post('/apps/image/jobs', data={
            'prompt': 'x', 'images': 1, 'size': 'huge', 'steps': 25,
            'cfg': 1.0, 'sampler': 'euler', 'scheduler': 'simple', 'seed': 0,
            'negative': ''}, follow_redirects=False)
        self.assertEqual(response.status_code, 400)
        self.assertIn('尺寸', response.json()['detail'])

    def test_empty_prompt_rejected(self):
        response = self.client.post('/apps/image/jobs', data={
            'prompt': '   ', 'images': 1, 'steps': 25, 'size': 'square',
            'cfg': 1.0, 'sampler': 'euler', 'scheduler': 'simple', 'seed': 0,
            'negative': ''}, follow_redirects=False)
        self.assertEqual(response.status_code, 400)

    def test_job_page_and_gallery_roundtrip(self):
        # 造一个已完成的任务与产物，避免真的调用 Modal
        params = {'images': 1, 'seed': 3, 'width': 1024, 'height': 1024, 'steps': 25,
                  'cfg': 1.0, 'sampler': 'euler', 'scheduler': 'simple', 'negative': ''}
        job_id = db.create_job('a teapot', params, {'key': 'b' * 64}, 'b' * 64, 'shape-x')
        directory = settings.JOBS_DIR / job_id
        directory.mkdir(parents=True, exist_ok=True)
        (directory / 'image-01.png').write_bytes(b'\x89PNG\r\n\x1a\n' + b'0' * 32)
        db.add_artifact(job_id, 'image-01.png',
                        str((directory / 'image-01.png').relative_to(settings.DATA)),
                        'c' * 64, 40, 3, 'a teapot')
        db.finish_job(job_id, 'succeeded')

        page = self.client.get(f'/jobs/{job_id}')
        self.assertEqual(page.status_code, 200)
        self.assertIn('a teapot', page.text)

        state = self.client.get(f'/jobs/{job_id}/state').json()
        self.assertEqual(state['state'], 'succeeded')
        self.assertEqual(len(state['artifacts']), 1)

        # SSE 对已完成任务应当立刻收尾
        with self.client.stream('GET', f'/jobs/{job_id}/stream') as stream:
            first = next(stream.iter_lines())
            self.assertIn('data:', first)

        self.assertEqual(self.client.get(f'/jobs/{job_id}/artifacts/image-01.png').status_code, 200)

        added = self.client.post('/gallery', data={'job_id': job_id, 'name': 'image-01.png'})
        self.assertEqual(added.status_code, 200)
        self.assertTrue(added.json()['added'])
        # 重复加入应当幂等
        self.assertFalse(self.client.post(
            '/gallery', data={'job_id': job_id, 'name': 'image-01.png'}).json()['added'])

        items = self.client.get('/api/v1/gallery').json()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['prompt'], 'a teapot')
        # 图库必须是独立副本，不能只指向生成历史。
        # 这是库层的不变量，而 API 有意不暴露内部路径，所以从 db 直接查。
        stored = db.gallery_items()[0]
        self.assertTrue(stored['rel_path'].startswith('gallery/'),
                        f"图库条目应指向 gallery/，实际 {stored['rel_path']}")
        self.assertTrue(db.data_path(stored['rel_path']).is_file())
        # API 侧只暴露可访问的 URL
        self.assertEqual(self.client.get(items[0]['url']).status_code, 200)

        item_id = items[0]['id']
        self.assertEqual(self.client.get(f'/gallery/{item_id}/file').status_code, 200)
        self.assertEqual(self.client.delete(f'/gallery/{item_id}').status_code, 200)
        self.assertEqual(self.client.get('/api/v1/gallery').json(), [])

    def test_duplicate_parameters_return_readable_409(self):
        """同参数重复提交要被挡住，且给出可读原因（不是 SQLite 内部错误）。"""
        form = {'prompt': 'duplicate probe', 'images': 1, 'size': 'square', 'steps': 25,
                'cfg': 1.0, 'sampler': 'euler', 'scheduler': 'simple',
                'seed': 4242, 'negative': ''}
        first = self.client.post('/apps/image/jobs', data=form, follow_redirects=False)
        self.assertEqual(first.status_code, 303)

        response = self.client.post('/apps/image/jobs', data=form, follow_redirects=False)
        self.assertEqual(response.status_code, 409)
        self.assertIn('相同参数', response.json()['detail'])

    def test_gallery_survives_history_cleanup(self):
        """图库是独立副本：删掉整个生成历史，图库里的图仍可访问。"""
        import shutil

        params = {'images': 1, 'seed': 77, 'width': 1024, 'height': 1024, 'steps': 25,
                  'cfg': 1.0, 'sampler': 'euler', 'scheduler': 'simple', 'negative': ''}
        job_id = db.create_job('keep me', params, {'key': 'd' * 64}, 'd' * 64, 'shape-keep')
        directory = settings.JOBS_DIR / job_id
        directory.mkdir(parents=True, exist_ok=True)
        (directory / 'image-01.png').write_bytes(b'\x89PNG\r\n\x1a\n' + b'k' * 40)
        db.add_artifact(job_id, 'image-01.png',
                        str((directory / 'image-01.png').relative_to(settings.DATA)),
                        'e' * 64, 48, 77, 'keep me')

        added = self.client.post('/gallery', data={'job_id': job_id, 'name': 'image-01.png'})
        self.assertTrue(added.json()['added'])
        item_id = [i for i in self.client.get('/api/v1/gallery').json() if i['prompt'] == 'keep me'][0]['id']

        # 模拟"清理生成历史"
        shutil.rmtree(directory)
        self.assertFalse(directory.exists())

        self.assertEqual(self.client.get(f'/gallery/{item_id}/file').status_code, 200,
                         '清理历史后图库仍应可访问')
        self.assertEqual(self.client.get(f'/gallery/{item_id}/download').status_code, 200)

        # 移除图库条目应删掉副本，但生成历史的记录不受影响
        self.assertEqual(self.client.delete(f'/gallery/{item_id}').status_code, 200)
        self.assertFalse(db.gallery_item(item_id) is not None)

    def test_download_of_missing_file_is_404_not_500(self):
        """账本有记录但文件被清掉时应给 404，不是 500。"""
        params = {'images': 1, 'seed': 5, 'width': 1024, 'height': 1024, 'steps': 25,
                  'cfg': 1.0, 'sampler': 'euler', 'scheduler': 'simple', 'negative': ''}
        job_id = db.create_job('gone', params, {'key': '1' * 64}, '1' * 64, 'shape-gone')
        db.add_artifact(job_id, 'image-09.png', 'jobs/nonexistent/image-09.png',
                        '2' * 64, 10, 5, 'gone')
        self.assertEqual(self.client.get(f'/jobs/{job_id}/download').status_code, 404)
        self.assertEqual(
            self.client.get(f'/jobs/{job_id}/artifacts/image-09.png').status_code, 404)

        added = self.client.post('/gallery', data={'job_id': job_id, 'name': 'image-09.png'})
        self.assertEqual(added.status_code, 404)

    def test_missing_job_is_404(self):
        self.assertEqual(self.client.get('/jobs/nope').status_code, 404)




# 两张内容不同的合法 1×1 PNG。测试「输入图进幂等键」时必须真的换一张图。
PNG_A = base64.b64decode(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==')
PNG_B = base64.b64decode(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==')


class EditAppTest(unittest.TestCase):
    """改图入口：上传、模式校验，以及「输入图必须进幂等键」。"""

    @classmethod
    def setUpClass(cls):
        db.init()
        settings.ensure_dirs()
        cls.client = TestClient(web.app)

    def setUp(self):
        reset()

    def _post(self, data=PNG_A, **fields):
        form = {'mode': 'img2img', 'prompt': '', 'images': 1, 'steps': 20,
                'cfg': 1.0, 'sampler': 'euler', 'scheduler': 'simple',
                'seed': 0, 'denoise': 0.6, 'negative': ''}
        form.update(fields)
        return self.client.post('/apps/edit/jobs',
                                files={'file': ('in.png', data, 'image/png')},
                                data=form, follow_redirects=False)

    def test_page_renders_both_modes(self):
        response = self.client.get('/apps/edit')
        self.assertEqual(response.status_code, 200)
        for marker in ('图生图', '指令编辑', '输入图片'):
            self.assertIn(marker, response.text)

    def test_img2img_allows_empty_prompt(self):
        # 图生图的「画个变体」不需要提示词
        self.assertEqual(self._post(prompt='').status_code, 303)

    def test_edit_requires_prompt(self):
        response = self._post(mode='edit', prompt='   ')
        self.assertEqual(response.status_code, 400)
        self.assertIn('指令', response.json()['detail'])

    def test_upload_must_be_png_or_jpeg(self):
        response = self._post(data=b'not an image at all')
        self.assertEqual(response.status_code, 400)
        self.assertIn('PNG', response.json()['detail'])

    def test_oversized_upload_is_rejected(self):
        original = settings.MAX_UPLOAD_BYTES
        settings.MAX_UPLOAD_BYTES = 1024
        try:
            response = self._post(data=PNG_A + b'0' * 4096)
        finally:
            settings.MAX_UPLOAD_BYTES = original
        self.assertEqual(response.status_code, 400)
        self.assertIn('MiB', response.json()['detail'])

    def test_same_image_same_params_is_409(self):
        self.assertEqual(self._post(seed=7).status_code, 303)
        self.assertEqual(self._post(seed=7).status_code, 409)

    def test_two_different_images_are_both_accepted(self):
        """输入图必须参与幂等键：否则第二张图会被唯一约束永久挡住。"""
        self.assertEqual(self._post(seed=7).status_code, 303)
        self.assertEqual(self._post(data=PNG_B, seed=7).status_code, 303)

    def test_job_page_and_input_route(self):
        self.assertEqual(self._post(seed=11).status_code, 303)
        job = db.recent_jobs(limit=1)[0]
        self.assertEqual(job['mode'], 'img2img')
        self.assertIn('输入图', self.client.get('/jobs/' + job['id']).text)
        self.assertEqual(self.client.get('/jobs/' + job['id'] + '/input').status_code, 200)
class ApiTest(unittest.TestCase):
    """JSON API：agent 的入口。这里钉住它的形状与 agent 友好的行为。"""

    @classmethod
    def setUpClass(cls):
        db.init()
        settings.ensure_dirs()
        cls.client = TestClient(web.app)

    def setUp(self):
        reset()

    def test_capabilities_are_self_describing(self):
        body = self.client.get('/api/v1').json()
        self.assertEqual(body['service'], 'aladin')
        for key in ('text_to_image', 'edit_image', 'get_job', 'gallery'):
            self.assertIn(key, body['endpoints'])
        self.assertIn('openapi', body)

    def test_params_expose_bounds_and_presets(self):
        body = self.client.get('/api/v1/params').json()
        for key in ('limits', 'defaults', 'sizes', 'samplers', 'edit_modes'):
            self.assertIn(key, body)

    def test_submit_returns_job_without_redirect(self):
        response = self.client.post('/api/v1/images',
                                    json={'prompt': 'a teapot', 'size': 'portrait', 'steps': 8})
        self.assertEqual(response.status_code, 202)
        body = response.json()
        self.assertTrue(body['created'])
        self.assertEqual(body['mode'], 'txt2img')
        self.assertEqual(body['params']['width'], 768)
        self.assertEqual(body['state'], 'pending')
        self.assertTrue(body['links']['self'].endswith(body['id']))

    def test_duplicate_returns_existing_job_not_an_error_string(self):
        """agent 最需要的：撞车时也要拿到一个可用的 job id，而不是一段中文。"""
        first = self.client.post('/api/v1/images', json={'prompt': 'dup', 'seed': 5}).json()
        again = self.client.post('/api/v1/images', json={'prompt': 'dup', 'seed': 5})
        self.assertEqual(again.status_code, 200)
        self.assertFalse(again.json()['created'])
        self.assertEqual(again.json()['id'], first['id'])

    def test_invalid_params_return_a_list_of_reasons(self):
        response = self.client.post('/api/v1/images', json={'prompt': 'x', 'steps': 99})
        self.assertEqual(response.status_code, 422)
        self.assertTrue(any('步数' in item for item in response.json()['detail']))

    def test_edit_base64_accepts_an_image_without_multipart(self):
        import base64
        response = self.client.post('/api/v1/edits/base64', json={
            'image_base64': base64.b64encode(PNG_A).decode(),
            'mode': 'img2img', 'prompt': '', 'steps': 8, 'seed': 1})
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()['mode'], 'img2img')
        self.assertIsNotNone(response.json()['input_url'])

    def test_edit_duplicate_also_returns_the_existing_job(self):
        """改图路径的幂等键必须与 pipeline 入库时算的那个完全一致。

        API 提交前会先算一遍键用于 409 反查；两处归一化只要有一点不同，
        反查就会落空，agent 只能拿到一个没有 id 的 409。所以这条要真跑一次。
        """
        import base64
        payload = {'image_base64': base64.b64encode(PNG_A).decode(),
                   'mode': 'edit', 'prompt': 'make it blue', 'steps': 8, 'seed': 3}
        first = self.client.post('/api/v1/edits/base64', json=payload).json()
        again = self.client.post('/api/v1/edits/base64', json=payload)
        self.assertEqual(again.status_code, 200)
        self.assertFalse(again.json()['created'])
        self.assertEqual(again.json()['id'], first['id'])

    def test_edit_base64_rejects_non_image(self):
        import base64
        response = self.client.post('/api/v1/edits/base64', json={
            'image_base64': base64.b64encode(b'not an image').decode(), 'mode': 'img2img'})
        self.assertEqual(response.status_code, 422)
        self.assertIn('PNG', response.json()['detail'][0])

    def test_job_detail_carries_progress_and_links(self):
        job_id = self.client.post('/api/v1/images', json={'prompt': 'x'}).json()['id']
        body = self.client.get('/api/v1/jobs/' + job_id).json()
        self.assertEqual(body['id'], job_id)
        self.assertIn('progress', body)
        self.assertIn('page', body['links'])

    def test_unknown_job_is_404(self):
        self.assertEqual(self.client.get('/api/v1/jobs/nope').status_code, 404)

    def test_openapi_schema_is_served(self):
        body = self.client.get('/openapi.json').json()
        self.assertIn('/api/v1/images', body['paths'])
        self.assertIn('/api/v1/edits/base64', body['paths'])


class PagesTest(unittest.TestCase):
    """页面导航：站点要能走通，不能只有孤立的两个表单页。"""

    @classmethod
    def setUpClass(cls):
        db.init()
        settings.ensure_dirs()
        cls.client = TestClient(web.app)

    def setUp(self):
        reset()

    def test_nav_links_every_section(self):
        for path in ('/', '/apps/image', '/apps/edit', '/jobs', '/gallery'):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, path)
            for target in ('/apps/image', '/apps/edit', '/jobs', '/gallery'):
                self.assertIn('href="' + target + '"', response.text, path)

    def test_jobs_page_filters_by_state(self):
        db.create_job('pending one', {'images': 1, 'seed': 1, 'width': 1024, 'height': 1024,
                                      'steps': 8, 'cfg': 1.0, 'sampler': 'euler',
                                      'scheduler': 'simple', 'negative': ''},
                      {'key': 'a' * 64}, 'a' * 64, 'a' * 64)
        response = self.client.get('/jobs?state=succeeded')
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('pending one', response.text)
        self.assertIn('pending one', self.client.get('/jobs').text)

    def test_gallery_page_is_html(self):
        body = self.client.get('/gallery')
        self.assertEqual(body.status_code, 200)
        self.assertIn('text/html', body.headers['content-type'])
        self.assertIn('收藏', body.text)


class DeleteAndFilterTest(unittest.TestCase):
    """删除任务、历史清理，以及列表过滤在 LIMIT 之前生效。"""

    @classmethod
    def setUpClass(cls):
        db.init()
        settings.ensure_dirs()
        cls.client = TestClient(web.app)

    def setUp(self):
        reset()

    def _job(self, key, mode='txt2img', state='succeeded', input_path=None):
        needs_input = mode in ('img2img', 'edit')
        job_id = db.create_job('job ' + key, {'images': 1, 'seed': 1}, {'key': key},
                               key * 64, mode=mode,
                               input_sha256='e' * 64 if needs_input else None,
                               input_path=input_path or ('uploads/x.png' if needs_input else None))
        db.update_job(job_id, state=state)
        return job_id

    def _artifact(self, job_id):
        directory = settings.JOBS_DIR / job_id
        directory.mkdir(parents=True, exist_ok=True)
        (directory / 'image-01.png').write_bytes(b'png')
        db.add_artifact(job_id, 'image-01.png', f'jobs/{job_id}/image-01.png',
                        'd' * 64, 3, 1, 'p')
        return directory

    def test_delete_removes_rows_files_and_orphan_input(self):
        upload = settings.INPUT_DIR / 'orphan.png'
        upload.write_bytes(b'x')
        job_id = self._job('a', mode='img2img', input_path='uploads/orphan.png')
        directory = self._artifact(job_id)
        response = self.client.post(f'/jobs/{job_id}/delete', follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertIsNone(db.job(job_id))
        self.assertFalse(directory.exists())
        self.assertFalse(upload.exists())

    def test_delete_keeps_shared_input_and_gallery_copy(self):
        upload = settings.INPUT_DIR / 'shared.png'
        upload.write_bytes(b'x')
        first = self._job('a', mode='img2img', input_path='uploads/shared.png')
        self._job('b', mode='img2img', input_path='uploads/shared.png')
        self._artifact(first)
        from aladin import gallery
        gallery.promote(first, 'image-01.png')
        self.assertEqual(self.client.delete(f'/api/v1/jobs/{first}').status_code, 200)
        self.assertTrue(upload.exists(), '另一个任务还在用这张输入图')
        item = db.gallery_items()[0]
        self.assertTrue(db.data_path(item['rel_path']).is_file(), '图库副本必须保留')

    def test_active_job_cannot_be_deleted(self):
        job_id = self._job('a', state='running')
        self.assertEqual(self.client.delete(f'/api/v1/jobs/{job_id}').status_code, 409)
        self.assertIsNotNone(db.job(job_id))
        self.assertEqual(self.client.delete('/api/v1/jobs/nope').status_code, 404)

    def test_purge_skips_recent_and_reviewed_jobs(self):
        from aladin import cleanup
        old, reviewed, recent = self._job('a'), self._job('b'), self._job('c')
        self._artifact(reviewed)
        db.review_artifact(reviewed, 'image-01.png', {'anatomy': 'pass'})
        with db.connect() as connection:
            connection.execute("UPDATE jobs SET finished_at = now() - interval '40 days'"
                               ' WHERE id = ANY(%s)', ([old, reviewed],))
            connection.execute('UPDATE jobs SET finished_at = now() WHERE id = %s', (recent,))
        self.assertEqual(cleanup.purge(30, dry_run=True), 1)
        self.assertIsNotNone(db.job(old), 'dry-run 不删')
        self.assertEqual(cleanup.purge(30), 1)
        self.assertIsNone(db.job(old))
        self.assertIsNotNone(db.job(reviewed))
        self.assertIsNotNone(db.job(recent))

    def test_mode_filter_applies_before_limit(self):
        edit = self._job('a', mode='edit')
        for key in 'bcd':
            self._job(key)
        body = self.client.get('/api/v1/jobs?mode=edit&limit=2').json()
        self.assertEqual([job['id'] for job in body], [edit])

    def test_running_filter_includes_queued_states(self):
        self._job('a', state='pending')
        self._job('b', state='unknown')
        self._job('c', state='succeeded')
        text = self.client.get('/jobs?state=running').text
        self.assertIn('job a', text)
        self.assertIn('job b', text)
        self.assertNotIn('job c', text)


class ProgressTest(unittest.TestCase):
    def test_percent_counts_finished_images_not_image_index(self):
        """实测采样顺序 2 → 3 → 1：采第 1 张时已经完成两张，应在 67% 以上而不是回到 0。"""
        from aladin.api import overall_percent, progress_view
        self.assertEqual(overall_percent({'image': 1, 'total': 3, 'step': 6, 'max': 12}, {2, 3}), 83)
        self.assertEqual(overall_percent({'image': 2, 'total': 3, 'step': 6, 'max': 12}), 17)
        self.assertEqual(overall_percent({'image': 2, 'total': 3, 'step': 12, 'max': 12}, {2}), 33)
        self.assertEqual(overall_percent({'step': 3, 'max': 6}), 50)
        self.assertIsNone(overall_percent({'step': None, 'max': 20}))

        def step(image, n):
            return {'kind': 'progress', 'message': json.dumps(
                {'kind': 'step', 'image': image, 'total': 3, 'step': n, 'max': 12})}
        events = [step(2, 12), step(3, 12), step(1, 6)]
        self.assertEqual(progress_view(events)['percent'], 83)
        self.assertEqual(progress_view(events)['images_done'], 2)
        # 重试后重新计数
        retried = events + [{'kind': 'retry', 'message': ''}, step(3, 6)]
        self.assertEqual(progress_view(retried)['percent'], 17)


class VaryTest(unittest.TestCase):
    def setUp(self):
        reset()
        db.init()
        settings.ensure_dirs()
        self.client = TestClient(web.app)

    def _job(self, seed=10):
        return db.create_job('a teapot', {'images': 1, 'seed': seed, 'width': 1024,
                                          'height': 1024, 'steps': 8, 'cfg': 1.0,
                                          'sampler': 'euler', 'scheduler': 'simple',
                                          'negative': ''}, {'key': 'c' * 64},
                             'c' * 64, 'c' * 64)

    def test_vary_creates_a_new_job_with_next_seed(self):
        job_id = self._job(seed=10)
        response = self.client.post('/jobs/' + job_id + '/vary', follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        newest = db.recent_jobs(limit=1)[0]
        self.assertNotEqual(newest['id'], job_id)
        self.assertEqual(newest['params']['seed'], 11)

    def test_vary_is_404_for_unknown_job(self):
        self.assertEqual(self.client.post('/jobs/nope/vary').status_code, 404)

    def test_billing_endpoint_without_snapshot(self):
        response = self.client.get('/api/v1/billing')
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()['available'])

    def test_billing_snapshot_reports_costs_and_balance(self):
        summary = types.SimpleNamespace(
            start=datetime(2026, 9, 1, tzinfo=timezone.utc),
            end=datetime(2026, 10, 1, tzinfo=timezone.utc),
            metered_cost=Decimal('11.25'),
            billed_cost=Decimal('0'),
            adjustments={'Credits': Decimal('-7.44'),
                         'Free Storage': Decimal('-3.81')},
            metered_cost_breakdown={'Volumes': Decimal('3.81')})
        with mock.patch.object(settings, 'CREDIT_GRANT', 30.0):
            db.save_billing(billing.summarize(summary))
        body = self.client.get('/api/v1/billing').json()
        self.assertTrue(body['available'])
        self.assertAlmostEqual(body['metered_cost'], 11.25)
        self.assertAlmostEqual(body['credits_applied'], 7.44)
        self.assertAlmostEqual(body['balance_estimate'], 22.56)
        self.assertAlmostEqual(body['adjustments']['credits'], -7.44)
        self.assertAlmostEqual(body['breakdown']['volumes'], 3.81)

    def test_index_has_billing_widget(self):
        self.assertIn('id="billing"', self.client.get('/').text)

class VideoAppTest(unittest.TestCase):
    """图生视频入口：表单、参数校验、幂等闸门、任务页播放器与 JSON API。

    全程不碰 Modal：只到「登记成一条视频任务」为止，容器侧由 test_pipeline 覆盖。
    """

    @classmethod
    def setUpClass(cls):
        db.init()
        settings.ensure_dirs()
        cls.client = TestClient(web.app)

    def setUp(self):
        reset()

    def _post(self, data=PNG_A, **fields):
        form = {'prompt': 'clouds drift slowly', 'model': 'ltx-2.5', 'duration': 'short',
                'size': 'landscape', 'seed': 0, 'loras': ''}
        form.update(fields)
        return self.client.post('/apps/video/jobs',
                                files={'file': ('in.png', data, 'image/png')},
                                data=form, follow_redirects=False)

    def _newest_job(self):
        return db.recent_jobs(limit=1)[0]

    def test_page_renders_the_form(self):
        response = self.client.get('/apps/video')
        self.assertEqual(response.status_code, 200)
        for marker in ('起始图', '提示词', '时长', '尺寸', '开始生成', '高级参数'):
            self.assertIn(marker, response.text)

    def test_index_lists_the_video_app(self):
        self.assertIn('生成视频', self.client.get('/').text)

    def test_prompt_may_be_empty(self):
        # 起始图已经承载内容，提示词只描述运动
        self.assertEqual(self._post(prompt='').status_code, 303)

    def test_unknown_duration_is_rejected(self):
        response = self._post(duration='forever')
        self.assertEqual(response.status_code, 400)
        self.assertIn('时长', response.json()['detail'])

    def test_unknown_size_is_rejected(self):
        response = self._post(size='gigantic')
        self.assertEqual(response.status_code, 400)
        self.assertIn('尺寸', response.json()['detail'])

    def test_upload_must_be_png_or_jpeg(self):
        response = self._post(data=b'definitely not an image')
        self.assertEqual(response.status_code, 400)

    def test_submit_enqueues_a_video_job(self):
        self.assertEqual(self._post(seed=3).status_code, 303)
        row = self._newest_job()
        self.assertEqual(row['app'], 'video')
        self.assertEqual(row['mode'], 'i2v')
        self.assertEqual(row['request']['model'], 'ltx-2.5')
        self.assertEqual(row['params']['frames'], 49, 'short 档 = 49 帧')
        self.assertEqual(row['params']['seconds'], 2.04)
        self.assertEqual(row['image_count'], 1)

    def test_submit_with_video_loras_appends_triggers(self):
        loras = json.dumps([{'id': 'ltx23-deepthroat', 'strength': 0.9}])
        self.assertEqual(self._post(seed=4, model='ltx-2.3', loras=loras).status_code, 303)
        row = self._newest_job()
        self.assertEqual(row['request']['model'], 'ltx-2.3')
        self.assertEqual(row['request']['loras'][0]['strength'], 0.9)
        self.assertIn('LTXdeepthroat', row['request']['prompt'])

    def test_video_lora_from_the_other_model_is_rejected(self):
        loras = json.dumps([{'id': 'ltx23-deepthroat'}])
        self.assertEqual(self._post(seed=4, model='ltx-2.5', loras=loras).status_code, 400)

    def test_same_image_same_params_redirects_to_the_existing_job(self):
        first = self._post(seed=5)
        self.assertEqual(first.status_code, 303)
        again = self._post(seed=5)
        # 幂等闸门：不新建，直接把用户送回已存在的那条任务
        self.assertEqual(again.status_code, 303)
        self.assertEqual(again.headers['location'], first.headers['location'])
        self.assertEqual(len(db.recent_jobs(limit=10)), 1)

    def test_different_image_same_params_is_accepted(self):
        self.assertEqual(self._post(seed=5).status_code, 303)
        self.assertEqual(self._post(data=PNG_B, seed=5).status_code, 303)

    def test_job_page_renders_a_video_player_not_a_gallery_button(self):
        self._post(seed=9)
        job_id = self._newest_job()['id']
        db.add_artifact(job_id, 'video-01.webm', f'jobs/{job_id}/video-01.webm',
                        'a' * 64, 2048, 9, 'clouds drift')
        response = self.client.get('/jobs/' + job_id)
        self.assertEqual(response.status_code, 200)
        self.assertIn('<video', response.text)
        self.assertIn('起始图', response.text)
        self.assertIn('MiB', response.text, '视频下载按钮要标出体积')
        # 视频现在也能长期保留：任务页要有「加入收藏」而不是只让下载
        self.assertIn('加入收藏', response.text)

    def test_video_artifact_served_as_webm(self):
        self._post(seed=21)
        job_id = self._newest_job()['id']
        directory = settings.JOBS_DIR / job_id
        directory.mkdir(parents=True, exist_ok=True)
        (directory / 'video-01.webm').write_bytes(b'\x1a\x45\xdf\xa3' + b'0' * 16)
        db.add_artifact(job_id, 'video-01.webm', f'jobs/{job_id}/video-01.webm',
                        'b' * 64, 20, 21, 'clouds drift')
        response = self.client.get(f'/jobs/{job_id}/artifacts/video-01.webm')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['content-type'], 'video/webm')
        api_response = self.client.get(f'/api/v1/jobs/{job_id}/artifacts/video-01.webm')
        self.assertEqual(api_response.status_code, 200)
        self.assertEqual(api_response.headers['content-type'], 'video/webm')
        self.assertEqual(api_response.content, response.content)

    def test_api_accepts_a_video_request_with_base64(self):
        response = self.client.post('/api/v1/videos/base64', json={
            'image_base64': base64.b64encode(PNG_A).decode(),
            'prompt': 'camera pans right', 'duration': 'short', 'seed': 1})
        self.assertEqual(response.status_code, 202)
        body = response.json()
        self.assertEqual(body['app'], 'video')
        self.assertEqual(body['mode'], 'i2v')
        self.assertEqual(body['state'], 'pending')
        self.assertEqual(body['artifacts'], [])

    def test_api_video_rejects_an_unknown_duration(self):
        response = self.client.post('/api/v1/videos/base64', json={
            'image_base64': base64.b64encode(PNG_A).decode(), 'duration': 'forever'})
        self.assertEqual(response.status_code, 422)

    def test_api_describes_the_video_capability(self):
        endpoints = self.client.get('/api/v1').json()['endpoints']
        self.assertIn('image_to_video', endpoints)
        self.assertIn('image_to_video_base64', endpoints)
        video = self.client.get('/api/v1/params').json()['video']
        self.assertEqual(video['fps'], 24)
        self.assertIn('normal', video['durations'])
        self.assertIn('landscape', video['sizes'])
        self.assertEqual(set(video['models']), {'ltx-2.3', 'ltx-2.5'})


class CollectionTest(unittest.TestCase):
    """长期保留：图片和视频都能进收藏，且必须是独立副本。"""

    @classmethod
    def setUpClass(cls):
        db.init()
        settings.ensure_dirs()
        cls.client = TestClient(web.app)

    def setUp(self):
        reset()

    def _job_with(self, name, payload, app='image', mode='txt2img', sha='f' * 64):
        """造一条带单个产物的任务（不碰 Modal）。"""
        params = {'images': 1, 'seed': 5, 'width': 832, 'height': 480, 'steps': 4,
                  'cfg': 1.0, 'sampler': 'euler', 'scheduler': 'simple', 'negative': ''}
        # 非文生图的任务按 schema 必须带输入图（jobs_input_check 守着这条）
        source = ('f' * 64, 'uploads/' + 'f' * 16 + '.png') if mode != 'txt2img' else (None, None)
        job_id = db.create_job('keep me', params, {'key': sha}, sha, f'shape-{name}',
                               mode=mode, app=app, input_sha256=source[0],
                               input_path=source[1])
        directory = settings.JOBS_DIR / job_id
        directory.mkdir(parents=True, exist_ok=True)
        (directory / name).write_bytes(payload)
        db.add_artifact(job_id, name, f'jobs/{job_id}/{name}', sha, len(payload), 5, 'keep me')
        return job_id

    def test_video_is_kept_as_its_own_copy(self):
        import shutil

        job_id = self._job_with('video-01.webm', b'\x1a\x45\xdf\xa3' + b'v' * 32,
                                app='video', mode='i2v', sha='a' * 64)
        response = self.client.post('/gallery',
                                    data={'job_id': job_id, 'name': 'video-01.webm'})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['added'])
        item = self.client.get('/api/v1/gallery').json()[0]
        self.assertEqual(item['kind'], 'video')
        stored = db.gallery_items()[0]
        self.assertTrue(stored['rel_path'].endswith('.webm'),
                        f"视频副本要保留扩展名，实际 {stored['rel_path']}")
        self.assertTrue(db.data_path(stored['rel_path']).is_file())
        self.assertEqual(self.client.get(f"/gallery/{item['id']}/file").headers['content-type'],
                         'video/webm')
        self.assertIn('<video', self.client.get('/gallery').text)
        # 独立副本：清掉生成历史后收藏仍可访问
        shutil.rmtree(settings.JOBS_DIR / job_id)
        self.assertEqual(self.client.get(f"/gallery/{item['id']}/file").status_code, 200)

    def test_images_and_videos_are_listed_and_filterable(self):
        image_job = self._job_with('image-01.png', b'\x89PNG\r\n\x1a\n' + b'i' * 32,
                                   sha='b' * 64)
        video_job = self._job_with('video-01.webm', b'\x1a\x45\xdf\xa3' + b'v' * 32,
                                   app='video', mode='i2v', sha='c' * 64)
        self.client.post('/gallery', data={'job_id': image_job, 'name': 'image-01.png'})
        self.client.post('/gallery', data={'job_id': video_job, 'name': 'video-01.webm'})
        items = self.client.get('/api/v1/gallery').json()
        self.assertEqual(sorted(i['kind'] for i in items), ['image', 'video'])
        page = self.client.get('/gallery?kind=video').text
        self.assertIn('<video', page)
        self.assertNotIn('image-01.png', page)
        self.assertIn('data-kind="video"', page)

    def test_unknown_artifact_type_is_refused(self):
        job_id = self._job_with('note.html', b'<html></html>', sha='d' * 64)
        response = self.client.post('/gallery', data={'job_id': job_id, 'name': 'note.html'})
        self.assertEqual(response.status_code, 400)
        self.assertIn('不支持的产物类型', response.json()['detail'])

    def test_pages_carry_the_zoom_hook(self):
        """点媒体本体要就地放大：页面带 data-lightbox，base 提供放大层。"""
        job_id = self._job_with('video-01.webm', b'\x1a\x45\xdf\xa3' + b'v' * 32,
                                app='video', mode='i2v', sha='e' * 64)
        self.client.post('/gallery', data={'job_id': job_id, 'name': 'video-01.webm'})
        job_page = self.client.get(f'/jobs/{job_id}').text
        self.assertIn('id="lightbox"', job_page, 'base.html 要提供放大层')
        self.assertIn('data-lightbox', job_page)
        gallery_page = self.client.get('/gallery').text
        self.assertIn('data-lightbox', gallery_page)
        self.assertIn('data-kind="video"', gallery_page)
        self.assertIn('data-kind="video"', self.client.get('/apps/video').text)


if __name__ == '__main__':
    unittest.main(verbosity=2)


class GalleryOrganizeTest(unittest.TestCase):
    """收藏整理：来源随收藏抄下、标签与星级可改、检索条件在 SQL 里组合。"""

    @classmethod
    def setUpClass(cls):
        db.init()
        settings.ensure_dirs()
        cls.client = TestClient(web.app)

    def setUp(self):
        reset()

    def _promote(self, name, sha, prompt, params, app='image', mode='txt2img'):
        from aladin import gallery
        source = ('e' * 64, 'uploads/e.png') if mode != 'txt2img' else (None, None)
        job_id = db.create_job(prompt, params, {'key': sha}, sha, mode=mode, app=app,
                               input_sha256=source[0], input_path=source[1])
        directory = settings.JOBS_DIR / job_id
        directory.mkdir(parents=True, exist_ok=True)
        (directory / name).write_bytes(sha.encode())
        db.add_artifact(job_id, name, f'jobs/{job_id}/{name}', sha, 64, 1, prompt)
        gallery.promote(job_id, name)
        return db.gallery_items()[0]['id']

    def test_organize_and_search(self):
        pony = self._promote('image-01.png', 'a' * 64, 'Red Dress at night',
                             {'model': 'pony-realism-2.2', 'rating': 'explicit'})
        qwen = self._promote('image-01.png', 'b' * 64, 'blue sky 100% clear',
                             {'rating': 'general'})
        video = self._promote('video-01.webm', 'c' * 64, 'waves', {'rating': 'suggestive'},
                              app='video', mode='i2v')
        items = {item['id']: item for item in self.client.get('/api/v1/gallery').json()}
        self.assertEqual(items[pony]['model'], 'pony-realism-2.2')
        self.assertEqual(items[qwen]['model'], 'qwen-image-2.1')
        self.assertEqual(items[video]['model'], '10eros-max')

        r = self.client.patch(f'/api/v1/gallery/{pony}',
                              json={'tags': [' night  shot ', 'Fav', 'fav', ''], 'stars': 4})
        self.assertEqual(r.status_code, 200)
        self.assertEqual((r.json()['tags'], r.json()['stars']), (['night shot', 'Fav'], 4))
        self.assertEqual(self.client.patch(f'/api/v1/gallery/{pony}', json={'stars': 6}).status_code, 422)
        self.assertEqual(self.client.patch(f'/api/v1/gallery/{pony}', json={'tags': ['x' * 33]}).status_code, 422)
        self.assertEqual(self.client.patch('/api/v1/gallery/9999', json={'stars': 1}).status_code, 404)
        self.client.patch(f'/api/v1/gallery/{qwen}', json={'tags': ['fav'], 'stars': 2})

        def ids(**query):
            return [item['id'] for item in self.client.get('/api/v1/gallery', params=query).json()]
        self.assertEqual(ids(q='red dress'), [pony], '提示词搜索不分大小写')
        self.assertEqual(ids(q='100%'), [qwen], '% 按字面匹配')
        self.assertEqual(ids(tag='Fav'), [pony])
        self.assertEqual(ids(kind='video'), [video])
        self.assertEqual(set(ids(kind='image')), {pony, qwen})
        self.assertEqual(ids(min_stars=3), [pony])
        self.assertEqual(ids(rating='suggestive'), [video])
        self.assertEqual(ids(sort='stars')[:2], [pony, qwen])
        facets = self.client.get('/api/v1/gallery/facets').json()
        self.assertEqual({t['tag'] for t in facets['tags']}, {'Fav', 'fav', 'night shot'})

        page = self.client.get('/gallery', params={'tag': 'Fav'})
        self.assertEqual(page.status_code, 200)
        self.assertIn('Red Dress at night', page.text)
        self.assertNotIn('blue sky', page.text)
        self.assertIn('Pony Realism 2.2', page.text)
        self.assertIn('data-tags="[&#34;night shot&#34;, &#34;Fav&#34;]"', page.text)
