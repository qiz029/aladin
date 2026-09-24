"""一句需求出图的请求契约；不导入 Modal app，不在 API 进程执行模型。"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import request as image_request
from . import settings
from .prompt_defaults import FAMILIES, default_rating, prepare, compile_prompt, tags_for

APP = 'aladin-planner-v1'
RESULTS_VOLUME = 'aladin-planner-results-v1'


def revision() -> str:
    root = Path(__file__).resolve().parent.parent
    digest = hashlib.sha256()
    for name in ('aladin_planner_modal_app.py', 'aladin/planner_schema.py', 'aladin/settings.py'):
        digest.update((root / name).read_bytes())
    return digest.hexdigest()


def build(brief: str, count: int | None = None, rating: str | None = None) -> dict:
    # 最终出图用 Qwen-Image：按 Qwen 族的词表在提交时冻结标签
    rating = rating or default_rating()
    body = {'brief': brief.strip(), 'count': count, 'plannerRevision': revision(),
            'imageWorkerRevision': image_request.worker_revision(),
            'promptTags': tags_for('qwen', rating), 'rating': rating}
    body['key'] = hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return body


def is_planning(row: dict) -> bool:
    return row.get('mode') == 'director' and 'variants' not in row['request']


def image_batch(row: dict, plan: dict) -> tuple[dict, dict]:
    """重新过宿主端 harness；原始计划和有效参数都保留。"""
    from .planner_schema import validate_plan
    from .params import image_params
    if not isinstance(plan, dict) or not isinstance(plan.get('variants'), list):
        raise ValueError('planner 返回了无效计划')
    if plan.get('plannerRevision') != row['request']['plannerRevision']:
        raise ValueError('planner 结果版本不符')
    variants, notes = validate_plan({'images': plan['variants']}, row['request'].get('count'))
    tags = row['request'].get('promptTags', [])
    for index, item in enumerate(variants, start=1):
        prepared = prepare(item['prompt'], {'rating': row['request'].get('rating')}, tags=tags)
        if len(prepared['prompt_defaults']['effective']) > settings.PARAM_LIMITS['prompt_chars']:
            budget = settings.PARAM_LIMITS['prompt_chars'] - len(compile_prompt('', tags)) - 2
            if budget <= 0:
                raise ValueError('默认标签过长，无法容纳生成提示词')
            prepared['prompt_defaults']['effective'] = compile_prompt(
                item['prompt'][:budget], tags, FAMILIES['qwen']['placement'])
            notes.append(f'第 {index} 条为默认标签预留空间，已缩短规划提示词；原文保留在 prompt_defaults.original')
        item['prompt_defaults'] = prepared['prompt_defaults']
        item['prompt'] = prepared['prompt_defaults']['effective']
        _, errors = image_params(item['prompt'], 1, item['size'], item['negative'],
                                  item['steps'], item['cfg'], settings.DEFAULT_PARAMS['sampler'],
                                  settings.DEFAULT_PARAMS['scheduler'], item['seed'], prompt_tags=tags)
        if errors:
            raise ValueError('；'.join(errors))
    if image_request.worker_revision() != row['request']['imageWorkerRevision']:
        raise ValueError('规划期间图像 worker 版本已变，请重新提交需求')
    effective = dict(plan, variants=variants, notes=list(plan.get('notes', [])) + notes,
                     request=row['request'], call_id=row.get('call_id'))
    params = dict(row['params'], images=len(variants), plan=effective, stage='generating')
    request = image_request.build_variants(row['result_key'], variants,
                                           settings.DEFAULT_PARAMS['sampler'],
                                           settings.DEFAULT_PARAMS['scheduler'])
    return request, params
