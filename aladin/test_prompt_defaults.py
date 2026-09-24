import os
import unittest
from unittest.mock import patch

from aladin import params, pipeline
from aladin.prompt_defaults import compile_prompt, prepare, tags_for
from aladin.image_models import MODELS
import request
import video_request


class PromptDefaultsTest(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, ALADIN_DEFAULT_RATING='explicit')
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_dedup_placement_and_empty(self):
        tags = ['sensitive', 'nsfw']
        self.assertEqual(compile_prompt('portrait', tags), 'portrait, sensitive, nsfw')
        self.assertEqual(compile_prompt('portrait, NSFW', tags), 'portrait, NSFW, sensitive')
        self.assertEqual(compile_prompt('insensitive', tags), 'insensitive, sensitive, nsfw')
        self.assertEqual(compile_prompt('', tags), 'sensitive, nsfw')
        # Anima / Pony 的分级词按模型卡约定放在开头
        self.assertEqual(compile_prompt('1girl, beach', ['explicit'], 'prefix'), 'explicit, 1girl, beach')
        self.assertEqual(prepare('portrait', {'rating': 'general'})['prompt_defaults']['effective'],
                         'portrait', 'Qwen 日常尺度不补任何词')

    def test_each_model_family_uses_its_own_vocabulary(self):
        expected = {
            'qwen-image-2.1': 'portrait, nsfw, explicit, uncensored',
            'anima-base-1.0': 'explicit, portrait',
            'pony-realism-2.2': 'rating_explicit, portrait',
        }
        for model, effective in expected.items():
            built, errors = params.image_params('portrait', 1, None, None, None, None, None,
                                                None, 1, model)
            self.assertFalse(errors)
            self.assertEqual(built['prompt_defaults']['effective'], effective, model)
        built, _ = params.image_params('portrait', 1, None, None, None, None, None, None, 1,
                                       'pony-realism-2.2', rating='suggestive')
        self.assertEqual(built['prompt_defaults']['effective'], 'rating_questionable, portrait')
        _, errors = params.image_params('portrait', 1, None, None, None, None, None, None, 1,
                                        rating='xxx')
        self.assertTrue(any('尺度' in e for e in errors))

    def test_rating_changes_the_idempotency_key(self):
        a, _ = params.image_params('portrait', 1, None, None, None, None, None, None, 1, rating='general')
        b, _ = params.image_params('portrait', 1, None, None, None, None, None, None, 1, rating='explicit')
        self.assertNotEqual(request.storage_key_for('portrait', 1, a),
                            request.storage_key_for('portrait', 1, b))

    def test_seed_variations_keep_frozen_policy(self):
        frozen = prepare('portrait', {'seed': 1, 'rating': 'suggestive'})
        with patch.dict(os.environ, ALADIN_DEFAULT_RATING='general'):
            self.assertEqual(prepare('portrait', dict(frozen, seed=2))['prompt_defaults'],
                             frozen['prompt_defaults'])
            self.assertEqual(prepare(frozen['prompt_defaults']['effective'], frozen)['prompt_defaults'],
                             frozen['prompt_defaults'])

    def test_every_image_model_keys_and_requests_use_same_effective_prompt(self):
        for model in MODELS:
            built, errors = params.image_params('portrait', 1, None, None, None, None, None, None, 1, model)
            self.assertFalse(errors)
            with patch('aladin.pipeline.db.create_job', return_value='test') as create:
                pipeline.enqueue('portrait', 1, built)
            saved = create.call_args.kwargs
            effective = built['prompt_defaults']['effective']
            self.assertEqual(saved['prompt'], 'portrait')
            self.assertEqual(saved['request']['prompts'], [effective])
            self.assertEqual(saved['result_key'], request.storage_key_for('portrait', 1, built))
            self.assertEqual(saved['params']['prompt_defaults']['original'], 'portrait')

    def test_edit_repair_and_video_use_their_family(self):
        qwen = ', '.join(tags_for('qwen', 'explicit'))
        for mode, region in [('img2img', None), ('edit', None), ('edit', [.1, .1, .9, .9])]:
            built, errors = params.edit_params(mode, 'repair', 1, 'blur', 20, 1, 'euler', 'simple', 1, .6, region)
            self.assertFalse(errors)
            with patch('aladin.pipeline.db.create_job', return_value='test') as create:
                pipeline.enqueue('repair', 1, built, mode, input_sha256='a' * 64)
            saved = create.call_args.kwargs
            self.assertEqual(saved['request']['prompts'], ['repair, ' + qwen])
            self.assertEqual(saved['request']['negative'], 'blur')
            self.assertEqual(saved['result_key'], request.storage_key_for('repair', 1, built, mode, 'a' * 64))
        built, errors = params.video_params('', 'blur', 'short', 'landscape', 6, 1, 12, 1,
                                            'res_multistep', 'simple', 1, rating='general')
        self.assertFalse(errors)
        with patch('aladin.pipeline.db.create_job', return_value='test') as create:
            pipeline.enqueue('', 1, built, 'i2v', input_sha256='b' * 64, app='video')
        saved = create.call_args.kwargs
        self.assertEqual(saved['request']['prompt'], '')
        self.assertEqual(saved['result_key'], video_request.key_for('', 'b' * 64, built))

    def test_final_length_is_checked_without_truncating_user_input(self):
        built, errors = params.image_params('x' * 2000, 1, None, None, None, None, None, None, 1)
        self.assertTrue(any('默认标签' in message for message in errors))
        self.assertEqual(built['prompt_defaults']['original'], 'x' * 2000)
        _, errors = params.image_params('x' * 2000, 1, None, None, None, None, None, None, 1,
                                        rating='general')
        self.assertEqual(errors, [])


if __name__ == '__main__':
    unittest.main()
