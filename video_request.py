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

MODEL = worker.MODEL
DEFAULTS = worker.DEFAULTS


def worker_revision() -> str:
    return hashlib.sha256(Path(worker.__file__).read_bytes()).hexdigest()


def storage_key(prompt: str, negative: str, width: int, height: int, frames: int,
                steps: int, cfg: float, shift: float, seed: int, sampler: str,
                scheduler: str, input_sha256: str, lora_strength: float) -> str:
    """64 位 hex 结果目录名：包含全部影响输出的参数，同参数重跑命中容器回执。"""
    shape = json.dumps({
        'prompt': prompt, 'negative': negative, 'width': width, 'height': height,
        'frames': frames, 'steps': steps, 'cfg': cfg, 'shift': shift, 'seed': seed,
        'sampler': sampler, 'scheduler': scheduler, 'inputSha256': input_sha256,
        'loraStrength': lora_strength,
        'model': MODEL, 'modelRevision': worker.MODELS[MODEL]['revision'],
        'workerRevision': worker_revision(),
    }, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(shape.encode()).hexdigest()


def key_for(prompt: str, input_sha256: str, params: dict) -> str:
    """从「页面/API 的参数字典」算幂等键。

    params 的字段名与 build() 的 overrides 一致（width/height/frames/steps/...）；
    web 与 api 都走这里，避免两处各算一遍键导致 409 反查不到任务。
    """
    from aladin.prompt_defaults import effective_prompt
    return storage_key(
        prompt=effective_prompt(prompt, params), negative=params.get('negative', ''),
        width=params['width'], height=params['height'], frames=params['frames'],
        steps=params['steps'], cfg=params['cfg'], shift=params['shift'],
        seed=params['seed'], sampler=params['sampler'], scheduler=params['scheduler'],
        input_sha256=input_sha256,
        lora_strength=float(params.get('loraStrength', 1.0)))


def build(prompt: str, input_sha256: str, **overrides) -> dict:
    """按默认值 + 覆盖项构造请求。inputSha256 必须与将要发给容器的字节一致。"""
    params = dict(DEFAULTS, **overrides)
    params.setdefault('negative', '')
    params.setdefault('loraStrength', 1.0)
    prompt = prompt.strip()
    key = storage_key(prompt=prompt, input_sha256=input_sha256,
                      negative=params['negative'], width=params['width'],
                      height=params['height'], frames=params['frames'],
                      steps=params['steps'], cfg=params['cfg'], shift=params['shift'],
                      seed=params['seed'], sampler=params['sampler'],
                      scheduler=params['scheduler'],
                      lora_strength=float(params['loraStrength']))
    return {
        'workerRevision': worker_revision(),
        'comfyRevision': worker.COMFY_REVISION,
        'ggufRevision': worker.GGUF_REVISION,
        'model': MODEL,
        'modelRevision': worker.MODELS[MODEL]['revision'],
        'key': key,
        'prompt': prompt,
        'negative': params['negative'],
        'width': params['width'],
        'height': params['height'],
        'frames': params['frames'],
        'seed': params['seed'],
        'steps': params['steps'],
        'cfg': params['cfg'],
        'shift': params['shift'],
        'loraStrength': float(params['loraStrength']),
        'sampler': params['sampler'],
        'scheduler': params['scheduler'],
        'inputSha256': input_sha256,
    }
