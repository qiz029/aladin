"""Model contract regressions: defaults, graph, idempotency and API parity."""
import unittest
from unittest.mock import patch
from aladin.image_models import MODELS, DEFAULT_MODEL
from aladin.params import image_params
from aladin.extra_image_request import build
from aladin.extra_image_worker import workflow, validate
from aladin.prompt_defaults import effective_prompt
import request

class ModelContractTest(unittest.TestCase):
    def params(self, model, **overrides):
        values = dict(prompt='a ceramic teapot', images=1, size=None, negative=None,
                      steps=None, cfg=None, sampler=None, scheduler=None, seed=42, model=model)
        values.update(overrides)
        result, errors = image_params(**values)
        self.assertEqual(errors, [])
        return result

    def test_defaults_and_explicit_empty_negative(self):
        for model, cfg in [(DEFAULT_MODEL, 1), ('anima-base-1.0', 4.5), ('pony-realism-2.2', 6.5)]:
            with self.subTest(model=model):
                self.assertEqual(self.params(model)['cfg'], cfg)
                self.assertEqual(self.params(model, negative='')['negative'], '')

    def test_unsupported_model_sampler_and_step_bounds(self):
        for model, sampler, steps in [('bad', None, None), ('anima-base-1.0', 'lcm', 35), (DEFAULT_MODEL, None, 50)]:
            _, errors = image_params('tea', 1, None, None, steps, None, sampler, None, 0, model)
            self.assertTrue(errors)
        self.params('anima-base-1.0', steps=50)

    def test_native_graphs_and_request_integrity(self):
        for model in ['anima-base-1.0', 'pony-realism-2.2']:
            params = self.params(model)
            payload = build('a ceramic teapot', 2, params)
            self.assertEqual(validate(payload), payload)
            graph = workflow(payload)
            self.assertEqual(graph['11']['inputs']['seed'], 42)
            self.assertEqual(graph['15']['inputs']['seed'], 43)
            self.assertEqual(graph['10']['inputs']['text'], 'a ceramic teapot')
            self.assertEqual(graph['1']['class_type'], 'UNETLoader' if model.startswith('anima') else 'CheckpointLoaderSimple')
            if model.startswith('pony'):
                self.assertEqual(graph['2']['inputs']['stop_at_clip_layer'], -2)
            payload['workerRevision'] = 'stale'
            with self.assertRaises(ValueError):
                validate(payload)

    def test_keys_are_model_specific_and_qwen_is_backward_compatible(self):
        keys = [request.storage_key_for('tea', 1, self.params(m)) for m in MODELS]
        self.assertEqual(len(set(keys)), 3)
        params = self.params(DEFAULT_MODEL)
        key = request.storage_key_for('tea', 1, params)
        params.pop('model')
        self.assertEqual(key, request.storage_key_for('tea', 1, params))
        pony = self.params('pony-realism-2.2')
        self.assertEqual(request.storage_key_for(' tea ', 1, pony), build(effective_prompt('tea', pony), 1, pony)['key'])

    def test_api_resolves_selected_model_and_routes_worker(self):
        from aladin.api import ImageRequest, create_image
        import asyncio
        for model in MODELS:
            with patch('aladin.api._submit', return_value=('test', True)) as submit, patch('aladin.api._respond', return_value={}) as respond:
                asyncio.run(create_image(ImageRequest(prompt='teapot', model=model), False, 1))
                self.assertEqual(submit.call_args.kwargs['built']['cfg'], MODELS[model]['defaults']['cfg'])

    def test_reuse_context_preserves_custom_params(self):
        from aladin.web import image_context, _vary_key
        params = self.params('pony-realism-2.2', steps=44, cfg=7, size='portrait')
        context = image_context(params)
        self.assertEqual(context['defaults']['steps'], 44)
        self.assertEqual(context['defaults']['model'], 'pony-realism-2.2')
        self.assertEqual(context['sizes']['portrait']['width'], 832)
        self.assertEqual(_vary_key(dict(prompt='tea'), params, 'txt2img'), build(effective_prompt('tea', params), 1, params)['key'])
