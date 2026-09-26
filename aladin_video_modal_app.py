"""aladin 的视频 Modal app：LTX 图生视频与音频。一个文件部署两套，各自独立的 app 与权重 Volume：

    ALADIN_VIDEO_MODEL=ltx-2.3 modal deploy aladin_video_modal_app.py
    ALADIN_VIDEO_MODEL=ltx-2.5 modal deploy aladin_video_modal_app.py

LTX-2.5 的 HF 仓库需要先在网页上同意协议，下载用 Modal secret `aladin-hf`（含 HF_TOKEN）；
token 只在 Modal 里，不进代码与日志。2.3 的仓库是公开的，不挂 secret。

ComfyUI 钉在 v0.37.0（LTX-2.5 节点所需），与生图镜像的层不再共享。
产物 Volume 两套共用（任务 key 已含模型与钉版，不会撞）。
"""
import os
from pathlib import Path

import modal

MODEL = os.environ.get('ALADIN_VIDEO_MODEL', 'ltx-2.5')
COMFY_REVISION = '73c9bad4d21e7addbe1d13bc92eee0f1431b017d'
APPS = {'ltx-2.3': ('aladin-video-ltx23-v1', 'aladin-ltx23-models-v1', False),
        'ltx-2.5': ('aladin-video-ltx25-v1', 'aladin-ltx25-models-v1', True)}
if MODEL not in APPS:
    raise SystemExit('ALADIN_VIDEO_MODEL 需为 ' + ' / '.join(APPS))
APP_NAME, VOLUME_NAME, GATED = APPS[MODEL]


def _image() -> modal.Image:
    return (modal.Image.debian_slim(python_version='3.11')
            .apt_install('git', 'ffmpeg')
            .pip_install('torch==2.8.0', 'torchvision==0.23.0', 'torchaudio==2.8.0',
                         index_url='https://download.pytorch.org/whl/cu128')
            .run_commands('git clone --quiet https://github.com/comfyanonymous/ComfyUI /opt/ComfyUI',
                          f'git -C /opt/ComfyUI checkout --quiet {COMFY_REVISION}',
                          'pip install --no-cache-dir -r /opt/ComfyUI/requirements.txt')
            .pip_install('websocket-client==1.9.0', 'huggingface-hub>=0.36')
            .env({'HF_HOME': '/models/hf', 'PYTHONPATH': '/opt', 'ALADIN_VIDEO_MODEL': MODEL})
            .add_local_file(Path(__file__).resolve(), '/opt/aladin_video_modal_app.py', copy=True)
            # 只挂 video_worker.py：挂整个 aladin/ 会让镜像在每次改 web 代码时失效重建。
            .add_local_file(Path(__file__).resolve().parent / 'aladin' / 'video_worker.py',
                            '/opt/aladin/video_worker.py', copy=True))


app = modal.App(APP_NAME, include_source=False)
models = modal.Volume.from_name(VOLUME_NAME, create_if_missing=True)
results = modal.Volume.from_name('aladin-video-results-v1', create_if_missing=True)
secrets = [modal.Secret.from_name('aladin-hf')] if GATED else []


@app.function(image=_image(), gpu='L40S', cpu=8, memory=98304, timeout=3600,
              startup_timeout=1200, retries=0, min_containers=0, max_containers=1,
              scaledown_window=2, volumes={'/models': models, '/results': results},
              secrets=secrets)
def generate(request: dict, source: bytes = b'') -> dict:
    """跑一个受限的图生视频请求；产物写 Volume，返回值只带 JSON 原语。

    `source` 是输入图的原始字节。它不进请求 JSON：图片进 JSONB 会让每行任务
    挂上十几 MB，也会随返回值一起被序列化。容器按 request['inputSha256'] 校验。
    """
    from aladin import video_worker

    try:
        record = video_worker.execute(dict(request), '/results', source)
    except Exception as error:
        raise RuntimeError(
            f'{error} | container_worker={video_worker.revision()[:16]} '
            f'request={str(request.get("workerRevision"))[:16]}') from None
    results.commit()
    video_worker.done(record)
    return {'key': request['key'], 'videos': record['videos'],
            'elapsedSeconds': record['elapsedSeconds']}


@app.function(image=_image(), timeout=900)
def object_info(nodes: list[str]) -> dict:
    """运维用：报告容器里这些节点的真实输入名。

    工作流是按 ComfyUI 源码写的，但源码与容器的差异只有跑起来才知道；
    这个探针不需要 GPU、不下载权重，几秒钟就能把「输入名对不对」问清楚。
    """
    import json as _json
    import subprocess
    import time
    import urllib.request

    log = open('/tmp/comfy-nodes.log', 'w')
    process = subprocess.Popen(
        ['python', 'main.py', '--listen', '127.0.0.1', '--port', '8188',
         '--disable-auto-launch', '--cpu'],
        cwd='/opt/ComfyUI', stdout=log, stderr=subprocess.STDOUT)
    try:
        deadline = time.time() + 300
        info = {}
        while time.time() < deadline:
            try:
                body = urllib.request.urlopen(
                    'http://127.0.0.1:8188/object_info', timeout=10).read()
                info = _json.loads(body)
                break
            except Exception:
                if process.poll() is not None:
                    raise RuntimeError('ComfyUI 退出: ' +
                                       ' / '.join(_tail('/tmp/comfy-nodes.log')))
                time.sleep(1)
        if not info:
            raise RuntimeError('ComfyUI 未在期限内就绪')
        missing = [name for name in nodes if name not in info]
        return {
            'missing': missing,
            'required': {name: info[name]['input']['required'] for name in nodes if name in info},
            'optional': {name: sorted(info[name]['input'].get('optional', {}))
                         for name in nodes if name in info},
        }
    finally:
        process.terminate()
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            process.kill()


def _tail(path: str, lines: int = 6) -> list:
    try:
        return Path(path).read_text(errors='replace').splitlines()[-lines:]
    except FileNotFoundError:
        return []


@app.function(image=_image(), timeout=120)
def worker_probe() -> dict:
    """运维用：报告容器里实际跑的 video_worker.py 与本地是否一致。"""
    import hashlib
    from pathlib import Path as _Path

    from aladin import video_worker

    data = _Path(video_worker.__file__).resolve().read_bytes()
    return {'revision': video_worker.revision(),
            'sha256': hashlib.sha256(data).hexdigest(),
            'bytes': len(data),
            'model': video_worker.MODEL,
            'weights': [item[3] for item in video_worker.MODELS[video_worker.MODEL]['files']]}


@app.function(image=_image(), timeout=3600, cpu=4, memory=8192, volumes={'/models': models},
              secrets=secrets)
def prepare_models() -> dict:
    """CPU 下载权重，避免 GPU 冷启动时为下载等待付费。"""
    from aladin import video_worker
    manifest = video_worker.ensure_weights(video_worker.MODEL)
    models.commit()
    return {'model': video_worker.MODEL, 'files': manifest['files']}


@app.function(image=_image(), timeout=3600, cpu=4, memory=8192, volumes={'/models': models},
              secrets=secrets)
def fetch_lora(repo: str, revision: str, hub: str, file: str, size: int, sha256: str) -> dict:
    """把 HF 上的 LoRA 直接下载进本 app 的模型 Volume（loras-sync 调用）。"""
    from aladin import video_worker
    result = video_worker.fetch_lora(repo, revision, hub, file, size, sha256)
    models.commit()
    return result
