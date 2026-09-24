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
