"""H3 的跨层参数契约、模型缓存键及音视频图结构回归测试。"""
import unittest
from unittest.mock import patch

import video_request
from aladin import params, settings, video_worker


class H3WorkerTest(unittest.TestCase):
    def request(self, **changes):
        return video_request.build('A blue ceramic teapot; gentle camera move.', 'a' * 64,
                                   **changes)

    def test_all_public_presets_validate_in_worker(self):
        d = settings.VIDEO_DEFAULT_PARAMS
        for duration in settings.VIDEO_DURATIONS:
            for size in settings.VIDEO_SIZES:
                built, errors = params.video_params('teapot', '', duration, size,
                    d['steps'], d['cfg'], d['shift'], d['seed'],
                    d['sampler'], d['scheduler'], d['loraStrength'])
                self.assertEqual(errors, [])
                request = self.request(**built)
                video_worker.validate(request)
                self.assertEqual(built['fps'], video_worker.FPS)
                self.assertEqual((built['frames'] - 5) % 17, 0)

    def test_old_wan_frame_count_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'Frame count'):
            video_worker.validate(self.request(frames=33))

    def test_baked_turbo_rejects_old_lora_control(self):
        with self.assertRaisesRegex(ValueError, 'baked-in'):
            video_worker.validate(self.request(loraStrength=0.5))

    def test_model_switch_cannot_reuse_old_video_receipt_key(self):
        original = self.request()['key']
        with patch.object(video_request, 'worker_revision', return_value='new-worker'):
            self.assertNotEqual(original, self.request()['key'])
        with patch.dict(video_worker.MODELS[video_worker.MODEL], revision='new-revision'):
            self.assertNotEqual(original, self.request()['key'])

    def test_joint_av_workflow_preserves_first_frame_and_audio(self):
        r = dict(self.request(), inputName='source.png')
        g = video_worker.workflow(r)
        self.assertEqual(g['conditioning']['inputs']['first_frame'], ['input', 0])
        self.assertEqual(g['sample']['inputs']['latent_image'], ['conditioning', 1])
        self.assertEqual(g['video']['inputs']['audio'], ['decode_audio', 0])
        self.assertEqual(g['video']['inputs']['fps'], 24)
        self.assertEqual(g['save']['inputs']['format.codec'], 'h264')
        self.assertEqual(g['guider']['class_type'], 'BasicGuider')
        self.assertFalse(any('Lora' in n['class_type'] for n in g.values()))
        self.assertEqual(video_worker.classify(g), {'sample': (1, 1)})

    def test_cfg_branch_uses_negative_and_same_image_condition(self):
        r = dict(self.request(cfg=2, negative='blur'), inputName='source.png')
        g = video_worker.workflow(r)
        self.assertEqual(g['guider']['class_type'], 'CFGGuider')
        self.assertEqual(g['negative']['inputs']['prompt'], 'blur')
        self.assertEqual(g['negative']['inputs']['first_frame'], ['input', 0])
