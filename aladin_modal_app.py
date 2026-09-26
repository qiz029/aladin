"""aladin 的 Modal app：跑 Qwen-Image 生图。

镜像前几层与 agent-media-lab 的 `agent-media-lab-image-v1` **逐字一致**，
以便复用其已缓存的 apt/pip/git 层（实测基础镜像重建仅 2–3 秒）；
任何前缀差异都会让后续所有昂贵层失效并触发十几分钟重建。
"""
from pathlib import Path

import modal

COMFY_REVISION = 'c194dd00cd42aa18d9dbf27d977bf6b85d9ea565'
# 保留已缓存的依赖层；最后单独更新只改 loader.py 的上游修复。
GGUF_BASE_REVISION = 'f912d5e5c25921e41eae2c0131eeb4d350e7c165'
GGUF_REVISION = 'edd981b10e107d3b8f58e16c498f2d08f631bc47'


def _image() -> modal.Image:
    base = (modal.Image.debian_slim(python_version='3.11')
            .apt_install('git')
            .pip_install('torch==2.8.0', 'torchvision==0.23.0', 'torchaudio==2.8.0',
                         index_url='https://download.pytorch.org/whl/cu128')
            .run_commands('git clone --quiet https://github.com/comfyanonymous/ComfyUI /opt/ComfyUI',
                          f'git -C /opt/ComfyUI checkout --quiet {COMFY_REVISION}',
                          'git clone --quiet https://github.com/leejet/ComfyUI-GGUF /opt/ComfyUI/custom_nodes/ComfyUI-GGUF',
                          f'git -C /opt/ComfyUI/custom_nodes/ComfyUI-GGUF checkout --quiet {GGUF_BASE_REVISION}',
                          'pip install --no-cache-dir -r /opt/ComfyUI/requirements.txt',
                          'pip install --no-cache-dir -r /opt/ComfyUI/custom_nodes/ComfyUI-GGUF/requirements.txt',
                          'pip install --no-cache-dir transformers==4.57.6'))
    return (base.pip_install('websocket-client==1.9.0', 'huggingface-hub>=0.36')
            .run_commands(
                f'git -C /opt/ComfyUI/custom_nodes/ComfyUI-GGUF fetch origin {GGUF_REVISION}',
                f'git -C /opt/ComfyUI/custom_nodes/ComfyUI-GGUF checkout --quiet {GGUF_REVISION}')
            .env({'HF_HOME': '/models/qwen-image', 'PYTHONPATH': '/opt'})
            # include_source=False 时容器仍要能 import 本模块，否则函数无法 hydrate。
            .add_local_file(Path(__file__).resolve(), '/opt/aladin_modal_app.py', copy=True)
            .add_local_file(Path(__file__).resolve().parent / 'request.py',
                            '/opt/request.py', copy=True)
            # 只挂 worker.py。挂整个 aladin/ 会让镜像在每次改 web 代码时失效重建。
            .add_local_file(Path(__file__).resolve().parent / 'aladin' / 'worker.py',
                            '/opt/aladin/worker.py', copy=True))


app = modal.App('aladin-image-v1', include_source=False)
cache = modal.Volume.from_name('agent-media-lab-gpu-models-v1', create_if_missing=True)
results = modal.Volume.from_name('aladin-image-results-v1', create_if_missing=True)


@app.function(image=_image(), gpu='L40S', cpu=4, memory=32768, timeout=3600,
              startup_timeout=1200, retries=0, min_containers=0, max_containers=1,
              scaledown_window=2, volumes={'/models': cache, '/results': results})
def generate(request: dict, source: bytes = b'') -> dict:
    """跑一个受限的生图请求；产物写 Volume，返回值保持 JSON 原语。

    返回值刻意不含 torch 类型：容器返回值曾多次无法在本地反序列化（实测），
    权威产物一律以 Volume 里的 result.json 为准。

    `source` 是输入图的原始字节（图生图 / 指令编辑用）。它不进请求 JSON：
    图片塞进 JSONB 会让每行任务挂上十几 MB，也会随返回值一起被序列化。
    容器按 request['inputSha256'] 校验它，不符即拒绝。
    """
    from aladin import worker

    try:
        record = worker.execute(dict(request), '/results', source, commit=results.commit)
    except Exception as error:
        # 版本不一致时把两侧哈希都带出来，便于判断是哪边旧
        raise RuntimeError(
            f'{error} | container_worker={worker.revision()[:16]} '
            f'request={str(request.get("workerRevision"))[:16]}') from None
    results.commit()
    worker.done(record)
    return {'key': request['key'], 'images': record['images'],
            'elapsedSeconds': record['elapsedSeconds']}


@app.function(image=_image(), timeout=120)
def worker_probe() -> dict:
    """运维用：报告容器里实际跑的 worker.py 与本地是否一致。"""
    import hashlib
    from pathlib import Path

    from aladin import worker

    module_file = Path(worker.__file__).resolve()
    data = module_file.read_bytes()
    # 用 pipeline 会发出的那种请求走一遍校验，直接看真实报错
    try:
        worker.validate({'workerRevision': hashlib.sha256(data).hexdigest()})
        verdict = 'unexpectedly passed'
    except Exception as error:
        verdict = type(error).__name__ + ': ' + str(error)[:200]
    return {'revision': worker.revision(),
            'module_file': str(module_file),
            'sha256': hashlib.sha256(data).hexdigest(),
            'bytes': len(data),
            'is_current': b'deployed=' in data,
            'verdict': verdict}
