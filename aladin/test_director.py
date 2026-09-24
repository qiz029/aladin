"""Director 两阶段的恢复、幂等、有效计划与可复现交付。"""
import io
import json
import math
import os
import tempfile
import types
import unittest
import zipfile
from unittest.mock import patch

from aladin.testing import configure, reset
configure()
os.environ.setdefault('ALADIN_DATA', tempfile.mkdtemp(prefix='aladin-director-'))
from fastapi.testclient import TestClient
from aladin import db, director, params, pipeline, planner_schema, settings, web, worker
import request

V = {'prompt': 'a brass compass', 'negative': 'text, watermark', 'size': 'square',
     'width': 1024, 'height': 1024, 'steps': 20, 'cfg': 1.0, 'seed': 17,
     'rationale': '突出黄铜质感'}


class DirectorTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.init()
        settings.ensure_dirs()
        cls.client = TestClient(web.app)

    def setUp(self):
        reset()

    def create(self, count=None):
        built, errors = params.director_params('两个视角的黄铜指南针', count)
        self.assertFalse(errors)
        return pipeline.enqueue('两个视角的黄铜指南针', count or 1, built, mode='director')

    def plan(self):
        return {'variants': [dict(V), dict(V, prompt='a compass from above', seed=18)],
                'notes': [], 'raw': '{"images": []}', 'model': 'test-model',
                'plannerRevision': director.revision()}

    def test_form_and_api_share_idempotent_enqueue_without_modal(self):
        with patch.object(pipeline, 'modal', types.SimpleNamespace()):
            response = self.client.post('/api/v1/director', json={'brief': '  一个指南针  '})
            self.assertEqual(response.status_code, 202)
            job = response.json()
            self.assertEqual(job['stage'], 'planning')
            self.assertIsNone(job['generation_request']['count'])
            form = self.client.post('/apps/director/jobs', data={'brief': '一个指南针'}, follow_redirects=False)
            self.assertEqual(form.status_code, 303)
            self.assertEqual(form.headers['location'], '/jobs/' + job['id'])
            again = self.client.post('/api/v1/director', json={'brief': '一个指南针'})
            self.assertEqual(again.status_code, 200)
            self.assertFalse(again.json()['created'])
            self.assertEqual(self.client.get('/apps/director').status_code, 200)
            self.assertEqual(self.client.get('/jobs/' + job['id']).status_code, 200)

    def test_invalid_brief_and_counts_never_enqueue(self):
        for body in ({'brief': ''}, {'brief': 'x'*4001}, {'brief': 'x', 'count': 9},
                     {'brief': 'x', 'count': True}, {'brief': 'x', 'count': 1.5}):
            self.assertEqual(self.client.post('/api/v1/director', json=body).status_code, 422)
        self.assertEqual(db.recent_jobs(), [])

    def test_plan_harness_bounds_nonfinite_numbers_and_auto_count(self):
        variants, notes = planner_schema.validate_plan({'images': [dict(V, cfg=float('nan'))]}, None)
        self.assertTrue(math.isfinite(variants[0]['cfg']))
        self.assertTrue(notes)
        self.assertEqual(planner_schema.plan_schema(None)['properties']['images']['maxItems'], 8)
        with self.assertRaises(ValueError):
            planner_schema.validate_plan({'images': []}, None)

    def test_planner_spawn_takes_one_argument_and_persists_call_id(self):
        job_id = self.create()
        call = types.SimpleNamespace(object_id='fc-planner')
        seen = []
        function = types.SimpleNamespace(spawn=lambda *args: (seen.append(args), call)[1])
        fake = types.SimpleNamespace(Function=types.SimpleNamespace(from_name=lambda *args: function))
        with patch.object(pipeline, 'modal', fake), patch.object(pipeline, '_watch'):
            self.assertTrue(pipeline._submit_one())
        self.assertEqual(len(seen[0]), 1)
        self.assertEqual(db.job(job_id)['call_id'], 'fc-planner')

    def test_plan_transition_is_atomic_and_replay_does_not_enqueue_twice(self):
        job_id = self.create()
        db.update_job(job_id, state='submitted', call_id='fc-plan')
        row = db.job(job_id)
        self.assertEqual(pipeline._accept_plan(row, self.plan()), 'pending')
        generated = db.job(job_id)
        self.assertEqual(generated['state'], 'pending')
        self.assertIsNone(generated['call_id'])
        self.assertEqual(generated['image_count'], 2)
        self.assertEqual(generated['params']['plan']['variants'][1]['seed'], 18)
        worker.validate(generated['request'])
        db.update_job(job_id, state='submitted', call_id='fc-images')
        pipeline._accept_plan(row, self.plan())
        self.assertEqual(db.job(job_id)['call_id'], 'fc-images')
        self.assertEqual(len([e for e in db.events(job_id) if e['kind']=='planned']), 1)

    def test_director_freezes_tags_and_applies_them_after_planning(self):
        """尺度在提交时按 Qwen 词表冻结；规划期间改默认值不影响这次任务。"""
        with patch.dict(os.environ, ALADIN_DEFAULT_RATING='suggestive'):
            job_id = self.create()
        row = db.job(job_id)
        self.assertEqual(row['request']['brief'], '两个视角的黄铜指南针')
        self.assertEqual(row['request']['promptTags'], ['sensual', 'suggestive'])
        with patch.dict(os.environ, ALADIN_DEFAULT_RATING='general'):
            result, built = director.image_batch(row,self.plan())
        self.assertEqual(result['variants'][0]['prompt'], V['prompt']+', sensual, suggestive')
        self.assertEqual(built['plan']['variants'][0]['prompt_defaults']['original'],V['prompt'])
        worker.validate(result)
        plan = self.plan()
        plan['variants'][0]['prompt'] = 'x'*2000
        result, built = director.image_batch(row,plan)
        self.assertLessEqual(len(result['variants'][0]['prompt']),2000)
        self.assertTrue(result['variants'][0]['prompt'].endswith('sensual, suggestive'))
        self.assertEqual(built['plan']['variants'][0]['prompt_defaults']['original'],'x'*2000)
        self.assertTrue(any('默认标签' in note for note in built['plan']['notes']))

    def test_director_rating_is_frozen_per_request(self):
        built, errors = params.director_params('海边', None, 'general')
        self.assertFalse(errors)
        self.assertEqual(director.build('海边', None, built['rating'])['promptTags'], [])
        self.assertNotEqual(director.build('海边', None, 'general')['key'],
                            director.build('海边', None, 'explicit')['key'])
        _, errors = params.director_params('海边', None, 'loud')
        self.assertTrue(errors)

    def test_known_planner_call_is_polled_without_spawning(self):
        job_id = self.create()
        db.update_job(job_id, state='submitted', call_id='fc-plan')
        fake = types.SimpleNamespace(FunctionCall=types.SimpleNamespace(
            from_id=lambda id: types.SimpleNamespace(get=lambda timeout: self.plan())))
        with patch.object(pipeline, 'modal', fake), patch.object(pipeline, '_watch'):
            self.assertEqual(pipeline._poll_row(db.job(job_id)), 'pending')
        self.assertFalse(director.is_planning(db.job(job_id)))

    def test_unknown_planning_uses_plan_receipt(self):
        job_id = self.create()
        db.update_job(job_id, state='unknown', attempts=1)
        receipt = json.dumps({'request': db.job(job_id)['request'], 'plan': self.plan()}).encode()
        fake = types.SimpleNamespace(Volume=types.SimpleNamespace(from_name=lambda name: object()))
        with patch.object(pipeline, 'modal', fake), patch.object(pipeline, '_read_bytes', return_value=receipt):
            self.assertTrue(pipeline._resolve_unknown_one())
        self.assertFalse(director.is_planning(db.job(job_id)))
        self.assertEqual(db.job(job_id)['attempts'], 0)

    def test_invalid_plan_does_not_enter_image_queue(self):
        job_id = self.create()
        db.update_job(job_id, state='submitted')
        with self.assertRaises(ValueError):
            pipeline._accept_plan(db.job(job_id), {'variants': []})
        self.assertTrue(director.is_planning(db.job(job_id)))

    def test_artifact_parameters_api_prefill_and_zip_are_complete(self):
        job_id = self.create()
        db.update_job(job_id, state='submitted')
        pipeline._accept_plan(db.job(job_id), self.plan())
        directory = settings.JOBS_DIR / job_id
        directory.mkdir(parents=True)
        (directory/'image-01.png').write_bytes(b'image-bytes')
        params = dict(V, sampler='euler', scheduler='simple')
        db.add_artifact(job_id, 'image-01.png', f'jobs/{job_id}/image-01.png', 'a'*64, 11, 17, V['prompt'], params)
        db.finish_job(job_id, 'succeeded')
        body = self.client.get('/api/v1/jobs/'+job_id).json()
        self.assertEqual(body['artifacts'][0]['params']['negative'], V['negative'])
        self.assertEqual(body['artifacts'][0]['prompt'], V['prompt'])
        self.assertEqual(self.client.get('/jobs/'+job_id).status_code, 200)
        page = self.client.get(f'/jobs/{job_id}/artifacts/image-01.png/reuse').text
        self.assertIn(V['prompt'], page)
        self.assertIn(V['negative'], page)
        archive = zipfile.ZipFile(io.BytesIO(self.client.get('/jobs/'+job_id+'/download').content))
        manifest = json.loads(archive.read('generation.json'))
        self.assertEqual(manifest['plan']['variants'][1]['seed'], 18)
        self.assertIn('image-01.png', archive.namelist())

    def test_worker_builder_preserves_current_protocol(self):
        # The regression that would silently undo Q8/variants on the next build.
        import ast
        tree = ast.parse((settings.ROOT/'tools/build_worker.py').read_text())
        values = {n.targets[0].id: ast.literal_eval(n.value) for n in tree.body
                  if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name)
                  and n.targets[0].id in ('header', 'body')}
        source = request.WORKER_PATH.read_text()
        self.assertTrue(source.startswith(values['header']))
        self.assertTrue(source.endswith(values['body']))


if __name__ == '__main__':
    unittest.main()
