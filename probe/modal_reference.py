"""从 agent-media-lab 参考实现读取事实，不复制也不依赖它。

参考实现是已冻结的原型：这里只读它的常量与函数名，用来构造合法的 Modal 请求。
版本哈希实时计算，避免上游改动后本地硬编码静默失配。
"""
from __future__ import annotations

import ast
import hashlib
import os
import re
from pathlib import Path

# 默认与本仓库同级；放在别处时用 AGENT_MEDIA_LAB 指定（写进 .env 或 shell 环境）
REFERENCE_ROOT = Path(os.environ.get(
    'AGENT_MEDIA_LAB', Path(__file__).resolve().parent.parent.parent / 'agent-media-lab'))

APP_GPU = 'agent-media-lab-gpu-v1'
APP_IMAGE = 'agent-media-lab-image-v1'
# 仅供探针读回产物；aladin 自己的 Volume 命名见待落地的 ADR-0002。
VOLUME_RESULTS = 'agent-media-lab-gpu-results-v1'

_IMAGE_CONSTANTS = (
    'MODEL',
    'COMFY_REVISION',
    'GGUF_REVISION',
    'QUANT',
    'TEXT_ENCODER',
    'VAE',
    'SAMPLERS',
    'SCHEDULERS',
)


def _gpu_worker() -> Path:
    path = REFERENCE_ROOT / 'tools' / 'gpu' / 'worker.py'
    if not path.is_file():
        raise FileNotFoundError(
            f'找不到参考实现 {path}；探针需要它来计算 worker revision 与 GPU 档案'
        )
    return path


def _image_worker() -> Path:
    path = REFERENCE_ROOT / 'tools' / 'gpu' / 'image_worker.py'
    if not path.is_file():
        raise FileNotFoundError(f'找不到参考实现 {path}；探针需要它来构造生图请求')
    return path


def _profiles() -> dict:
    """解析参考实现的 PROFILES 表。

    不能手抄：worker.execute() 会逐字段比对 request['resources']，
    配额抄错会被容器以 'GPU deployment profile mismatch' 拒绝。
    """
    source = _gpu_worker().read_text()
    block = re.search(r'^PROFILES\s*=\s*\{(.*?)^\}', source, re.M | re.S)
    if block is None:
        raise ValueError('参考实现里找不到 PROFILES')
    entries = {}
    for gpu, function, cpu, memory in re.findall(
        r"'([A-Za-z0-9-]+)'\s*:\s*\{\s*'function'\s*:\s*'([a-z0-9_]+)'"
        r"\s*,\s*'cpu'\s*:\s*(\d+)\s*,\s*'memory'\s*:\s*(\d+)\s*\}",
        block.group(1),
    ):
        entries[gpu] = {'function': function, 'cpu': int(cpu), 'memory': int(memory)}
    if not entries:
        raise ValueError('PROFILES 解析结果为空')
    return entries


PROFILES = _profiles()
GPU_FUNCTIONS = {gpu: spec['function'] for gpu, spec in PROFILES.items()}


def worker_revision() -> str:
    """容器内 worker.revision() 就是 worker.py 自身的 sha256。"""
    return hashlib.sha256(_gpu_worker().read_bytes()).hexdigest()


def image_worker_revision() -> str:
    return hashlib.sha256(_image_worker().read_bytes()).hexdigest()


def image_constants() -> dict:
    """选取 image_worker.py 里的字面量常量，值必须是单行字面量。"""
    source = _image_worker().read_text()
    values = {}
    for name in _IMAGE_CONSTANTS:
        match = re.search(rf'^{name}\s*=\s*(.+)$', source, re.M)
        if match is None:
            raise ValueError(f'参考实现里找不到常量 {name}')
        try:
            values[name] = ast.literal_eval(match.group(1))
        except (ValueError, SyntaxError) as error:
            raise ValueError(
                f'参考实现里的 {name} 不是单行字面量（上游若改成多行或计算得出，'
                f'这里需要跟着改成解析 AST）：{match.group(1)!r}'
            ) from error
    return values


def storage_request(key: str, gpu: str, task: str, inputs: bytes = b'') -> dict:
    """构造 gpu-smoke / demucs 这类 worker.execute() 请求。"""
    if gpu not in PROFILES:
        raise ValueError('Unsupported GPU; choose: ' + ', '.join(PROFILES))
    resources = dict(PROFILES[gpu], gpu=gpu, timeout=600)
    return {
        'workerRevision': worker_revision(),
        'task': task,
        'gpu': gpu,
        'resources': resources,
        'key': key,
        'inputSha256': hashlib.sha256(inputs).hexdigest(),
    }


def image_request(prompts, width, height, steps, cfg, seed, sampler, scheduler,
                  negative='', model_revision='') -> dict:
    """构造 image_worker.validate() 能接受的生图请求。"""
    constants = image_constants()
    if sampler not in constants['SAMPLERS']:
        raise ValueError('sampler 必须是 ' + ', '.join(constants['SAMPLERS']))
    if scheduler not in constants['SCHEDULERS']:
        raise ValueError('scheduler 必须是 ' + ', '.join(constants['SCHEDULERS']))
    if not re.fullmatch(r'[a-f0-9]{40}', model_revision):
        raise ValueError('modelRevision 必须是 40 位 hex（HuggingFace 的模型 commit）')
    return {
        'workerRevision': image_worker_revision(),
        'model': constants['MODEL'],
        'modelRevision': model_revision,
        'comfyRevision': constants['COMFY_REVISION'],
        'ggufRevision': constants['GGUF_REVISION'],
        'quantization': constants['QUANT'],
        'key': '',
        'prompts': list(prompts),
        'negative': negative,
        'width': width,
        'height': height,
        'seed': seed,
        'steps': steps,
        'cfg': cfg,
        'sampler': sampler,
        'scheduler': scheduler,
    }
