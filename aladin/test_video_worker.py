"""LTX 视频的跨层参数契约、模型缓存键、LoRA 与两段式音视频图结构回归测试。"""
import unittest
from unittest.mock import patch

import video_request
from aladin import loras, params, settings, video_worker


class LtxWorkerTest(unittest.TestCase):
    def request(self, **changes):
        return video_request.build('A blue ceramic teapot; gentle camera move.', 'a' * 64,
                                   **changes)

    def test_all_public_presets_validate_in_worker_for_both_models(self):
        for model in video_worker.MODELS:
            for duration in settings.VIDEO_DURATIONS:
                for size in settings.VIDEO_SIZES:
                    built, errors = params.video_params('teapot', duration, size, None, model)
                    self.assertEqual(errors, [])
                    request = self.request(**built)
                    video_worker.validate(request, model)
                    self.assertEqual(built['fps'], video_worker.FPS)
                    self.assertEqual((built['frames'] - 1) % 8, 0)
                    # 第一段在半分辨率上跑，仍须是 32 的倍数
                    self.assertEqual(built['width'] // 2 % 32, 0)
                    self.assertEqual(built['height'] // 2 % 32, 0)

    def test_old_h3_frame_count_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'Frame count'):
            video_worker.validate(self.request(frames=124))

    def test_app_refuses_the_other_models_request(self):
        with self.assertRaisesRegex(ValueError, 'serves ltx-2.3'):
            video_worker.validate(self.request(model='ltx-2.5'), 'ltx-2.3')

    def test_model_switch_cannot_reuse_old_video_receipt_key(self):
        original = self.request()['key']
        self.assertNotEqual(original, self.request(model='ltx-2.3')['key'])
        with patch.object(video_request, 'worker_revision', return_value='new-worker'):
            self.assertNotEqual(original, self.request()['key'])
        with patch.dict(video_worker.MODELS['ltx-2.5'], revision='new-revision'):
            self.assertNotEqual(original, self.request()['key'])

    def test_loras_change_the_key_and_reach_the_request(self):
        chosen, errors = loras.resolve([{'id': 'ltx25-cumouf', 'strength': 0.9}], 'ltx-2.5')
        self.assertEqual(errors, [])
        with_lora = self.request(loras=chosen)
        self.assertNotEqual(self.request()['key'], with_lora['key'])
        self.assertEqual(with_lora['loras'], [{'file': 'civitai-3214563.safetensors',
                                               'sha256': chosen[0]['sha256'], 'strength': 0.9}])
        video_worker.validate(with_lora, 'ltx-2.5')

    def test_video_loras_stay_on_their_own_model(self):
        _, errors = loras.resolve([{'id': 'ltx23-dr34ml4y'}], 'ltx-2.5')
        self.assertTrue(errors)
        _, errors = loras.resolve([{'id': 'ltx25-cumouf'}], 'pony-realism-2.2')
        self.assertTrue(errors)

    def test_trigger_words_are_appended_to_the_prompt(self):
        built, errors = params.video_params('she smiles', 'short', 'landscape', 1, 'ltx-2.5',
                                            [{'id': 'ltx25-cumouf'}], 'general')
        self.assertEqual(errors, [])
        self.assertTrue(built['prompt_defaults']['effective'].endswith('CUMOUF'))

    def _graph(self, model, **changes):
        return video_worker.workflow(dict(self.request(model=model, **changes), inputName='source.png'))

    def test_two_stage_av_workflow_for_ltx25(self):
        g = self._graph('ltx-2.5')
        self.assertEqual(g['model']['class_type'], 'UNETLoader')
        self.assertEqual(g['clip']['inputs']['type'], 'ltxv')
        self.assertEqual(g['guider1']['class_type'], 'LTXVDualCFGGuider')
        self.assertEqual(g['latent']['inputs']['width'], 512)
        self.assertEqual(g['upscale']['inputs']['samples'], ['split1', 0])
        self.assertEqual(g['av2']['inputs']['audio_latent'], ['split1', 1])
        self.assertEqual(g['video']['inputs']['audio'], ['decode_audio', 0])
        self.assertEqual(g['video']['inputs']['fps'], 24)
        # SaveVideo 的 codec 是 format 下的动态子输入（容器 object_info 实测）
        self.assertEqual(g['save']['inputs']['format.codec'], 'h264')
        self.assertNotIn('crop_guides', g)
        self.assertEqual(video_worker.classify(g), {'sample1': (1, 2), 'sample2': (2, 2)})
        for node in video_worker.classify(g):
            self.assertEqual(g[node]['class_type'], 'SamplerCustomAdvanced')

    def test_ltx23_uses_checkpoint_distill_lora_and_cropped_guides(self):
        g = self._graph('ltx-2.3')
        self.assertEqual(g['checkpoint']['class_type'], 'CheckpointLoaderSimple')
        self.assertEqual(g['model']['inputs']['strength_model'], 0.5)
        self.assertEqual(g['first_frame1']['inputs']['vae'], ['checkpoint', 2])
        self.assertEqual(g['guider2']['inputs']['positive'], ['crop_guides', 0])
        self.assertEqual(g['resize']['inputs']['input'], ['crop', 0])

    def test_user_loras_patch_the_model_both_stages_share(self):
        chosen, _ = loras.resolve([{'id': 'ltx23-dr34ml4y'}, {'id': 'ltx23-crisp', 'strength': 0.5}],
                                  'ltx-2.3')
        g = self._graph('ltx-2.3', loras=chosen)
        self.assertEqual(g['lora_0']['inputs']['model'], ['model', 0])
        self.assertEqual(g['lora_1']['inputs']['model'], ['lora_0', 0])
        self.assertEqual(g['lora_1']['inputs']['strength_model'], 0.5)
        self.assertEqual(g['guider1']['inputs']['model'], ['lora_1', 0])
        self.assertEqual(g['guider2']['inputs']['model'], ['lora_1', 0])

    def test_every_graph_link_points_at_an_existing_node(self):
        for model in video_worker.MODELS:
            g = self._graph(model)
            for node in g.values():
                for value in node['inputs'].values():
                    if isinstance(value, list):
                        self.assertIn(value[0], g)
