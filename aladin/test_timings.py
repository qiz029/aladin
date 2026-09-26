"""耗时报表：宿主事件 + 容器 timings 拼成的分段要算对，缺数据的老任务要能照常出表。"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

os.environ.setdefault('ALADIN_DATA', tempfile.mkdtemp(prefix='aladin-timings-'))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aladin.testing import configure, reset  # noqa: E402

configure()

from psycopg.types.json import Jsonb  # noqa: E402

from aladin import db, pipeline, timings  # noqa: E402

T0 = 1_790_000_000.0


def at(seconds: float) -> datetime:
    return datetime.fromtimestamp(T0 + seconds, timezone.utc)


def job(**fields) -> dict:
    base = {'id': 'abcdef0123', 'app': 'image', 'mode': 'txt2img', 'image_count': 1,
            'params': {'model': 'pony-realism-2.2'}, 'request': {},
            'created_at': at(0), 'finished_at': at(60), 'timings': None}
    return dict(base, **fields)


class BreakdownTest(unittest.TestCase):
    def test_host_and_container_segments(self):
        container = {'callIndex': 0, 'startedAt': T0 + 20, 'finishedAt': T0 + 50,
                     'phases': {'weights': 0.5, 'comfyBoot': 10.0, 'execute': 18.0, 'outputs': 1.0},
                     'nodes': [{'class': 'CheckpointLoaderSimple', 'seconds': 6.0},
                               {'class': 'LoraLoader', 'seconds': 1.0},
                               {'class': 'CLIPTextEncode', 'seconds': 0.5},
                               {'class': 'KSampler', 'seconds': 4.0},
                               {'class': 'VAEDecode', 'seconds': 1.5}]}
        row = timings.breakdown(
            job(timings={'container': container, 'detectedAt': T0 + 52, 'downloadSeconds': 3.0}),
            [{'kind': 'start', 'at': at(1)}, {'kind': 'queued', 'at': at(2)},
             {'kind': 'container', 'at': at(21)}])
        self.assertEqual(row['wait'], 2.0)
        self.assertEqual(row['modal'], 18.0, '提交 → 容器开始')
        self.assertEqual((row['load'], row['sample'], row['decode']), (7.0, 4.0, 1.5))
        self.assertEqual(row['tail'], 2.0, '容器结束 → 宿主发现')
        self.assertEqual(row['reuse'], 'cold')
        self.assertEqual(row['e2e'], 60.0)

    def test_director_splits_planning_from_generation(self):
        row = timings.breakdown(
            job(mode='director', timings={'container': {'callIndex': 2, 'startedAt': T0 + 130}}),
            [{'kind': 'queued', 'at': at(1)}, {'kind': 'planned', 'at': at(100)},
             {'kind': 'queued', 'at': at(110)}])
        self.assertEqual(row['plan'], 99.0)
        self.assertEqual(row['modal'], 20.0, '生成阶段从规划完成后的那次提交算起')
        self.assertEqual(row['reuse'], 'warm2')

    def test_backfilled_job_without_container_timings(self):
        row = timings.breakdown(job(timings={'backfill': True, 'comfyExecuteSeconds': 30.0}),
                                [{'kind': 'queued', 'at': at(1)}])
        self.assertEqual(row['exec'], 30.0)
        self.assertIsNone(row['modal'])
        self.assertEqual(row['reuse'], '')

    def test_node_groups(self):
        self.assertEqual([timings.node_group(c) for c in
                          ('UnetLoaderGGUF', 'SamplerCustomAdvanced', 'VAEDecodeTiled',
                           'VAEEncode', 'SaveImage', None)],
                         ['load', 'sample', 'decode', 'encode', 'other', 'other'])


class ReportTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.init()

    def setUp(self):
        reset()

    def test_report_from_database(self):
        self.assertIn('没有', timings.report())
        job_id = pipeline.enqueue('a teapot', 1, {'images': 1, 'seed': 1, 'width': 1024,
                                                  'height': 1024, 'steps': 8, 'cfg': 1.0,
                                                  'sampler': 'euler', 'scheduler': 'simple',
                                                  'negative': ''})
        db.add_event(job_id, 'queued', 'x')
        db.update_job(job_id, timings=Jsonb({'container': {'callIndex': 0,
                                                           'phases': {'comfyBoot': 11.0}}}))
        db.finish_job(job_id, 'succeeded')
        text = timings.report()
        self.assertIn(job_id[:8], text)
        self.assertIn('cold', text)
        self.assertIn('11/11', text, '汇总里有 boot 的中位数/p90')


if __name__ == '__main__':
    unittest.main()
