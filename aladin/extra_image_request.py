"""Pinned request and content-addressed result key for Anima and Pony."""
import hashlib
import json
from pathlib import Path
from .image_models import MODELS, COMFY_REVISION, DEFAULT_MODEL

def revision():
    root = Path(__file__).parent
    return hashlib.sha256(b''.join((root / name).read_bytes() for name in
        ('extra_image_worker.py', 'extra_image_request.py', 'image_models.py', 'worker.py'))).hexdigest()

def build(prompt, images, params):
    model = params['model']
    if model == DEFAULT_MODEL or model not in MODELS:
        raise ValueError('Unsupported extra image model')
    spec = MODELS[model]
    request = dict(mode='txt2img', modelId=model, model=spec['repo'], modelRevision=spec['revision'],
        workerRevision=revision(), comfyRevision=COMFY_REVISION, prompts=[prompt.strip()] * images,
        params={k: params[k] for k in ('model', 'negative', 'size', 'width', 'height', 'steps', 'cfg', 'sampler', 'scheduler', 'seed')})
    # 容器只需要文件、校验和与强度；名称和版本留在宿主的任务记录里
    loras = [{'file': item['file'], 'sha256': item['sha256'], 'strength': item['strength']}
             for item in params.get('loras') or []]
    if loras:
        request['loras'] = loras
    request['key'] = hashlib.sha256(json.dumps(request, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return request


def build_variants(key, model, variants, sampler, scheduler):
    """一句话出图的批量：每张图自带提示词、尺寸、步数、CFG、种子和 LoRA（与 Qwen 的 build_variants 同形）。

    LoRA 逐张给：现在同一批共用一套，以后由 planner 逐张挑时容器与记录格式都不用改。
    结果目录用调用方给的 key（一句话出图任务的 result_key），不按内容另算。
    """
    if model == DEFAULT_MODEL or model not in MODELS:
        raise ValueError('Unsupported extra image model')
    spec = MODELS[model]
    return dict(mode='txt2img', modelId=model, model=spec['repo'], modelRevision=spec['revision'],
        workerRevision=revision(), comfyRevision=COMFY_REVISION, key=key, sampler=sampler,
        scheduler=scheduler,
        variants=[dict(prompt=item['prompt'].strip(), negative=item.get('negative') or '',
                       size=item['size'], width=item['width'], height=item['height'],
                       steps=item['steps'], cfg=item['cfg'], seed=item['seed'],
                       loras=[{'file': l['file'], 'sha256': l['sha256'], 'strength': l['strength']}
                              for l in item.get('loras') or []])
                  for item in variants])
