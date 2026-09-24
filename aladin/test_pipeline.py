"""编排层的设计不变量（ADR-0002）。

重点测三件容易做错的事：
1. enqueue 不碰 Modal（只有 worker 提交）
2. 有 call_id 时绝不重新 spawn
3. unknown 先查回执再决定重试
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix='aladin-pipe-')
os.environ['ALADIN_DATA'] = _TMP
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aladin.testing import configure, reset  # noqa: E402

configure()

from aladin import db, pipeline, settings  # noqa: E402
import request  # noqa: E402
import modal.exception as modal_exception  # noqa: E402

PARAMS = {'images': 1, 'seed': 1, 'width': 1024, 'height': 1024, 'steps': 8,
          'cfg': 1.0, 'sampler': 'euler', 'scheduler': 'simple', 'negative': ''}
# 视频请求用的参数字典（web/api 校验后就是这个形状）
VIDEO_PARAMS = {'negative': '', 'duration': 'short', 'size': 'landscape',
                'width': 832, 'height': 480, 'frames': 56, 'fps': 24, 'seconds': 2.33,
                'steps': 4, 'cfg': 1.0, 'shift': 8.0, 'sampler': 'euler',
                'scheduler': 'simple', 'seed': 0, 'loraStrength': 1.0, 'images': 1}


class OutputExpiredError(Exception):
    """模块级定义：每次 fake_modal() 都新建类的话，抛出的实例与捕获的类对不上。"""


class FakeCall:
    def __init__(self, outcome):
        self.outcome = outcome          # 结果 dict 或要抛出的异常
        self.logs = types.SimpleNamespace(stream=lambda timeout=None: iter(()))

    def get(self, timeout=None):
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


def fake_modal(call=None, receipt=False, fail_on_spawn=True):
    """构造一个只会被 from_id 使用的 modal 替身。"""
    def from_id(call_id):
        return call

    def spawn(*args, **kwargs):
        raise AssertionError('不应调用 spawn：有 call_id 的任务只能接管')

    return types.SimpleNamespace(
        FunctionCall=types.SimpleNamespace(from_id=from_id),
        Function=types.SimpleNamespace(from_name=lambda *a: types.SimpleNamespace(
            spawn=spawn if fail_on_spawn else spawn)),
        exception=types.SimpleNamespace(
            OutputExpiredError=OutputExpiredError,
            DeserializationError=modal_exception.DeserializationError,
            ConnectionError=modal_exception.ConnectionError,
            ClientClosed=modal_exception.ClientClosed,
            NotFoundError=modal_exception.NotFoundError),
        Volume=types.SimpleNamespace(from_name=lambda *a: None),
    )


class PipelineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.init()

    def setUp(self):
        reset()
        self.originals = (pipeline.modal, pipeline._download, pipeline._watch,
                          pipeline._receipt_exists)
        pipeline._download = lambda *a, **k: None
        pipeline._watch = lambda *a, **k: None
        pipeline._receipt_exists = lambda *a, **k: False

    def tearDown(self):
        (pipeline.modal, pipeline._download, pipeline._watch,
         pipeline._receipt_exists) = self.originals

    def _job(self, state='pending', **fields):
        job_id = pipeline.enqueue('a teapot', 1, dict(PARAMS))
        if state != 'pending' or fields:
            db.update_job(job_id, state=state, **fields)
        return job_id

    # --- 视频切片路由 ------------------------------------------------------

    def test_additional_models_submit_to_their_own_apps(self):
        from unittest.mock import MagicMock, patch
        from aladin.params import image_params
        for model, app in [('anima-base-1.0', 'aladin-anima-image-v1'),
                           ('pony-realism-2.2', 'aladin-pony-image-v1')]:
            params, errors = image_params('a teapot', 1, None, None, None, None, None, None, 0, model)
            self.assertFalse(errors)
            pipeline.enqueue('a teapot', 1, params)
            function = MagicMock()
            function.spawn.return_value.object_id = 'fc-test'
            with patch.object(pipeline.modal.Function, 'from_name', return_value=function) as lookup:
                self.assertTrue(pipeline._submit_one())
                lookup.assert_called_once_with(app, 'generate')
                self.assertEqual(function.spawn.call_args.args[0]['modelId'], model)

    def test_target_routes_each_app_to_its_own_modal_app_and_volume(self):
        self.assertEqual(pipeline._target('video')[:2],
                         (settings.APP_VIDEO, settings.FUNCTION_VIDEO))
        self.assertEqual(pipeline._target('video')[2:],
                         (settings.VOLUME_RESULTS_VIDEO, 'videos'))
        self.assertEqual(pipeline._target('image')[:2],
                         (settings.APP_IMAGE, settings.FUNCTION_IMAGE))
        self.assertEqual(pipeline._target('image')[3], 'images')
        # 未知/空 app 落回图片：历史数据不该把 worker 卡住
        self.assertEqual(pipeline._target('')[:2],
                         (settings.APP_IMAGE, settings.FUNCTION_IMAGE))

    def test_enqueue_video_builds_an_h3_request(self):
        job_id = pipeline.enqueue('clouds drift', 1, dict(VIDEO_PARAMS), mode='i2v',
                                  input_sha256='b' * 64, input_path='uploads/x.png',
                                  app='video')
        row = db.job(job_id)
        self.assertEqual((row['app'], row['mode']), ('video', 'i2v'))
        self.assertEqual(row['request']['model'], '10eros-max-h3-turbo-beta5')
        self.assertEqual(row['request']['frames'], 56)
        self.assertEqual(row['request']['width'], 832)
        self.assertEqual(row['image_count'], 1, '账本只有一列 image_count，视频记 1')

    def test_video_download_reads_the_video_volume_and_records_the_artifact(self):
        job_id = pipeline.enqueue('clouds drift', 1, dict(VIDEO_PARAMS), mode='i2v',
                                  input_sha256='b' * 64, input_path='uploads/x.png',
                                  app='video')
        row = db.job(job_id)
        manifest = json.dumps({'videos': [
            {'file': 'video-01.webm', 'sha256': 'c' * 64, 'bytes': 4, 'seed': 0,
             'prompt': 'clouds drift'}]})
        files = {row['result_key'] + '/result.json': manifest.encode(),
                 row['result_key'] + '/video-01.webm': b'webm'}
        seen = {}

        def from_name(name):
            seen['volume'] = name
            return types.SimpleNamespace(read_file=lambda path: iter([files[path]]))

        pipeline.modal = types.SimpleNamespace(
            Volume=types.SimpleNamespace(from_name=from_name))
        # setUp 把 _download 换成了桩；这里要验的正是它，所以用保存下来的真函数
        self.originals[1](job_id, row['result_key'], {}, 'video')
        self.assertEqual(seen['volume'], settings.VOLUME_RESULTS_VIDEO,
                         '视频产物必须从视频 Volume 拉')
        self.assertTrue((settings.JOBS_DIR / job_id / 'video-01.webm').is_file())
        self.assertEqual([a['name'] for a in db.artifacts(job_id)], ['video-01.webm'])

    # --- enqueue -----------------------------------------------------------

    def test_enqueue_does_not_touch_modal(self):
        pipeline.modal = types.SimpleNamespace()   # 任何属性访问都会 AttributeError
        job_id = pipeline.enqueue('a teapot', 1, dict(PARAMS))
        row = db.job(job_id)
        self.assertEqual(row['state'], 'pending')
        self.assertIsNone(row['call_id'], 'api 不应提交任务')

    def test_same_parameters_are_rejected_by_unique_key(self):
        pipeline.enqueue('a teapot', 1, dict(PARAMS))
        with self.assertRaises(Exception):
            pipeline.enqueue('a teapot', 1, dict(PARAMS))

    # --- 轮询 ---------------------------------------------------------------

    def test_poll_with_result_marks_succeeded(self):
        job_id = self._job(state='submitted', call_id='fc-1')
        pipeline.modal = fake_modal(call=FakeCall({'key': 'k'}))

        self.assertEqual(pipeline._poll_row(db.job(job_id)), 'succeeded')
        self.assertEqual(db.job(job_id)['state'], 'succeeded')

    def test_poll_while_running_reschedules_with_backoff(self):
        job_id = self._job(state='submitted', call_id='fc-1', attempts=1)
        pipeline.modal = fake_modal(call=FakeCall(TimeoutError()))

        self.assertEqual(pipeline._poll_row(db.job(job_id)), 'running')
        row = db.job(job_id)
        self.assertEqual(row['state'], 'running', '首次轮询未完成应转入 running')
        self.assertIsNotNone(row['next_poll_at'], '必须安排下次轮询')

    def test_poll_never_spawns(self):
        """核心不变量：有 call_id 就只能接管。fake_modal 的 spawn 会抛异常。"""
        job_id = self._job(state='running', call_id='fc-1')
        pipeline.modal = fake_modal(call=FakeCall(TimeoutError()))
        pipeline._poll_row(db.job(job_id))       # 抛异常即测试失败
        self.assertEqual(db.job(job_id)['state'], 'running')

    def test_expired_call_becomes_unknown(self):
        job_id = self._job(state='running', call_id='fc-1')
        expired = OutputExpiredError('gone')
        pipeline.modal = fake_modal(call=FakeCall(expired))

        self.assertEqual(pipeline._poll_row(db.job(job_id)), 'unknown')
        self.assertEqual(db.job(job_id)['state'], 'unknown')

    def test_container_error_fails_without_retry(self):
        """容器内异常是确定性的：重试只会撞同一个错误。"""
        job_id = self._job(state='running', call_id='fc-1', attempts=1)
        pipeline.modal = fake_modal(call=FakeCall(RuntimeError('Worker revision mismatch')))

        self.assertEqual(pipeline._poll_row(db.job(job_id)), 'failed')
        self.assertIn('revision mismatch', db.job(job_id)['last_error'])

    def test_transient_poll_error_keeps_job_running(self):
        """查不到 ≠ 失败：连接抖动时容器可能还在跑，不能判死也不能重新 spawn。"""
        job_id = self._job(state='running', call_id='fc-1', attempts=1)
        pipeline.modal = fake_modal(call=FakeCall(modal_exception.ConnectionError('reset')))

        self.assertEqual(pipeline._poll_row(db.job(job_id)), 'running')
        row = db.job(job_id)
        self.assertEqual(row['state'], 'running')
        self.assertEqual(row['call_id'], 'fc-1')
        self.assertGreater(row['next_poll_at'], datetime.now(timezone.utc))

    def test_container_connection_error_still_fails(self):
        """容器内的内置 ConnectionError（例如连不上 ComfyUI）是确定性的。"""
        job_id = self._job(state='running', call_id='fc-1', attempts=1)
        pipeline.modal = fake_modal(call=FakeCall(ConnectionRefusedError('comfy down')))

        self.assertEqual(pipeline._poll_row(db.job(job_id)), 'failed')

    def test_undeserializable_result_falls_back_to_volume_manifest(self):
        job_id = self._job(state='running', call_id='fc-1', attempts=1)
        pipeline.modal = fake_modal(
            call=FakeCall(modal_exception.DeserializationError('bad pickle')))

        self.assertEqual(pipeline._poll_row(db.job(job_id)), 'succeeded')

    def test_retry_progress_is_not_swallowed_by_previous_attempt(self):
        """重试是新的一次调用：它的第 1 步不能被上一次调用的第 1 步去重掉。"""
        job_id = self._job(state='running', call_id='fc-1')
        line = '[aladin-progress] {"kind":"step","image":1,"total":1,"step":1,"max":20,"node":"3"}'
        for call_id in ('fc-1', 'fc-1', 'fc-2'):
            call = FakeCall({})
            call.object_id = call_id
            call.logs = types.SimpleNamespace(
                stream=lambda timeout=None: iter([types.SimpleNamespace(message=line)]))
            pipeline._watch_loop(job_id, call)
        steps = [e for e in db.events(job_id) if e['kind'] == 'progress']
        self.assertEqual(len(steps), 2, '同一调用的重放去重，新调用照常记录')

    def test_early_artifact_is_fetched_verified_and_replay_safe(self):
        import hashlib
        job_id = self._job(state='running', call_id='fc-1')
        data = b'png-bytes'
        reads = []
        original = pipeline._read_bytes
        pipeline._read_bytes = lambda volume, path: reads.append(path) or data
        pipeline.modal = fake_modal()
        try:
            info = {'kind': 'artifact', 'index': 1, 'total': 2, 'file': 'image-01.png',
                    'sha256': hashlib.sha256(data).hexdigest(), 'seed': 9, 'prompt': 'p',
                    'params': {'seed': 9}}
            self.assertTrue(pipeline._fetch_early(job_id, info))
            self.assertFalse(pipeline._fetch_early(job_id, info), '重放不重复下载')
            self.assertEqual(len(reads), 1)
            item = db.artifact(job_id, 'image-01.png')
            self.assertEqual((item['seed'], item['params']), (9, {'seed': 9}))
            self.assertFalse(pipeline._fetch_early(job_id, dict(info, file='image-02.png',
                                                                sha256='0' * 64)),
                             'sha256 对不上（Volume 还没提交完）不落地')
            self.assertIsNone(db.artifact(job_id, 'image-02.png'))
            self.assertFalse(pipeline._fetch_early(job_id, dict(info, file='../x.png')))
        finally:
            pipeline._read_bytes = original

    def test_deleted_jobs_are_purged_from_modal_volumes(self):
        from aladin import cleanup, director
        image = self._job(state='succeeded')
        video = db.create_job('v', {'images': 1, 'seed': 1}, {'key': 'v'}, 'b' * 64,
                              mode='i2v', app='video', input_sha256='c' * 64,
                              input_path='uploads/x.png')
        db.update_job(video, state='failed')
        for job_id in (image, video):
            self.assertTrue(cleanup.delete_job(job_id))
        with db.connect() as connection:
            queued = {(r['volume'], r['path']) for r in connection.execute('SELECT * FROM remote_purge')}
        self.assertIn((settings.VOLUME_RESULTS_VIDEO, 'b' * 64), queued)
        self.assertEqual({v for v, _ in queued}, {settings.VOLUME_RESULTS, settings.VOLUME_RESULTS_VIDEO})
        self.assertEqual(cleanup.remote_volumes({'app': 'image', 'mode': 'director'}),
                         [settings.VOLUME_RESULTS, director.RESULTS_VOLUME])

        removed, outcomes = [], iter([None, modal_exception.NotFoundError('gone')])
        def remove_file(path, recursive=False):
            removed.append((path, recursive))
            outcome = next(outcomes)
            if outcome:
                raise outcome
        pipeline.modal = fake_modal()
        pipeline.modal.Volume = types.SimpleNamespace(
            from_name=lambda name: types.SimpleNamespace(remove_file=remove_file))
        self.assertEqual(pipeline._purge_remote(), 2, 'NotFound 也算清理完成')
        self.assertTrue(all(recursive for _, recursive in removed))
        with db.connect() as connection:
            self.assertEqual(connection.execute('SELECT count(*) AS n FROM remote_purge').fetchone()['n'], 0)

    def test_failed_purge_is_retried_later(self):
        from aladin import cleanup
        job_id = self._job(state='succeeded')
        cleanup.delete_job(job_id)
        pipeline.modal = fake_modal()
        pipeline.modal.Volume = types.SimpleNamespace(from_name=lambda name: types.SimpleNamespace(
            remove_file=lambda *a, **k: (_ for _ in ()).throw(RuntimeError('network'))))
        self.assertEqual(pipeline._purge_remote(), 0)
        with db.connect() as connection:
            row = connection.execute('SELECT * FROM remote_purge').fetchone()
        self.assertEqual(row['attempts'], 1)
        self.assertIn('network', row['last_error'])

    # --- unknown ------------------------------------------------------------

    def test_unknown_with_receipt_succeeds_without_gpu(self):
        job_id = self._job(state='unknown', attempts=2, last_error='调用记录已过期')
        pipeline._receipt_exists = lambda *a, **k: True

        self.assertTrue(pipeline._resolve_unknown_one())
        self.assertEqual(db.job(job_id)['state'], 'succeeded')

    def test_unknown_without_receipt_goes_back_to_pending(self):
        job_id = self._job(state='unknown', attempts=1, last_error='过期')
        pipeline._receipt_exists = lambda *a, **k: False

        self.assertTrue(pipeline._resolve_unknown_one())
        row = db.job(job_id)
        self.assertEqual(row['state'], 'pending', '还有尝试次数时应回到 pending')
        self.assertIsNone(row['call_id'], '重试必须清掉旧 call_id')

    def test_unknown_exhausted_retries_fails(self):
        job_id = self._job(state='unknown', attempts=2, last_error='过期')
        pipeline._receipt_exists = lambda *a, **k: False

        self.assertTrue(pipeline._resolve_unknown_one())
        row = db.job(job_id)
        self.assertEqual(row['state'], 'failed')
        self.assertIn('重试次数已用尽', row['last_error'])

    # --- 中断恢复 ------------------------------------------------------------

    def test_interrupted_submission_becomes_unknown(self):
        job_id = self._job(state='submitting')
        db.update_job(job_id, lease_owner='dead-worker',
                      lease_expires_at=datetime.now(timezone.utc) - timedelta(minutes=5))

        self.assertEqual(pipeline._recover_interrupted(), 1)
        self.assertEqual(db.job(job_id)['state'], 'unknown')

    def test_active_lease_is_not_recovered(self):
        job_id = self._job(state='submitting')
        db.update_job(job_id, lease_owner='live-worker',
                      lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5))

        self.assertEqual(pipeline._recover_interrupted(), 0)
        self.assertEqual(db.job(job_id)['state'], 'submitting')

    # --- 提交 ---------------------------------------------------------------

    def test_spawn_failure_is_retryable_and_attempts_are_bounded(self):
        """spawn 失败通常是暂时的，应走 unknown 重试；且尝试次数必须真的增长。"""
        job_id = self._job(state='pending')

        def boom(*args, **kwargs):
            raise RuntimeError('network down')

        pipeline.modal = types.SimpleNamespace(
            Function=types.SimpleNamespace(
                from_name=lambda *a: types.SimpleNamespace(spawn=boom)))

        self.assertTrue(pipeline._submit_one())
        row = db.job(job_id)
        self.assertEqual(row['state'], 'unknown', 'spawn 失败不应直接判死')
        self.assertEqual(row['attempts'], 1, '尝试次数应在认领时记下')

        # 第二次尝试后到达上限，最终应失败而不是无限重试
        db.update_job(job_id, state='unknown')
        pipeline._resolve_unknown_one()          # attempts 1 < 2 -> 回 pending
        self.assertEqual(db.job(job_id)['state'], 'pending')
        pipeline._submit_one()                   # attempts -> 2
        pipeline._resolve_unknown_one()          # 2 >= 2 -> failed
        self.assertEqual(db.job(job_id)['state'], 'failed')

    def test_spawn_failure_does_not_loop_forever(self):
        """attempts 若只在成功后自增，这个测试会因为状态停在 pending 而失败。"""
        job_id = self._job(state='pending')
        pipeline.modal = types.SimpleNamespace(
            Function=types.SimpleNamespace(from_name=lambda *a: types.SimpleNamespace(
                spawn=lambda *a, **k: (_ for _ in ()).throw(RuntimeError('down')))))

        for _ in range(6):
            pipeline._submit_one()
            pipeline._resolve_unknown_one()
            if db.job(job_id)['state'] in ('failed', 'succeeded'):
                break
        self.assertEqual(db.job(job_id)['state'], 'failed')
        self.assertLessEqual(db.job(job_id)['attempts'], db.job(job_id)['max_attempts'])

    # --- 事件去重 ------------------------------------------------------------

    def test_progress_replay_is_harmless(self):
        job_id = self._job()
        self.assertTrue(db.add_event(job_id, 'progress', '{}', dedupe_key='step:1:3'))
        self.assertFalse(db.add_event(job_id, 'progress', '{}', dedupe_key='step:1:3'),
                         '重开日志流重放旧行时不应重复写入')
        self.assertEqual(len(db.events(job_id)), 1)




class RequestContractTest(unittest.TestCase):
    """请求与容器 validate() 的契约。

    少一个字段会让**所有**任务瞬间失败（不是某一种模式），所以这里逐项钉住。
    """

    FIELDS = ('workerRevision', 'comfyRevision', 'ggufRevision', 'mode',
              'model', 'modelRevision', 'quantization', 'key',
              'prompts', 'negative', 'width', 'height', 'seed',
              'steps', 'cfg', 'sampler', 'scheduler', 'denoise',
              'inputSha256')

    def test_build_carries_every_field_the_container_checks(self):
        built = request.build('a' * 64, 'x', 1, 0, 1024, 1024, 20, 1.0,
                              'euler', 'simple')
        self.assertEqual([f for f in self.FIELDS if f not in built], [])

    def test_pinned_revisions_match_the_generated_worker(self):
        """镜像换了 ComfyUI/GGUF 提交号时，worker.py 与 request.py 必须同步，
        否则容器会以 revision mismatch 拒绝每一个请求。"""
        source = (Path(__file__).resolve().parent / 'worker.py').read_text()
        quote = chr(39)
        self.assertIn('COMFY_REVISION = ' + quote + request.COMFY_REVISION + quote, source)
        self.assertIn('GGUF_REVISION = ' + quote + request.GGUF_REVISION + quote, source)

    def test_modes_map_to_pinned_models(self):
        for mode in request.MODES:
            spec = request.model_config(mode)
            self.assertRegex(spec['revision'], '[a-f0-9]{40}')
            self.assertTrue(spec['transformer'].endswith('.gguf'))
if __name__ == '__main__':
    unittest.main(verbosity=2)
