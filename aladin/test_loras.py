"""LoRA：目录完整、选择校验、触发词、幂等键、容器图与 API/表单同形。"""
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix='aladin-lora-')
os.environ['ALADIN_DATA'] = _TMP
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aladin.testing import configure, reset  # noqa: E402

configure()

from fastapi.testclient import TestClient  # noqa: E402

import request as request_module  # noqa: E402
from aladin import db, loras, settings, web  # noqa: E402
from aladin.extra_image_request import build  # noqa: E402
from aladin.extra_image_worker import validate, workflow  # noqa: E402
from aladin.params import image_params  # noqa: E402


def params(model='pony-realism-2.2', chosen=None, prompt='a woman on a sofa'):
    return image_params(prompt, 1, None, None, None, None, None, None, 7, model, loras=chosen)


class CatalogTest(unittest.TestCase):
    def test_every_entry_is_pinned_and_consistent(self):
        ids = [item['id'] for item in loras.CATALOG]
        self.assertEqual(len(ids), len(set(ids)))
        files = [item['file'] for item in loras.CATALOG]
        self.assertEqual(len(files), len(set(files)), '同一个 Civitai 版本不该登记两次')
        for item in loras.CATALOG:
            with self.subTest(item['id']):
                self.assertIn(item['family'], loras.VOLUME_OF_FAMILY)
                self.assertIn(item['kind'], loras.KINDS)
                self.assertRegex(item['sha256'], r'^[0-9a-f]{64}$')
                self.assertRegex(item['file'], loras.FILE_PATTERN)
                low, high = item['range']
                self.assertTrue(low <= item['strength'] <= high)
        for preset in loras.PRESETS:
            for choice in preset['loras']:
                self.assertEqual(loras.BY_ID[choice['id']]['family'], preset['family'], preset['id'])


class ResolveTest(unittest.TestCase):
    def test_valid_choice_expands_to_pinned_record(self):
        chosen, errors = loras.resolve([{'id': 'mating-press'}, {'id': 'real-skin-slider', 'strength': -1.5}],
                                       'pony-realism-2.2')
        self.assertEqual(errors, [])
        self.assertEqual(chosen[0]['strength'], 0.8, '省略强度用目录默认值')
        self.assertEqual(chosen[1]['strength'], -1.5, '滑杆允许负值')
        self.assertEqual(chosen[0]['file'], 'civitai-722613.safetensors')

    def test_rejections(self):
        cases = [
            ([{'id': 'nope'}], 'pony-realism-2.2', '未知'),
            ([{'id': 'anima-turbo'}], 'pony-realism-2.2', '只能配'),
            ([{'id': 'mating-press'}], 'qwen-image-2.1', '不支持'),
            ([{'id': 'mating-press'}, {'id': 'mating-press'}], 'pony-realism-2.2', '重复'),
            ([{'id': 'mating-press', 'strength': 5}], 'pony-realism-2.2', '强度'),
            ([{'id': 'mating-press', 'strength': True}], 'pony-realism-2.2', '强度'),
            ([{'id': 'hentai-comic-color'}, {'id': 'hentai-comic-hardcore'}], 'anima-base-1.0', '不能同时'),
            ([{'id': i['id']} for i in loras.CATALOG if i['family'] == 'pony'][:7], 'pony-realism-2.2', '最多'),
        ]
        for choice, model, message in cases:
            with self.subTest(message):
                _, errors = loras.resolve(choice, model)
                self.assertTrue(any(message in e for e in errors), errors)


class ParamsAndRequestTest(unittest.TestCase):
    def test_triggers_are_frozen_into_the_effective_prompt(self):
        built, errors = params(chosen=[{'id': 'mating-press'}, {'id': 'style-photo-2'}],
                               prompt='a woman, photo')
        self.assertEqual(errors, [])
        effective = built['prompt_defaults']['effective']
        self.assertTrue(effective.endswith('mating press, raw, realistic'), effective)
        self.assertEqual(effective.count('photo'), 1, '提示词里已有的触发词不重复')
        self.assertEqual(built['prompt_defaults']['triggers'], ['mating press', 'raw', 'photo', 'realistic'])

    def test_loras_change_the_idempotency_key(self):
        plain, _ = params()
        one, _ = params(chosen=[{'id': 'real-skin-slider'}])
        other, _ = params(chosen=[{'id': 'real-skin-slider', 'strength': 3}])
        keys = {request_module.storage_key_for('a woman on a sofa', 1, p) for p in (plain, one, other)}
        self.assertEqual(len(keys), 3)

    def test_container_graph_chains_loras_after_the_loaders(self):
        for model, lora_ids in [('pony-realism-2.2', ['pony-realism-enhancer', 'real-skin-slider']),
                                ('anima-base-1.0', ['anima-turbo'])]:
            built, errors = params(model, [{'id': i} for i in lora_ids])
            self.assertEqual(errors, [])
            payload = build(built['prompt_defaults']['effective'], 1, built)
            self.assertEqual(validate(payload), payload)
            graph = workflow(payload)
            first, last = '200', str(200 + len(lora_ids) - 1)
            self.assertEqual(graph[first]['inputs']['model'], ['1', 0])
            self.assertEqual(graph[first]['inputs']['clip'], ['2', 0])
            self.assertEqual(graph['13']['inputs']['model'], [last, 0], '采样用最后一个 LoRA 的模型')
            self.assertEqual(graph['10']['inputs']['clip'], [last, 1], '正向编码用最后一个 LoRA 的 clip')
            self.assertEqual(graph['11']['inputs']['clip'], [last, 1], '负向编码同样')

    def test_container_rejects_tampered_entries(self):
        built, _ = params(chosen=[{'id': 'real-skin-slider'}])
        payload = build('x', 1, built)
        for field, value in [('file', '../../etc/passwd'), ('file', 'model.ckpt'), ('sha256', 'abc'),
                             ('strength', float('nan')), ('strength', 50)]:
            with self.subTest(field=field, value=value):
                bad = dict(payload, loras=[dict(payload['loras'][0], **{field: value})])
                with self.assertRaises(ValueError):
                    validate(bad)


class SurfaceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.init()
        settings.ensure_dirs()
        cls.client = TestClient(web.app)

    def setUp(self):
        reset()

    def test_api_and_form_share_the_same_path(self):
        catalog = self.client.get('/api/v1/loras').json()
        self.assertTrue(any(item['id'] == 'anima-turbo' for item in catalog['loras']))
        body = {'prompt': 'a woman', 'model': 'pony-realism-2.2', 'seed': 3,
                'loras': [{'id': 'female-pov', 'strength': 0.6}]}
        job = self.client.post('/api/v1/images', json=body).json()
        self.assertEqual(job['params']['loras'][0]['version'], 960900)
        self.assertEqual(job['generation_request']['loras'][0]['strength'], 0.6)
        self.assertIn('f3mp0v', job['generation_request']['prompts'][0])
        bad = self.client.post('/api/v1/images', json=dict(body, model='qwen-image-2.1'))
        self.assertEqual(bad.status_code, 422)

        form = self.client.post('/apps/image/jobs', data={
            'prompt': 'a woman', 'model': 'pony-realism-2.2', 'seed': '3',
            'loras': '[{"id": "female-pov", "strength": 0.6}]'}, follow_redirects=False)
        self.assertEqual(form.status_code, 409, '同参数同 LoRA：表单与 API 撞到同一个任务')
        self.assertEqual(self.client.post('/apps/image/jobs', data={
            'prompt': 'a woman', 'model': 'pony-realism-2.2', 'loras': '{oops'}).status_code, 400)
        page = self.client.get('/jobs/' + job['id'])
        self.assertIn('Female POV', page.text)

    def test_image_page_ships_catalog_and_reuse_restores_selection(self):
        page = self.client.get('/apps/image')
        self.assertIn('id="loraCatalog"', page.text)
        job = self.client.post('/api/v1/images', json={
            'prompt': 'a woman', 'model': 'anima-base-1.0', 'seed': 4,
            'loras': [{'id': 'mature-slider', 'strength': 3}]}).json()
        db.add_artifact(job['id'], 'image-01.png', 'x.png', 'a' * 64, 1, 4, job['prompt'],
                        dict(job['params'], loras=job['generation_request']['loras']))
        reuse = self.client.get(f"/jobs/{job['id']}/artifacts/image-01.png/reuse")
        self.assertEqual(reuse.status_code, 200)
        self.assertIn('[{"id": "mature-slider", "strength": 3.0}]', reuse.text)


if __name__ == '__main__':
    unittest.main()
