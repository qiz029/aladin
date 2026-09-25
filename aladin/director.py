"""一句需求出图的请求契约；不导入 Modal app，不在 API 进程执行模型。

两阶段：planner 先把需求拆成逐张的指令，宿主再把计划收敛成生图批量。
目标模型可选（Qwen / Pony / Anima）：planner 按目标模型的「方言」写提示词，
尺寸、步数、CFG 边界与所选 LoRA 的说明都通过请求里的 target 告诉它。
可选「先看计划再生成」：规划完停在 review 状态，人确认（可编辑）后才生成。
预设 manga：planner 先写故事与人物，再按一页的格子版式分镜；每格有自己的尺度（不超过用户选的上限）。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import request as image_request
from . import settings
from .prompt_defaults import FAMILIES, compile_prompt, default_rating, family_for, prepare, tags_for

APP = 'aladin-planner-v1'
RESULTS_VOLUME = 'aladin-planner-results-v1'
DEFAULT_MODEL = 'qwen-image-2.1'


def revision() -> str:
    root = Path(__file__).resolve().parent.parent
    digest = hashlib.sha256()
    for name in ('aladin_planner_modal_app.py', 'aladin/planner_schema.py', 'aladin/settings.py'):
        digest.update((root / name).read_bytes())
    return digest.hexdigest()


def target_for(model: str, chosen_loras: list[dict]) -> dict:
    """给 planner 的目标规格：尺寸、边界、默认值，以及本次启用 LoRA 的说明。

    选中带建议采样参数的 LoRA（例如 Anima Turbo）时，边界和默认值按它收窄——
    planner 规划出的每张图都要落在那个区间里，否则 Turbo 会糊。
    """
    from .image_models import MODELS
    from .loras import BY_ID, KINDS
    from .planner_schema import default_target
    if model == DEFAULT_MODEL:
        target = default_target()
    else:
        spec = MODELS[model]
        defaults = spec['defaults']
        target = {'model': model, 'family': family_for(model),
                  'sizes': {key: {'width': p['width'], 'height': p['height'], 'label': p['label']}
                            for key, p in spec['sizes'].items()},
                  'steps': list(spec['steps']), 'cfg': [0.0, 10.0],
                  'defaults': {'size': defaults['size'], 'steps': defaults['steps'],
                               'cfg': float(defaults['cfg']), 'negative': defaults['negative'],
                               'sampler': defaults['sampler'], 'scheduler': defaults['scheduler']},
                  'loras': []}
    for chosen in chosen_loras:
        item = BY_ID[chosen['id']]
        target['loras'].append({'name': item['name'], 'kind': KINDS[item['kind']],
                                'notes': (item['notes'] or '')[:200]})
        settings_hint = item['settings']
        if 'steps' in settings_hint:
            low, high = target['steps']
            steps = settings_hint['steps']
            target['steps'] = [max(low, steps - 2), min(high, steps + 2)]
            target['defaults']['steps'] = steps
        if 'cfg' in settings_hint:
            target['cfg'] = [float(settings_hint['cfg'])] * 2
            target['defaults']['cfg'] = float(settings_hint['cfg'])
        if 'sampler' in settings_hint:
            target['defaults']['sampler'] = settings_hint['sampler']
    return target


def _worker_revision(model: str) -> str:
    if model == DEFAULT_MODEL:
        return image_request.worker_revision()
    from .extra_image_request import revision as extra_revision
    return extra_revision()


def build(brief: str, count: int | None = None, rating: str | None = None,
          model: str = DEFAULT_MODEL, loras: list[dict] | None = None,
          preset: str | None = None, creative_spec=None) -> dict:
    """规划请求。尺度标签、LoRA 触发词与目标规格都在提交时冻结，规划期间改配置不影响这次任务。"""
    from .planner_schema import allowed_ratings, creative_spec as validate_spec
    rating = rating or default_rating()
    loras = loras or []
    family = family_for(model)
    from .loras import triggers_for
    body = {'brief': brief.strip(), 'count': count, 'plannerRevision': revision(),
            'imageWorkerRevision': _worker_revision(model),
            'promptTags': tags_for(family, rating), 'rating': rating,
            'model': model, 'target': target_for(model, loras),
            'loras': [{'id': l['id'], 'version': l['version'], 'strength': l['strength']} for l in loras],
            'loraTriggers': triggers_for(loras)}
    spec = validate_spec(creative_spec)
    if spec:
        body['creative_spec'] = spec
    if preset:
        # 漫画每格的尺度不同：上限以内每一档的标签都在提交时冻结
        body['preset'] = preset
        body['ratingTags'] = {level: tags_for(family, level) for level in allowed_ratings(rating)}
    body['key'] = hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return body


def is_planning(row: dict) -> bool:
    return row.get('mode') == 'director' and 'variants' not in row['request']


def _planning_request(row: dict) -> dict:
    """规划时的请求。进入 review / 生成阶段后 request 换成了生图批量，原请求存在 plan.request 里。"""
    if is_planning(row):
        return row['request']
    return row['params']['plan']['request']


def image_batch(row: dict, plan: dict) -> tuple[dict, dict]:
    """planner 的输出 → 生图批量。重新过一遍宿主端 harness；原始计划和有效参数都保留。"""
    if not isinstance(plan, dict) or not isinstance(plan.get('variants'), list):
        raise ValueError('planner 返回了无效计划')
    planning = _planning_request(row)
    if plan.get('plannerRevision') != planning['plannerRevision']:
        raise ValueError('planner 结果版本不符')
    variants, story, notes = _validate(planning, plan, plan['variants'], planning.get('count'))
    base = dict(plan, **story, notes=list(plan.get('notes', [])) + notes)
    return _finalize(row, planning, variants, base)


def _validate(planning: dict, plan: dict, items: list, count: int | None) -> tuple[list[dict], dict, list[str]]:
    """普通计划与漫画计划各走各的 harness；两者的输出都能原样再校验一遍。"""
    from .planner_schema import panel_input, validate_manga, validate_plan
    if planning.get('preset') == 'manga':
        return validate_manga({'story': plan.get('story'), 'characters': plan.get('characters'),
                               'panels': [panel_input(item) for item in items]},
                              count, planning.get('target'), planning.get('rating'))
    variants, notes = validate_plan({'images': items}, count, planning.get('target'))
    return variants, {}, notes


def approve(row: dict, edits: list[dict] | None) -> tuple[dict, dict]:
    """review 阶段确认：可以改提示词、负向词、尺寸、步数、CFG、种子，也可以删掉几张。

    改过的计划与 planner 的输出走同一套校验和标签补充，不开第二条路。
    edits 为 None 表示原样确认。
    """
    planning = _planning_request(row)
    current = row['params']['plan']
    if edits is None:
        edits = [dict(item, prompt=item['prompt_defaults']['original']) for item in current['variants']]
    if not isinstance(edits, list) or not edits:
        raise ValueError('至少保留一张图')
    if planning.get('preset') == 'manga':
        # 格子与版式绑定：不能删格；每条修改盖在同位置的原格上，没给的字段保持原样
        if len(edits) != len(current['variants']):
            raise ValueError('漫画的格数由版式决定，确认时不能增删格子')
        edits = [{key: value for key, value in {
                      **original, 'prompt': original['prompt_defaults']['original'],
                      **original.get('panel', {}), **edit}.items() if key != 'panel'}
                 for original, edit in zip(current['variants'], edits)]
    variants, story, notes = _validate(planning, current, edits, None)
    base = dict(current, **story, notes=list(current.get('notes', [])) + ['计划经人工确认'] +
                [f'确认时：{note}' for note in notes])
    return _finalize(row, planning, variants, base)


def _finalize(row: dict, planning: dict, variants: list[dict], base: dict) -> tuple[dict, dict]:
    from .params import image_params
    model = planning.get('model') or DEFAULT_MODEL
    family = family_for(model)
    batch_tags = planning.get('promptTags', [])
    rating_tags = planning.get('ratingTags') or {}
    triggers = planning.get('loraTriggers', [])
    placement = FAMILIES[family]['placement']
    target = planning.get('target') or {}
    notes = list(base.get('notes', []))
    policy = {'rating': planning.get('rating'), 'prompt_family': family, 'lora_triggers': triggers}
    limit = settings.PARAM_LIMITS['prompt_chars']
    for index, item in enumerate(variants, start=1):
        # 漫画每格按自己的尺度补标签；普通计划整批一套
        rating = item.get('panel', {}).get('rating')
        tags = rating_tags[rating] if rating in rating_tags else batch_tags
        policy['rating'] = rating or planning.get('rating')
        original = item.get('prompt_defaults', {}).get('original', item['prompt'])
        prepared = prepare(original, policy, tags=tags)['prompt_defaults']
        if len(prepared['effective']) > limit:
            budget = limit - len(compile_prompt(compile_prompt('', tags, placement), triggers)) - 4
            if budget <= 0:
                raise ValueError('默认标签与触发词过长，无法容纳生成提示词')
            prepared = dict(prepare(original[:budget], policy, tags=tags)['prompt_defaults'],
                            original=original)
            notes.append(f'第 {index} 条为默认标签与触发词预留空间，已缩短规划提示词；原文保留在 prompt_defaults.original')
        item['prompt_defaults'] = prepared
        item['prompt'] = prepared['effective']
        if 'sampler' in target.get('defaults', {}):
            item['sampler'] = target['defaults']['sampler']
            item['scheduler'] = target['defaults']['scheduler']
        _, errors = image_params(item['prompt'], 1, item['size'], item['negative'], item['steps'],
                                 item['cfg'], item['sampler'], item['scheduler'], item['seed'],
                                 model, prompt_tags=tags)
        if errors:
            raise ValueError('；'.join(errors))
        item['loras'] = [{'file': l['file'], 'sha256': l['sha256'], 'strength': l['strength']}
                         for l in row['params'].get('loras') or []]
    if _worker_revision(model) != planning['imageWorkerRevision']:
        raise ValueError('规划期间图像 worker 版本已变，请重新提交需求')
    effective = dict(base, variants=variants, notes=notes, request=planning,
                     call_id=base.get('call_id', row.get('call_id')))
    params = dict(row['params'], images=len(variants), plan=effective)
    if model == DEFAULT_MODEL:
        request = image_request.build_variants(row['result_key'], variants,
                                               settings.DEFAULT_PARAMS['sampler'],
                                               settings.DEFAULT_PARAMS['scheduler'])
    else:
        from .extra_image_request import build_variants
        request = build_variants(row['result_key'], model, variants,
                                 target['defaults']['sampler'], target['defaults']['scheduler'])
    return request, params
