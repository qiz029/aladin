"""创作要求贯穿规划与评审；逐图设置可回填再提交，不调用 GPU。"""
import base64
import io
import os
import tempfile
import unittest
from unittest.mock import patch

from aladin.testing import configure, reset
configure()
os.environ.setdefault('ALADIN_DATA', tempfile.mkdtemp(prefix='aladin-creation-'))
from fastapi.testclient import TestClient
from PIL import Image
from aladin import db, director, pipeline, planner_schema, settings, web


class CreationSettingsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.init()
        settings.ensure_dirs()
        cls.client = TestClient(web.app)

    def setUp(self):
        reset()

    def artifact(self, job, name='image-01.png', seed=None, params=None, prompt=None):
        db.add_artifact(job['id'], name, 'unused.png', 'a'*64, 10,
                        job['params'].get('seed', 0) if seed is None else seed,
                        prompt or job['params'].get('prompt_defaults', {}).get('effective', job['prompt']),
                        params if params is not None else job['params'])
        return f'/api/v1/jobs/{job["id"]}/artifacts/{name}/reuse'

    def test_creative_spec_normalized_shared_and_part_of_identity(self):
        spec = {'must_keep': ' 蓝色外套 ', 'may_change': '背景', 'change_only': ''}
        body = dict(brief='成人时装人像', rating='general', creative_spec=spec, review=True)
        job = self.client.post('/api/v1/director', json=body).json()
        expected = {'must_keep': '蓝色外套', 'may_change': '背景'}
        self.assertEqual(job['params']['creative_spec'], expected)
        self.assertEqual(job['generation_request']['creative_spec'], expected)
        duplicate = self.client.post('/api/v1/director', json=body).json()
        self.assertEqual(job['id'], duplicate['id'])
        form = self.client.post('/apps/director/jobs', data=dict(brief=body['brief'], rating='general',
                               review='true', **spec), follow_redirects=False)
        self.assertEqual(form.headers['location'], '/jobs/' + job['id'])
        other = self.client.post('/api/v1/director', json=dict(body, creative_spec={'must_keep': '红色外套'})).json()
        self.assertNotEqual(job['id'], other['id'])
        self.assertIn('蓝色外套', self.client.get('/jobs/' + job['id']).text)
        from aladin_planner_modal_app import validate_request
        validate_request(job['generation_request'])
        with self.assertRaises(ValueError):
            validate_request(dict(job['generation_request'], creative_spec={'must_keep': 4}))

    def test_creative_spec_rejected_before_enqueue_and_old_payload_unchanged(self):
        for spec in [[], {'unknown': 'x'}, {'must_keep': 3}, {'may_change': 'x'*1001}]:
            with patch.object(pipeline, 'enqueue') as enqueue:
                response = self.client.post('/api/v1/director', json=dict(brief='a coat', creative_spec=spec))
                self.assertEqual(response.status_code, 422, response.text)
                enqueue.assert_not_called()
        self.assertEqual(director.build('coat', rating='general'),
                         director.build('coat', rating='general', creative_spec={'must_keep': ' '}))
        instructions = planner_schema.creative_instructions({'must_keep': 'blue coat', 'change_only': 'lighting'})
        self.assertIn('blue coat', instructions)
        self.assertIn('lighting', instructions)

    def test_spec_survives_planning_approval_and_criterion_review(self):
        job = self.client.post('/api/v1/director', json=dict(brief='blue coat', count=1, rating='general',
            review=True, creative_spec={'must_keep': 'blue coat'})).json()
        variant = dict(prompt='blue coat in daylight', size='portrait', steps=21, cfg=1, seed=31)
        plan = {'variants': [variant], 'plannerRevision': director.revision()}
        db.update_job(job['id'], state='submitted')
        pipeline._accept_plan(db.job(job['id']), plan)
        response = self.client.post(f'/api/v1/jobs/{job["id"]}/approve')
        self.assertEqual(response.status_code, 200, response.text)
        updated = self.client.get('/api/v1/jobs/' + job['id']).json()
        self.assertEqual(updated['params']['creative_spec'], job['params']['creative_spec'])
        path = self.artifact(updated, params=updated['plan']['variants'][0], seed=31)
        response = self.client.put(path.removesuffix('/reuse') + '/review', json=dict(
            anatomy='not_applicable', matches_request='pass', requirements={'must_keep': 'pass'}))
        self.assertEqual(response.status_code, 200, response.text)
        exported = self.client.get('/jobs/' + job['id'] + '/review.json').json()
        self.assertEqual(exported['artifacts'][0]['review']['requirements'], {'must_keep': 'pass'})

    def test_reuse_preserves_single_image_settings_and_roundtrip(self):
        body = dict(prompt='a ceramic teapot', model='pony-realism-2.2', images=3,
                    size='portrait', negative='', seed=90, cfg=6.8, steps=33,
                    sampler='euler_ancestral', scheduler='normal', rating='general',
                    loras=[{'id': 'pony-realism-enhancer', 'strength': 0.65}])
        job = self.client.post('/api/v1/images', json=body).json()
        # worker 只回传文件、哈希与强度，没有 id 或尺度。
        actual = {k: job['params'][k] for k in ('model','negative','width','height','steps','cfg','sampler','scheduler')}
        actual['loras'] = [{k: l[k] for k in ('file','sha256','strength')} for l in job['params']['loras']]
        path = self.artifact(job, name='image-03.png', seed=92, params=actual)
        with patch.object(pipeline, 'enqueue') as enqueue:
            recipe = self.client.get(path).json()
            page = self.client.get(path.removeprefix('/api/v1')).text
            enqueue.assert_not_called()
        expected = dict(body, images=1, seed=92)
        for key, value in expected.items():
            self.assertEqual(recipe['settings'][key], value, key)
        self.assertIn('value="general" checked', page)
        self.assertIn('value="92"', page)
        copied = self.client.post('/api/v1/images', json=recipe['settings']).json()
        self.assertEqual(copied['params']['prompt_defaults']['effective'], job['params']['prompt_defaults']['effective'])
        self.assertEqual(copied['params']['loras'], job['params']['loras'])

    def test_director_reuse_uses_variant_policy_not_batch_policy(self):
        job = self.client.post('/api/v1/director', json=dict(brief='coat', rating='suggestive', count=1)).json()
        variant = dict(prompt='blue coat', negative='', size='portrait', width=768, height=1152,
                       steps=19, cfg=1, seed=83, sampler='euler', scheduler='simple',
                       prompt_defaults={'original':'blue coat', 'effective':'blue coat', 'rating':'general'})
        with db.connect() as connection:
            connection.execute('UPDATE jobs SET params = %s WHERE id = %s',
                               (db.dumps(dict(job['params'], plan={'variants':[variant]})), job['id']))
        path = self.artifact(job, seed=83, params={k:v for k,v in variant.items() if k != 'prompt_defaults'}, prompt='blue coat')
        restored = self.client.get(path).json()['settings']
        self.assertEqual(restored['rating'], 'general')
        self.assertEqual(restored['steps'], 19)
        self.assertEqual(restored['size'], 'portrait')

    def test_edit_reuse_restores_original_input_and_region(self):
        buffer = io.BytesIO()
        Image.new('RGB', (128, 128), 'blue').save(buffer, format='PNG')
        body = dict(image_base64=base64.b64encode(buffer.getvalue()).decode(), mode='edit',
                    prompt='make the coat red', rating='general', region=[0.1,0.2,0.8,0.9],
                    seed=27, negative='text', steps=17)
        job = self.client.post('/api/v1/edits/base64', json=body).json()
        path = self.artifact(job)
        recipe = self.client.get(path).json()
        self.assertEqual(recipe['page'], '/apps/edit')
        self.assertEqual(recipe['input_url'], '/jobs/' + job['id'] + '/input')
        self.assertEqual(recipe['settings']['region'], body['region'])
        self.assertEqual(recipe['settings']['mode'], 'edit')
        page = self.client.get(path.removeprefix('/api/v1'))
        self.assertEqual(page.status_code, 200, page.text)
        self.assertIn('id="reuse-data"', page.text)
        self.assertIn('value="edit" checked', page.text)
        self.assertIn('make the coat red', page.text)
        self.assertIn('text</textarea>', page.text)

    def test_missing_sources_are_explicit_errors(self):
        self.assertEqual(self.client.get('/api/v1/jobs/missing/artifacts/image-01.png/reuse').status_code, 404)
        job = self.client.post('/api/v1/images', json=dict(prompt='teapot', rating='general')).json()
        path = self.artifact(job, params=dict(job['params'], width=123))
        self.assertEqual(self.client.get(path).status_code, 422)


if __name__ == '__main__':
    unittest.main()
