"""验证 watchdog 的节点映射与消息过滤：纯函数，不需要网络或容器。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import watchdog

TARGET = 'prompt-target'
FOREIGN = 'prompt-other'
MAPPING = {'sampler0': (1, 1), 'sampler1': (2, 2)}


def progress(value, maximum, node, prompt_id=TARGET):
    """复刻 ComfyUI hijack_progress 发出的 progress 消息形状。"""
    return {'type': 'progress',
            'data': {'value': value, 'max': maximum, 'prompt_id': prompt_id, 'node': node}}


class WatchdogTest(unittest.TestCase):
    def test_ws_url_converts_scheme(self):
        # 容器实测：WebSocketApp 传 http 会报 "scheme http is invalid"。
        self.assertEqual(watchdog.ws_url('http://127.0.0.1:8188'), 'ws://127.0.0.1:8188')
        self.assertEqual(watchdog.ws_url('https://example.com'), 'wss://example.com')
        self.assertEqual(watchdog.ws_url('ws://already'), 'ws://already')

    def test_classify_orders_samplers_numerically_not_lexically(self):
        workflow = {
            'sampler10': {'class_type': 'KSampler'},
            'sampler2': {'class_type': 'KSampler'},
            'sampler1': {'class_type': 'KSampler'},
            'decode1': {'class_type': 'VAEDecode'},
            'model': {'class_type': 'UnetLoaderGGUF'},
        }
        self.assertEqual(watchdog.classify(workflow),
                         {'sampler1': (1, 3), 'sampler2': (2, 3), 'sampler10': (3, 3)})

    def test_progress_line_matches_comfyui_shape(self):
        line = watchdog.format_progress(progress(12, 30, 'sampler1'), TARGET, MAPPING)
        self.assertEqual(line, f'{watchdog.PREFIX} image=2/2 step=12/30 node=sampler1')

    def test_foreign_prompt_is_filtered(self):
        self.assertIsNone(
            watchdog.format_progress(progress(12, 30, 'sampler1', FOREIGN), TARGET, MAPPING),
            '广播里属于其他任务的消息必须被过滤')

    def test_non_progress_events_are_ignored(self):
        for event in ({'type': 'executing', 'data': {'node': 'sampler0', 'prompt_id': TARGET}},
                      {'type': 'status', 'data': {}},
                      {'type': 'executed', 'data': {'prompt_id': TARGET}}):
            self.assertIsNone(watchdog.format_progress(event, TARGET, MAPPING))

    def test_unknown_node_is_ignored(self):
        self.assertIsNone(watchdog.format_progress(progress(1, 30, 'vae'), TARGET, MAPPING))

    def test_malformed_input_does_not_raise(self):
        for event in (None, [], 'nonsense', {'type': 'progress'}, {'type': 'progress', 'data': None}):
            self.assertIsNone(watchdog.format_progress(event, TARGET, MAPPING))


if __name__ == '__main__':
    unittest.main(verbosity=2)
