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


class DirectorTargetTest(unittest.TestCase):
    """一句话出图选模型 + LoRA，以及「先看计划再生成」。"""

    @classmethod
    def setUpClass(cls):
        db.init()
        settings.ensure_dirs()
        cls.client = TestClient(web.app)

    def setUp(self):
        reset()

    def enqueue(self, model, loras=None, review=False, brief='海边的一个女人'):
        built, errors = params.director_params(brief, 2, 'explicit', model, loras, review)
        self.assertEqual(errors, [])
        return pipeline.enqueue(brief, 2, built, mode='director')

    def planned(self, job_id, variants):
        row = db.job(job_id)
        db.update_job(job_id, state='running', call_id='fc-plan')
        plan = {'variants': variants, 'notes': [], 'raw': '{}', 'model': 'test',
                'plannerRevision': director.revision()}
        return pipeline._accept_plan(db.job(job_id), plan), row

    def test_target_spec_follows_model_and_lora_settings(self):
        pony = director.target_for('pony-realism-2.2', [])
        self.assertEqual(pony['family'], 'pony')
        self.assertEqual(pony['sizes']['portrait'], {'width': 832, 'height': 1216, 'label': '竖版'})
        chosen, _ = __import__('aladin.loras', fromlist=['x']).resolve([{'id': 'anima-turbo'}], 'anima-base-1.0')
        anima = director.target_for('anima-base-1.0', chosen)
        self.assertEqual((anima['cfg'], anima['steps'], anima['defaults']['sampler']), ([1.0, 1.0], [8, 12], 'euler'))
        prompt = planner_schema.system_prompt(anima)
        self.assertIn('Danbooru', prompt)
        self.assertIn('Anima Turbo LoRA', prompt)
        self.assertIn('成年人', prompt)
        self.assertEqual(planner_schema.plan_schema(2, anima)['properties']['images']['items']['properties']['steps']['maximum'], 12)
        with self.assertRaises(ValueError):
            planner_schema.validate_target(dict(pony, family='sd15'))
        with self.assertRaises(ValueError):
            planner_schema.validate_target(dict(pony, sizes={'x': {'width': 3000, 'height': 8}}))

    def test_model_and_loras_change_the_key(self):
        keys = {director.build('b', 2, 'explicit', m)['key'] for m in ('qwen-image-2.1', 'pony-realism-2.2')}
        chosen, _ = __import__('aladin.loras', fromlist=['x']).resolve([{'id': 'mating-press'}], 'pony-realism-2.2')
        keys.add(director.build('b', 2, 'explicit', 'pony-realism-2.2', chosen)['key'])
        self.assertEqual(len(keys), 3)

    def test_pony_plan_becomes_a_per_image_batch_with_loras(self):
        from aladin import extra_image_worker
        job_id = self.enqueue('pony-realism-2.2', [{'id': 'mating-press'}, {'id': 'real-skin-slider'}])
        variants = [dict(prompt='score_9, 1girl, beach', negative='score_4, blurry', size='portrait',
                         steps=30, cfg=6.0, seed=5),
                    dict(prompt='score_9, 1girl, sunset', negative='', size='landscape', steps=100, cfg=6.5, seed=6)]
        state, _ = self.planned(job_id, variants)
        self.assertEqual(state, 'pending')
        row = db.job(job_id)
        request = row['request']
        self.assertEqual(request['modelId'], 'pony-realism-2.2')
        self.assertEqual(extra_image_worker.validate(request), request)
        first = request['variants'][0]
        self.assertTrue(first['prompt'].startswith('rating_explicit, score_9'), first['prompt'])
        self.assertTrue(first['prompt'].endswith('mating press'))
        self.assertEqual((first['width'], first['height']), (832, 1216))
        self.assertEqual(len(first['loras']), 2)
        self.assertEqual(request['variants'][1]['steps'], 50, '越界步数夹到 Pony 的上限')
        graph = extra_image_worker.workflow(request)
        self.assertEqual(sum(1 for n in graph.values() if n['class_type'] == 'LoraLoader'), 2,
                         '两张图同一套 LoRA：共用一条链')
        self.assertEqual(api_stage(row), 'generating')

    def test_review_then_approve_with_edits(self):
        job_id = self.enqueue('anima-base-1.0', [{'id': 'hentai-studio-quality'}], review=True)
        variants = [dict(prompt='masterpiece, 1girl, cafe', negative='worst quality', size='square', steps=35, cfg=4.5, seed=1),
                    dict(prompt='masterpiece, 1girl, park', negative='worst quality', size='portrait', steps=35, cfg=4.5, seed=2)]
        state, _ = self.planned(job_id, variants)
        self.assertEqual(state, 'review')
        row = db.job(job_id)
        self.assertEqual(row['state'], 'review')
        self.assertIsNone(db.claim("state = 'pending'", (), 'w'), 'review 不被 worker 认领')
        page = self.client.get('/jobs/' + job_id)
        self.assertIn('确认计划', page.text)
        self.assertIn('masterpiece, 1girl, cafe', page.text, '编辑框里是原文，不带自动标签')
        edits = [dict(prompt='masterpiece, 1girl, library', negative='', size='landscape', steps=30, cfg=4.0, seed=9)]
        r = self.client.post(f'/api/v1/jobs/{job_id}/approve', json={'variants': edits})
        self.assertEqual(r.status_code, 200, r.text)
        row = db.job(job_id)
        self.assertEqual((row['state'], row['image_count']), ('pending', 1))
        v = row['request']['variants'][0]
        self.assertEqual(v['prompt'], 'explicit, masterpiece, 1girl, library, hentai_studio_quality')
        self.assertEqual((v['width'], v['height']), (1216, 832))
        self.assertIn('计划经人工确认', row['params']['plan']['notes'])
        self.assertEqual(self.client.post(f'/api/v1/jobs/{job_id}/approve').status_code, 409)

    def test_review_rejects_bad_edits_and_can_be_deleted(self):
        job_id = self.enqueue('qwen-image-2.1', review=True)
        self.planned(job_id, [dict(prompt='a beach', size='square', steps=20, cfg=1.0, seed=3)])
        r = self.client.post(f'/api/v1/jobs/{job_id}/approve', json={'variants': []})
        self.assertEqual(r.status_code, 422)
        self.assertEqual(db.job(job_id)['state'], 'review')
        ok = self.client.post(f'/api/v1/jobs/{job_id}/approve', json={})
        self.assertEqual(ok.status_code, 200, '不带修改 = 原样确认')
        job2 = self.enqueue('qwen-image-2.1', review=True, brief='另一个需求')
        self.planned(job2, [dict(prompt='a hill', size='square', steps=20, cfg=1.0, seed=4)])
        self.assertEqual(self.client.delete(f'/api/v1/jobs/{job2}').status_code, 200, 'review 可以直接删')

    def test_api_and_form_accept_model_loras_review(self):
        r = self.client.post('/api/v1/director', json={
            'brief': '夜晚的街道', 'model': 'pony-realism-2.2', 'review': True,
            'loras': [{'id': 'style-photo-2', 'strength': 0.9}]})
        self.assertEqual(r.status_code, 202, r.text)
        job = r.json()
        self.assertEqual(job['params']['model'], 'pony-realism-2.2')
        self.assertTrue(job['params']['review'])
        self.assertEqual(job['generation_request']['target']['family'], 'pony')
        bad = self.client.post('/api/v1/director', json={'brief': 'x', 'loras': [{'id': 'mating-press'}]})
        self.assertEqual(bad.status_code, 422, 'Qwen 不支持 LoRA')
        form = self.client.post('/apps/director/jobs', data={
            'brief': '夜晚的街道', 'model': 'pony-realism-2.2', 'review': 'true',
            'loras': '[{"id": "style-photo-2", "strength": 0.9}]'}, follow_redirects=False)
        self.assertEqual(form.headers['location'], '/jobs/' + job['id'], '表单与 API 同参数撞同一个任务')
        page = self.client.get('/apps/director')
        self.assertIn('id="loraCatalog"', page.text)
        self.assertIn('先看计划再生成', page.text)


def api_stage(row):
    from aladin.api import director_stage
    return director_stage(row)


MANGA_PANEL = dict(beat='setup', shot='wide', rating='general', characters=[0], action='两人被困在电梯里',
                   caption='晚上十一点。', dialogue=[{'speaker': '美咲', 'text': '……停电了？'}],
                   prompt='masterpiece, 1girl, adult, office lady, elevator', negative='text, speech bubble',
                   steps=35, cfg=4.5, seed=7, rationale='交代处境')


def manga_plan(count=6, **overrides):
    panels = [dict(MANGA_PANEL, **overrides) for _ in range(count)]
    return {'story': {'title': '停电', 'logline': '上司与下属被困电梯，关系失控', 'setting': '深夜写字楼'},
            'characters': [{'name': '美咲', 'appearance': 'long black hair, business suit'}],
            'panels': panels}


class MangaTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.init()
        settings.ensure_dirs()
        cls.client = TestClient(web.app)

    def setUp(self):
        reset()

    def test_layouts_tile_the_page_in_reading_order(self):
        for count, slots in planner_schema.MANGA_LAYOUTS.items():
            self.assertEqual(len(slots), count)
            self.assertAlmostEqual(sum(w * h for _, _, w, h in slots), 1.0, places=6, msg=count)
            for a, (x1, y1, w1, h1) in enumerate(slots):
                self.assertTrue(0 <= x1 and x1 + w1 <= 1 + 1e-9 and 0 <= y1 and y1 + h1 <= 1 + 1e-9)
                for x2, y2, w2, h2 in slots[a + 1:]:
                    overlap = max(0, min(x1 + w1, x2 + w2) - max(x1, x2)) * max(0, min(y1 + h1, y2 + h2) - max(y1, y2))
                    self.assertLess(overlap, 1e-9, f'{count} 格版式有重叠')
            # 日漫阅读顺序：同一行里先右后左
            first, second = slots[0], slots[1]
            if abs(first[1] - second[1]) < 1e-9:
                self.assertGreater(first[0], second[0])
            self.assertEqual(len({planner_schema.slot_shape(s) for s in slots}) > 1, True, '版式里格子形状要有变化')

    def test_schema_writes_story_first_and_caps_rating(self):
        schema = planner_schema.manga_schema(6, None, 'suggestive')
        self.assertEqual(list(schema['properties']), ['story', 'characters', 'panels'])
        self.assertEqual(schema['required'], ['story', 'characters', 'panels'])
        panels = schema['properties']['panels']
        self.assertEqual((panels['minItems'], panels['maxItems']), (6, 6))
        self.assertEqual(panels['items']['properties']['rating']['enum'], ['general', 'suggestive'])
        self.assertNotIn('size', panels['items']['properties'], '尺寸由格子决定')
        prompt = planner_schema.manga_system_prompt(None, 6, 'explicit')
        self.assertIn('第 6 格', prompt)
        self.assertIn('不要每一格都是 explicit', prompt)
        self.assertNotIn('{', prompt)

    def test_harness_clamps_rating_and_sizes_from_slots_and_is_idempotent(self):
        target = director.target_for('anima-base-1.0', [])
        plan = manga_plan(6, rating='explicit', beat='nonsense', characters=[0, 5])
        variants, story, notes = planner_schema.validate_manga(plan, 6, target, 'suggestive')
        self.assertEqual(story['story']['title'], '停电')
        self.assertEqual([v['panel']['rating'] for v in variants], ['suggestive'] * 6)
        self.assertEqual(variants[0]['panel']['beat'], 'build')
        self.assertEqual(variants[0]['panel']['characters'], [0], '越界的角色下标丢掉')
        layout = planner_schema.manga_layout(6)
        self.assertEqual([v['size'] for v in variants], [slot['shape'] for slot in layout])
        self.assertTrue(any('超过上限' in n for n in notes))
        again, _, notes2 = planner_schema.validate_manga(
            {**story, 'panels': [planner_schema.panel_input(v) for v in variants]}, 6, target, 'suggestive')
        self.assertEqual(again, variants)
        self.assertEqual(notes2, [])
        with self.assertRaises(ValueError):
            planner_schema.validate_manga(manga_plan(3), None, target, 'explicit')

    def test_params_default_count_and_reject_pose_and_page_loras(self):
        built, errors = params.director_params('电梯', None, 'explicit', 'anima-base-1.0', preset='manga')
        self.assertEqual((errors, built['count'], built['preset']), ([], 6, 'manga'))
        _, errors = params.director_params('电梯', 3, preset='manga')
        self.assertTrue(errors)
        _, errors = params.director_params('电梯', None, 'explicit', 'anima-base-1.0',
                                           [{'id': 'hentai-comic-color'}], preset='manga')
        self.assertTrue(any('漫画生成器' in e for e in errors))
        _, errors = params.director_params('电梯', None, 'explicit', 'pony-realism-2.2',
                                           [{'id': 'mating-press'}], preset='manga')
        self.assertTrue(any('体位' in e for e in errors))
        _, errors = params.director_params('电梯', preset='comic')
        self.assertTrue(errors)
        plain = director.build('电梯', 6, 'explicit', 'anima-base-1.0')
        self.assertNotIn('preset', plain, '普通请求形状不变')
        manga = director.build('电梯', 6, 'explicit', 'anima-base-1.0', preset='manga')
        self.assertEqual(manga['ratingTags'], {'general': ['safe'], 'suggestive': ['sensitive'],
                                               'explicit': ['explicit']})
        self.assertNotEqual(plain['key'], manga['key'])

    def submit(self, review=True):
        r = self.client.post('/api/v1/director', json={
            'brief': '被困电梯的两个同事', 'model': 'anima-base-1.0', 'preset': 'manga',
            'rating': 'explicit', 'review': review})
        self.assertEqual(r.status_code, 202, r.text)
        return r.json()['id']

    def accept(self, job_id, plan):
        row = db.job(job_id)
        planning = row['request']
        variants, story, notes = planner_schema.validate_manga(
            plan, planning['count'], planning['target'], planning['rating'])
        db.update_job(job_id, state='running', call_id='fc-plan')
        return pipeline._accept_plan(db.job(job_id), {
            **story, 'variants': variants, 'notes': notes, 'raw': '{}', 'model': 'test',
            'plannerRevision': director.revision()})

    def test_panels_get_their_own_rating_tags_and_review_edits_merge_by_position(self):
        job_id = self.submit()
        plan = manga_plan(6)
        plan['panels'][5] = dict(MANGA_PANEL, beat='climax', shot='close_up', rating='explicit')
        self.assertEqual(self.accept(job_id, plan), 'review')
        page = self.client.get('/jobs/' + job_id)
        self.assertIn('确认分镜', page.text)
        self.assertIn('停电', page.text)
        self.assertIn('……停电了？', page.text)
        r = self.client.post(f'/api/v1/jobs/{job_id}/approve', json={'variants': [
            dict(prompt=MANGA_PANEL['prompt'], steps=35, cfg=4.5, seed=7)] * 5})
        self.assertEqual(r.status_code, 422, '不能增删格')
        edits = [dict(prompt=MANGA_PANEL['prompt'], negative='text', steps=35, cfg=4.5, seed=7)
                 for _ in range(6)]
        edits[1].update(rating='suggestive', caption='灯灭了。',
                        dialogue=[{'speaker': '健太', 'text': '别怕'}])
        r = self.client.post(f'/api/v1/jobs/{job_id}/approve', json={'variants': edits})
        self.assertEqual(r.status_code, 200, r.text)
        row = db.job(job_id)
        prompts = [v['prompt'] for v in row['request']['variants']]
        self.assertTrue(prompts[0].startswith('safe, masterpiece'), prompts[0])
        self.assertTrue(prompts[1].startswith('sensitive, masterpiece'), prompts[1])
        self.assertTrue(prompts[5].startswith('explicit, masterpiece'), prompts[5])
        plan = row['params']['plan']
        self.assertEqual(plan['story']['title'], '停电')
        self.assertEqual(plan['variants'][1]['panel']['caption'], '灯灭了。')
        self.assertEqual(plan['variants'][5]['panel']['beat'], 'climax', '没给的字段保持原样')
        self.assertEqual(plan['variants'][1]['prompt_defaults']['rating'], 'suggestive')
        sizes = [(v['width'], v['height']) for v in row['request']['variants']]
        self.assertEqual(sizes[0], (1216, 832), '第 1 格是横长格')
        page = self.client.get('/jobs/' + job_id)
        self.assertIn('灯灭了。', page.text)

    def test_form_submits_preset_and_count(self):
        page = self.client.get('/apps/director')
        self.assertIn('value="manga"', page.text)
        form = self.client.post('/apps/director/jobs', data={
            'brief': '被困电梯的两个同事', 'model': 'anima-base-1.0', 'preset': 'manga', 'count': '8',
            'rating': 'explicit'}, follow_redirects=False)
        self.assertEqual(form.status_code, 303, form.text)
        row = db.job(form.headers['location'].rsplit('/', 1)[1])
        self.assertEqual((row['request']['preset'], row['request']['count']), ('manga', 8))
        plain = self.client.post('/apps/director/jobs', data={'brief': '一个指南针', 'preset': ''},
                                 follow_redirects=False)
        self.assertEqual(plain.status_code, 303)
        self.assertIn('manga', self.client.get('/api/v1/params').json()['director']['presets'])

    def test_container_request_validation_accepts_manga(self):
        import aladin_planner_modal_app as app_module
        request = director.build('电梯', 6, 'explicit', 'anima-base-1.0', preset='manga')
        cleaned = app_module.validate_request(request)
        self.assertEqual((cleaned['preset'], cleaned['rating'], cleaned['count']), ('manga', 'explicit', 6))
        with self.assertRaises(ValueError):
            app_module.validate_request(dict(request, count=None))
