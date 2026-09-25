"""构造发给 video Modal app 的生成请求（宿主侧）。

权重表与钉版直接从 `aladin/video_worker.py` 取——宿主与容器共用同一份，
不另写一遍模型表。`workerRevision` 是 video_worker.py 自身的 sha256，
容器会拿它跟挂进镜像的那份比对，不一致直接拒绝。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from aladin import video_worker as worker

DEFAULT_MODEL = worker.DEFAULT_MODEL
DEFAULTS = worker.DEFAULTS


def worker_revision() -> str:
    return hashlib.sha256(Path(worker.__file__).read_bytes()).hexdigest()


def _loras(loras) -> list[dict]:
    """容器只收 文件名 + sha256 + 强度；名称、版本留在宿主的任务记录里。"""
    return [{'file': item['file'], 'sha256': item['sha256'], 'strength': float(item['strength'])}
            for item in loras or []]


def storage_key(prompt: str, model: str, width: int, height: int, frames: int, seed: int,
                input_sha256: str, loras=None) -> str:
    """64 位 hex 结果目录名：包含全部影响输出的参数，同参数重跑命中容器回执。"""
    shape = json.dumps({
        'prompt': prompt, 'width': width, 'height': height, 'frames': frames, 'seed': seed,
        'inputSha256': input_sha256, 'loras': _loras(loras),
        'model': model, 'modelRevision': worker.MODELS[model]['revision'],
        'workerRevision': worker_revision(),
    }, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(shape.encode()).hexdigest()


def key_for(prompt: str, input_sha256: str, params: dict) -> str:
    """从「页面/API 的参数字典」算幂等键。

    params 的字段名与 build() 的 overrides 一致（model/width/height/frames/seed/loras）；
    web 与 api 都走这里，避免两处各算一遍键导致 409 反查不到任务。
    """
    from aladin.prompt_defaults import effective_prompt
    return storage_key(
        prompt=effective_prompt(prompt, params), model=params.get('model', DEFAULT_MODEL),
        width=params['width'], height=params['height'], frames=params['frames'],
        seed=params['seed'], input_sha256=input_sha256, loras=params.get('loras'))


def build(prompt: str, input_sha256: str, **overrides) -> dict:
    """按默认值 + 覆盖项构造请求。inputSha256 必须与将要发给容器的字节一致。"""
    params = dict(DEFAULTS, **overrides)
    model = params.get('model') or DEFAULT_MODEL
    prompt = prompt.strip()
    key = storage_key(prompt=prompt, model=model, width=params['width'],
                      height=params['height'], frames=params['frames'], seed=params['seed'],
                      input_sha256=input_sha256, loras=params.get('loras'))
    return {
        'workerRevision': worker_revision(),
        'comfyRevision': worker.COMFY_REVISION,
        'model': model,
        'modelRevision': worker.MODELS[model]['revision'],
        'key': key,
        'prompt': prompt,
        'width': params['width'],
        'height': params['height'],
        'frames': params['frames'],
        'seed': params['seed'],
        'loras': _loras(params.get('loras')),
        'inputSha256': input_sha256,
    }
