"""从一张产物恢复创作表单；网页与 agent 使用同一份设置。"""
from __future__ import annotations

from . import db, settings
from .image_models import DEFAULT_MODEL, MODELS


def artifact_settings(job_id: str, name: str) -> dict:
    job = db.job(job_id)
    item = db.artifact(job_id, name)
    if job is None or item is None or not name.lower().endswith(('.png', '.jpg', '.jpeg')):
        raise LookupError('图片不存在')
    mode = job.get('mode', 'txt2img')
    if mode not in ('txt2img', 'director', 'img2img', 'edit'):
        raise ValueError('这个任务不支持图片设置回填')
    params = dict(job['params'])
    # planner 的逐图设置（包括每格尺度）比整批设置更具体。
    variants = params.get('plan', {}).get('variants', [])
    for index, variant in enumerate(variants, 1):
        if name == f'image-{index:02d}.png':
            params.update(variant)
            break
    params.update(item.get('params') or {})
    policy = params.get('prompt_defaults') or {}
    prompt = item['prompt']
    # 表单编辑原文，避免改尺度或取消 LoRA 时留下旧标签；无法核实时保留实际提示词。
    if policy.get('effective') == prompt and len(policy.get('original', '')) <= settings.PARAM_LIMITS['prompt_chars']:
        prompt = policy['original']
    params.update(prompt=prompt, seed=item['seed'], images=1,
                  rating=policy.get('rating') or params.get('rating'))
    model = job['params'].get('model', DEFAULT_MODEL)
    if mode in ('txt2img', 'director'):
        model = params.get('model', model)
        if model not in MODELS:
            raise ValueError('原模型已不可用，无法完整回填')
        spec = MODELS[model]
        size = next((key for key, value in spec['sizes'].items()
                     if (value['width'], value['height']) == (params.get('width'), params.get('height'))), None)
        if size is None:
            raise ValueError('原尺寸不在当前模型预设内，无法完整回填')
        defaults = dict(spec['defaults'], **params)
        defaults.update(model=model, size=size)
        # worker 的 LoRA 记录没有 id，按文件与哈希从冻结的任务记录还原。
        chosen = job['params'].get('loras') or []
        loras = []
        for lora in params.get('loras') or []:
            source = next((value for value in chosen
                           if value.get('file') == lora.get('file') and value.get('sha256') == lora.get('sha256')), None)
            if source is None:
                raise ValueError('原 LoRA 记录不完整，无法完整回填')
            from .loras import BY_ID
            current = BY_ID.get(source['id'])
            if current is None or current['sha256'] != source['sha256']:
                raise ValueError('原 LoRA 版本已不可用，无法完整回填')
            loras.append({'id': source['id'], 'strength': lora['strength']})
        defaults['loras'] = loras
        target = '/apps/image'
        input_url = None
    else:
        if not job.get('input_path') or not db.data_path(job['input_path']).is_file():
            raise LookupError('原始输入图片已不在本地，无法恢复改图设置')
        defaults = dict(settings.EDIT_DEFAULT_PARAMS, **params, mode=mode)
        target = '/apps/edit'
        input_url = f'/jobs/{job_id}/input'
    # 不把完整任务/规划记录塞进表单或 API 回填对象。
    fields = ('prompt', 'negative', 'model', 'images', 'size', 'width', 'height', 'steps',
              'cfg', 'sampler', 'scheduler', 'seed', 'rating', 'loras', 'denoise', 'region', 'mode')
    return {'page': target, 'mode': 'txt2img' if mode == 'director' else mode,
            'settings': {key: defaults[key] for key in fields if key in defaults},
            'input_url': input_url, 'source_job': job_id, 'source_name': name,
            'creative_spec': job['params'].get('creative_spec', {})}
