"""把 watchdog 放进真实容器验证采样步级进度。

部署形态刻意与 aladin 的生产形态接近：
- 每个请求一个容器（`min_containers=0`、`max_containers=1`）
- 只把 watchdog.py 带进镜像，不挂载仓库
- 用最小的 stock SD1.5 checkpoint（2.13 GB），不碰 Qwen-Image 的 14.63 GB

镜像层与 `agent-media-lab` 的 `agent-media-lab-image-v1` 前缀一致，
以便复用其已缓存的 apt/pip/git 层；只在末尾追加 websocket-client。
"""
from pathlib import Path

import modal

COMFY_REVISION = 'c194dd00cd42aa18d9dbf27d977bf6b85d9ea565'
GGUF_REVISION = 'f912d5e5c25921e41eae2c0131eeb4d350e7c165'
CHECKPOINT = 'v1-5-pruned-emaonly-fp16.safetensors'
CHECKPOINT_URL = ('https://huggingface.co/Comfy-Org/stable-diffusion-v1-5-archive/'
                  'resolve/main/' + CHECKPOINT)
HEARTBEAT = '[aladin-progress]'


def _image() -> modal.Image:
    # 与 agent-media-lab-image-v1 的前缀逐字一致，最大化镜像层复用。
    base = (modal.Image.debian_slim(python_version='3.11')
            .apt_install('git')
            .pip_install('torch==2.8.0', 'torchvision==0.23.0', 'torchaudio==2.8.0',
                         index_url='https://download.pytorch.org/whl/cu128')
            .run_commands('git clone --quiet https://github.com/comfyanonymous/ComfyUI /opt/ComfyUI',
                          f'git -C /opt/ComfyUI checkout --quiet {COMFY_REVISION}',
                          'git clone --quiet https://github.com/leejet/ComfyUI-GGUF /opt/ComfyUI/custom_nodes/ComfyUI-GGUF',
                          f'git -C /opt/ComfyUI/custom_nodes/ComfyUI-GGUF checkout --quiet {GGUF_REVISION}',
                          'pip install --no-cache-dir -r /opt/ComfyUI/requirements.txt',
                          'pip install --no-cache-dir -r /opt/ComfyUI/custom_nodes/ComfyUI-GGUF/requirements.txt',
                          'pip install --no-cache-dir transformers==4.57.6'))
    # 追加层：watchdog 的 WebSocket 依赖。放在最后，避免让上面昂贵的层失效。
    # 保持 include_source=False 的同时让容器能 import watchdog 与本模块：
    # 只挂 probe/ 这一层，不把整个仓库带进容器。
    return (base.pip_install('websocket-client==1.9.0')
            .env({'HF_HOME': '/models/hf', 'PYTHONPATH': '/opt/probe'})
            .add_local_dir(Path(__file__).resolve().parent, '/opt/probe', copy=True))

app = modal.App('aladin-probe-v1', include_source=False)
cache = modal.Volume.from_name('agent-media-lab-gpu-models-v1', create_if_missing=True)
results = modal.Volume.from_name('aladin-probe-results-v1', create_if_missing=True)


def workflow_graph():
    """与 image_worker.branch() 同构的最小工作流，用来复现同一套进度语义。"""
    return {
        'model': {'class_type': 'CheckpointLoaderSimple',
                  'inputs': {'ckpt_name': CHECKPOINT}},
        'pos': {'class_type': 'CLIPTextEncode',
                'inputs': {'text': 'a lone lighthouse at dusk, calm sea',
                           'clip': ['model', 1]}},
        'neg': {'class_type': 'CLIPTextEncode',
                'inputs': {'text': '', 'clip': ['model', 1]}},
        'latent': {'class_type': 'EmptyLatentImage',
                   'inputs': {'width': 512, 'height': 512, 'batch_size': 1}},
        'sampler0': {'class_type': 'KSampler',
                     'inputs': {'seed': 0, 'steps': 25, 'cfg': 8.0,
                                'sampler_name': 'euler', 'scheduler': 'simple',
                                'denoise': 1.0, 'model': ['model', 0],
                                'positive': ['pos', 0], 'negative': ['neg', 0],
                                'latent_image': ['latent', 0]}},
        'decode': {'class_type': 'VAEDecode',
                   'inputs': {'samples': ['sampler0', 0], 'vae': ['model', 2]}},
        'save0': {'class_type': 'SaveImage',
                  'inputs': {'filename_prefix': 'probe-01', 'images': ['decode', 0]}},
    }


def _download_resumable(url: str, destination: Path, log) -> None:
    """标准库实现的可续传下载。

    镜像里没有 curl；改 apt 装会打破已缓存的昂贵层，故用 urllib。
    按 Range 续传，网络中断后不必重下 2.1GB。
    """
    import urllib.error
    import urllib.request

    existing = destination.stat().st_size if destination.is_file() else 0
    request = urllib.request.Request(url)
    if existing:
        request.add_header('Range', f'bytes={existing}-')
        log(f'续传 checkpoint：已有 {existing} bytes')
    try:
        response = urllib.request.urlopen(request, timeout=120)
    except urllib.error.HTTPError as error:
        if error.code == 416:
            return  # 已完整
        raise
    resumed = existing > 0 and response.status == 206
    mode = 'ab' if resumed else 'wb'
    total = int(response.headers.get('Content-Length') or 0) + (existing if resumed else 0)
    written = existing if resumed else 0
    log(f'下载 checkpoint 约 {total // 1_000_000} MB')
    with open(destination, mode) as handle:
        while True:
            chunk = response.read(1 << 20)
            if not chunk:
                break
            handle.write(chunk)
            written += len(chunk)
            if total and written % (200 * 1_000_000) < (1 << 20):
                log(f'  已下载 {written // 1_000_000} / {total // 1_000_000} MB')


@app.function(image=_image(), gpu='L4', cpu=4, memory=32768, timeout=1800,
              startup_timeout=1200, retries=0, min_containers=0, max_containers=1,
              scaledown_window=2, volumes={'/models': cache, '/results': results})
def sampling_progress_probe() -> dict:
    import json
    import subprocess
    import threading
    import time
    import uuid
    import urllib.request
    from pathlib import Path

    import watchdog  # 来自挂载的 probe/

    def say(message):
        print(message, flush=True)

    destination = Path('/models/checkpoints') / CHECKPOINT
    destination.parent.mkdir(parents=True, exist_ok=True)
    _download_resumable(CHECKPOINT_URL, destination, say)
    cache.commit()
    size = destination.stat().st_size
    if size < 2_000_000_000:
        raise RuntimeError(f'checkpoint 不完整：{size} bytes')
    say(f'checkpoint {size} bytes')

    # ComfyUI 默认只扫自己的 models/；必须显式告知 Volume 里的模型路径。
    config = Path('/tmp/extra_model_paths.yaml')
    config.write_text('probe:\n    base_path: /models\n'
                      '    checkpoints: checkpoints\n    diffusion_models: diffusion_models\n'
                      '    text_encoders: text_encoders\n    vae: vae\n')
    server = subprocess.Popen(
        ['python', 'main.py', '--listen', '127.0.0.1', '--port', '8188',
         '--disable-auto-launch', '--extra-model-paths-config', str(config),
         '--output-directory', '/tmp/comfy/output',
         '--temp-directory', '/tmp/comfy/temp', '--input-directory', '/tmp/comfy/input'],
        cwd='/opt/ComfyUI', stdout=open('/tmp/comfy-server.log', 'w'), stderr=subprocess.STDOUT)
    started = time.time()
    while time.time() - started < 300:
        try:
            urlopen_check = urllib.request.urlopen('http://127.0.0.1:8188/system_stats', timeout=2)
            urlopen_check.read()
            break
        except Exception:
            time.sleep(1)
    else:
        raise RuntimeError('ComfyUI 未能在 300s 内就绪')
    say(f'ComfyUI 就绪，用时 {time.time() - started:.1f}s')

    graph = workflow_graph()
    # ComfyUI 要求 prompt_id 是规范 UUID，自定义字符串会被 400 拒绝（实测）。
    client_prompt_id = str(uuid.uuid4())
    # 进度只发给与 extra_data['client_id'] 相同的那个 WebSocket 连接，
    # 所以观察者与提交必须用同一个 id（execution.py:736）。
    forwarder_id = 'aladin-forwarder'
    payload = json.dumps({'prompt': graph, 'client_id': forwarder_id,
                          'prompt_id': client_prompt_id}).encode()
    request = urllib.request.Request(
        'http://127.0.0.1:8188/prompt', data=payload,
        headers={'Content-Type': 'application/json'})

    progress_sample = []
    handle = watchdog.watch_async(
        'http://127.0.0.1:8188', client_prompt_id,
        watchdog.classify(graph), timeout=1200.0, on_line=progress_sample.append,
        capture_raw=True, client_id=forwarder_id)
    if not handle.ready.wait(timeout=30):
        raise RuntimeError('WebSocket 订阅未能在 30s 内建立')
    say('WebSocket 订阅已建立')

    submitted = time.time()
    try:
        body = urllib.request.urlopen(request, timeout=30).read()
    except urllib.error.HTTPError as error:
        detail = error.read().decode('utf-8', errors='replace')[:2000]
        raise RuntimeError(f'ComfyUI 拒绝该工作流：HTTP {error.code} {detail}') from None
    response = json.loads(body)
    say(f'已入队 prompt_id={response.get("prompt_id")}')

    # 轮询 /history 判断完成；同时让 watchdog 继续收集
    finished_at = None
    while time.time() - submitted < 900:
        try:
            history = json.loads(urllib.request.urlopen(
                f'http://127.0.0.1:8188/history/{client_prompt_id}', timeout=5).read())
        except Exception:
            history = {}
        if history:
            finished_at = time.time()
            break
        time.sleep(1.0)

    time.sleep(1.0)
    emitted = handle.wait(timeout=30)

    server.terminate()
    output = Path('/tmp/comfy/output')
    files = sorted(p.name for p in output.rglob('*.png')) if output.is_dir() else []
    tail = Path('/tmp/comfy-server.log').read_text(errors='replace').splitlines()[-5:]
    record = {
        'prompt_id': client_prompt_id,
        'progress_lines': len(progress_sample) or emitted,
        'ws_event_counts': dict(handle.counts),
        'ws_raw_sample': handle.seen[:6],
        'progress_sample': progress_sample,
        'sampling_seconds': None if finished_at is None else round(finished_at - submitted, 2),
        'images': files,
        'server_tail': tail,
    }
    Path('/results/probe.json').write_text(json.dumps(record, ensure_ascii=False, indent=2))
    results.commit()
    say('RESULT ' + json.dumps(record, ensure_ascii=False))
    return record


if __name__ == '__main__':
    with app.run():
        print(sampling_progress_probe.remote())
