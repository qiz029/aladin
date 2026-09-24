"""容器侧（aladin/worker.py）的本地测试：请求归一、逐条校验、workflow 形状。

worker 只依赖标准库，能在本地直接跑——容器里的行为不该只能靠"烧一次 GPU"来验证。
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import request as request_module  # noqa: E402
from aladin import worker  # noqa: E402

V1 = {'prompt': 'one', 'negative': 'blurry', 'width': 1024, 'height': 1024,
      'steps': 25, 'cfg': 1.0, 'seed': 11}
V2 = {'prompt': 'two', 'negative': '', 'width': 832, 'height': 1216,
      'steps': 30, 'cfg': 1.5, 'seed': 12}


def _batch(variants):
    return request_module.build_variants('a' * 64, variants, 'euler', 'simple')


class VariantTest(unittest.TestCase):
    def test_generation_key_changes_with_model_or_worker_revision(self):
        from unittest.mock import patch
        args = dict(prompt='compass', images=1, seed=1, width=1024, height=1024,
                    steps=25, cfg=1.0, sampler='euler', scheduler='simple')
        original = request_module.storage_key(**args)
        with patch.object(request_module, 'worker_revision', return_value='new-worker'):
            self.assertNotEqual(original, request_module.storage_key(**args))
        with patch.dict(request_module.MODELS['qwen-image-2.1'], transformer='new-quant.gguf'):
            self.assertNotEqual(original, request_module.storage_key(**args))

    def test_legacy_shape_keeps_incrementing_seeds(self):
        """老的 prompts[] 形状：种子仍按序号递增，尺度仍是共享的。"""
        legacy = request_module.build('b' * 64, 'a teapot', 3, 7, 1024, 1024, 25, 1.0,
                                      'euler', 'simple')
        normalized = worker.variants_of(legacy)
        self.assertEqual([item['seed'] for item in normalized], [7, 8, 9])
        self.assertEqual([item['prompt'] for item in normalized], ['a teapot'] * 3)
        self.assertEqual({item['width'] for item in normalized}, {1024})
        self.assertTrue(worker.validate(legacy))

    def test_validate_accepts_per_image_params(self):
        cleaned = worker.validate(_batch([V1, V2]))
        self.assertEqual(len(worker.variants_of(cleaned)), 2)

    def test_validate_rejects_out_of_range_variant(self):
        for field, value in (('steps', 41), ('steps', 0), ('cfg', 11.0),
                             ('width', 1544), ('width', 1020), ('seed', -1),
                             ('prompt', '   ')):
            with self.assertRaises(ValueError, msg=f'{field}={value!r} 应当被拒'):
                worker.validate(_batch([V1, dict(V2, **{field: value})]))

    def test_validate_rejects_empty_batch(self):
        with self.assertRaises(ValueError):
            worker.validate(_batch([]))

    def test_workflow_uses_each_variant(self):
        graph = worker.workflow(_batch([V1, V2]))
        self.assertEqual(graph['latent0']['inputs'],
                         {'width': 1024, 'height': 1024, 'batch_size': 1})
        self.assertEqual(graph['latent1']['inputs']['width'], 832)
        self.assertEqual(graph['sampler1']['inputs']['steps'], 30)
        self.assertEqual(graph['sampler1']['inputs']['cfg'], 1.5)
        self.assertEqual(graph['sampler0']['inputs']['seed'], 11)
        self.assertEqual(graph['text1']['inputs']['text'], 'two')
        self.assertEqual(graph['negtext0']['inputs']['text'], 'blurry')
        self.assertEqual(graph['negtext1']['inputs']['text'], '')
        # 负向改成逐图编码，不再共用全局 negative 节点
        self.assertNotIn('negative', graph)

    def test_save_nodes_follow_the_batch(self):
        self.assertEqual(worker.save_nodes(_batch([V1, V2])), ['save0', 'save1'])
        legacy = request_module.build('c' * 64, 'a teapot', 2, 7, 1024, 1024, 25, 1.0,
                                      'euler', 'simple')
        self.assertEqual(worker.save_nodes(legacy), ['save0', 'save1'])


if __name__ == '__main__':
    unittest.main(verbosity=2)


class EarlyPublisherTest(unittest.TestCase):
    """逐张回传：只认本任务、本批 SaveImage 的 executed；先提交 Volume 再通知宿主。"""

    def _publisher(self, commit=True):
        self.calls = []
        def store(index, item):
            self.calls.append(('store', index, item['filename']))
            return {'index': index + 1, 'file': f'image-0{index + 1}.png', 'sha256': 'f' * 64,
                    'bytes': 3, 'seed': 7, 'prompt': 'p', 'params': {}}
        return worker.EarlyPublisher('prompt-1', ['save0', 'save1'], store,
                                     (lambda: self.calls.append(('commit',))) if commit else None)

    @staticmethod
    def _executed(node, prompt_id='prompt-1'):
        return {'type': 'executed', 'data': {'node': node, 'prompt_id': prompt_id,
                                             'output': {'images': [{'filename': node + '.png'}]}}}

    def test_publishes_each_image_after_commit(self):
        import io
        from contextlib import redirect_stdout
        publisher = self._publisher()
        out = io.StringIO()
        with redirect_stdout(out):
            publisher.on_event(self._executed('save1'))
            publisher.on_event(self._executed('save0', prompt_id='other'))   # 别的任务
            publisher.on_event({'type': 'executing', 'data': {'node': 'save0'}})
            publisher.close()
        self.assertEqual(self.calls, [('store', 1, 'save1.png'), ('commit',)])
        self.assertEqual(list(publisher.done), [1])
        line = [l for l in out.getvalue().splitlines() if l.startswith(worker.PREFIX)][0]
        self.assertIn('"kind": "artifact"', line)
        self.assertIn('"total": 2', line)

    def test_without_commit_falls_back_to_batch_download(self):
        publisher = self._publisher(commit=False)
        publisher.on_event(self._executed('save0'))
        publisher.close()
        self.assertEqual(self.calls, [])
        self.assertEqual(publisher.done, {})

    def test_store_failure_is_not_fatal(self):
        publisher = worker.EarlyPublisher('prompt-1', ['save0'],
                                          lambda *a: (_ for _ in ()).throw(OSError('boom')),
                                          lambda: None)
        publisher.on_event(self._executed('save0'))
        publisher.close()
        self.assertEqual(publisher.done, {}, '失败的那张留给整批结束后的正常流程')
