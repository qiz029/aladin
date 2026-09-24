"""临时基准 app：把当前 worker（Q8_0）跑在不同 GPU 上，测同一请求的耗时。

刻意不碰生产 app（`aladin-image-v1`）：它的 worker revision 与工作站上的副本绑定，
在量化切换的同时直接改生产 app，会让还没同步的客户端全部 revision mismatch。
基准跑完这个 app 可以整个删掉。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import modal  # noqa: E402

from aladin_modal_app import _image  # noqa: E402

# include_source=True：函数定义在 probe/ 下，而镜像只挂了 aladin_modal_app.py 那几个文件，
# 不带上源码的话容器 import 不到这个模块（实测会 crash-loop）。
app = modal.App('aladin-image-bench', include_source=True)
cache = modal.Volume.from_name('agent-media-lab-gpu-models-v1', create_if_missing=True)
results = modal.Volume.from_name('aladin-image-results-v1', create_if_missing=True)


def _bench(gpu: str):
    return app.function(image=_image(), gpu=gpu, cpu=4, memory=32768, timeout=3600,
                        startup_timeout=1200, retries=0, min_containers=0,
                        max_containers=1, scaledown_window=2,
                        volumes={'/models': cache, '/results': results})


def _run(request: dict) -> dict:
    from aladin import worker

    record = worker.execute(dict(request), '/results')
    results.commit()
    return {'key': request['key'], 'elapsedSeconds': record['elapsedSeconds'],
            'quantization': request['quantization'],
            'images': record['images']}


@_bench('L40S')
def generate_l40s(request: dict) -> dict:
    return _run(request)


@_bench('A100-80GB')
def generate_a100(request: dict) -> dict:
    return _run(request)


@_bench('H100')
def generate_h100(request: dict) -> dict:
    return _run(request)


def build_request(prompt: str, seed: int, **overrides):
    """用宿主侧的 request.py 构造请求（与生产同一条路径）。"""
    sys.path.insert(0, str(ROOT))
    import request as reqmod

    params = {'seed': seed, 'width': 1024, 'height': 1024, 'steps': 25, 'cfg': 1.0,
              'sampler': 'euler', 'scheduler': 'simple', 'negative': ''}
    params.update(overrides)
    key = reqmod.storage_key_for(prompt=prompt, images=1, params=params, mode='txt2img')
    return reqmod.build(key=key, prompt=prompt, images=1, seed=params['seed'],
                        width=params['width'], height=params['height'],
                        steps=params['steps'], cfg=params['cfg'],
                        sampler=params['sampler'], scheduler=params['scheduler'],
                        negative='', mode='txt2img')
