"""Modal 内的 10Eros-Max beta5 Turbo 图生视频工作流。

使用 ComfyUI 原生 MiniMax-H3 节点，联合生成 24fps 视频和音频。
权重和依赖固定版本；WebM 产物保持现有 API/存储契约。
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

PREFIX = '[aladin-progress]'

# 与 aladin_modal_app.py / aladin/video_worker.py 钉的同一批提交：镜像层共享，
# 容器会拿这两个值跟自己比对，不一致直接拒绝。
COMFY_REVISION = 'c194dd00cd42aa18d9dbf27d977bf6b85d9ea565'
GGUF_REVISION = 'f912d5e5c25921e41eae2c0131eeb4d350e7c165'

MODEL_REPO = 'TenStrip/10Eros-Max'
MODEL_REV = '8a198588c8870ab0d613b3492a3150d091c8c2dd'
SUPPORT_REPO = 'Comfy-Org/MiniMax-H3'
SUPPORT_REV = '0fea91688aefb62d4eb94d5952f277f46c298284'
MODEL = '10eros-max-h3-turbo-beta5'
TRANSFORMER = '10Eros_Max_h3_TURBO-hybrid_beta5_int8.safetensors'
MODELS = {MODEL: {
    'repo': MODEL_REPO, 'revision': MODEL_REV, 'transformer': TRANSFORMER,
    'encoder': 'qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors',
    'vae': 'minimax_h3_video_vae_fp16.safetensors',
    'audio_vae': 'minimax_h3_audio_vae_fp32.safetensors',
    'files': [
        (MODEL_REPO, MODEL_REV, TRANSFORMER, 'diffusion_models/' + TRANSFORMER, 20970414464),
        (SUPPORT_REPO, SUPPORT_REV, 'text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors',
         'text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors', 15687142551),
        (SUPPORT_REPO, SUPPORT_REV, 'vae/minimax_h3_video_vae_fp16.safetensors',
         'vae/minimax_h3_video_vae_fp16.safetensors', 5207808496),
        (SUPPORT_REPO, SUPPORT_REV, 'vae/minimax_h3_audio_vae_fp32.safetensors',
         'vae/minimax_h3_audio_vae_fp32.safetensors', 605254808),
    ],
}}
SAMPLERS = ['euler', 'euler_ancestral', 'dpmpp_2m', 'dpmpp_2m_sde', 'uni_pc',
            'res_multistep', 'er_sde', 'lcm']
SCHEDULERS = ['simple', 'normal', 'beta']
MAX_PROMPT = 2000
MIN_FRAMES, MAX_FRAMES = 22, 192
FPS = 24
SIZE_MIN, SIZE_MAX, SIZE_STEP = 256, 1344, 32
STEPS_MAX = 40
MAX_VIDEO = 192 * 1024 * 1024
WEIGHT_MANIFEST = 'weights-h3.json'
ROOT = Path('/models/h3')
# Turbo delta 已包含在 checkpoint 中，不能叠加旧 Wan lightx2v LoRA。
DEFAULTS = {'steps': 6, 'cfg': 1.0, 'shift': 12.0, 'width': 832, 'height': 480,
            'frames': 124, 'seed': 0, 'sampler': 'res_multistep', 'scheduler': 'simple'}


def revision() -> str:
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def say(message: str) -> None:
    print(message, flush=True)


def progress(event: dict) -> None:
    """结构化进度行：宿主 daemon 靠它更新 UI，所以格式必须稳定。"""
    print(PREFIX + ' ' + json.dumps(event, ensure_ascii=False), flush=True)


def atomic_json(path: Path, value) -> None:
    temporary = Path(str(path) + '.tmp')
    with temporary.open('w') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


# --- 采样进度观察 ----------------------------------------------------------

def classify(graph: dict) -> dict:
    """单个 H3 联合音视频采样器的进度。"""
    return {node_id: (1, 1) for node_id, spec in graph.items()
            if spec.get('class_type') == 'SamplerCustomAdvanced'}


def ws_url(server_url: str) -> str:
    """把 HTTP base URL 转成 WebSocket URL（websocket-client 只接受 ws/wss）。"""
    if server_url.startswith('https://'):
        return 'wss://' + server_url[len('https://'):]
    if server_url.startswith('http://'):
        return 'ws://' + server_url[len('http://'):]
    return server_url


def progress_info(event: dict, prompt_id: str, mapping: dict) -> dict | None:
    if not isinstance(event, dict) or event.get('type') != 'progress':
        return None
    data = event.get('data')
    if not isinstance(data, dict) or data.get('prompt_id') != prompt_id:
        return None
    node = data.get('node')
    if node not in mapping:
        return None
    image, total = mapping[node]
    return {'image': image, 'total': total, 'step': data.get('value'),
            'max': data.get('max'), 'node': node}


class WatchHandle:
    """正在运行的观察者。`ready` 在 WebSocket 订阅建立后才置位。"""

    def __init__(self):
        self.ready = threading.Event()
        self.done = threading.Event()
        self._emitted = 0
        self.counts: dict = {}

    def _finish(self, emitted: int) -> None:
        self._emitted = emitted
        self.done.set()

    def wait(self, timeout: float | None = None) -> int:
        self.done.wait(timeout)
        return self._emitted


def watch_async(server_url: str, prompt_id: str, mapping: dict,
                timeout: float = 1800.0, client_id: str = '') -> WatchHandle:
    """订阅 ComfyUI 的 WebSocket，把属于本任务的采样进度转成结构化 stdout 行。"""
    try:
        import websocket  # websocket-client
    except ImportError as error:
        raise RuntimeError('需要 websocket-client；Modal 镜像里要加入该依赖') from error

    handle = WatchHandle()
    emitted = [0]

    def on_open(_ws):
        handle.ready.set()

    def on_message(_ws, raw):
        if isinstance(raw, (bytes, bytearray)):
            return
        try:
            event = json.loads(raw)
        except (ValueError, TypeError):
            return
        kind = event.get('type') if isinstance(event, dict) else None
        handle.counts[kind] = handle.counts.get(kind, 0) + 1
        info = progress_info(event, prompt_id, mapping)
        if info is None:
            return
        progress(dict(info, kind='step'))
        emitted[0] += 1

    url = ws_url(server_url).rstrip('/') + '/ws'
    if client_id:
        url += '?clientId=' + client_id
    ws = websocket.WebSocketApp(url, on_open=on_open, on_message=on_message)

    def run():
        try:
            ws.run_forever(ping_interval=20, ping_timeout=10)
        finally:
            handle._finish(emitted[0])

    threading.Thread(target=run, daemon=True).start()

    def expire():
        if handle.done.wait(timeout):
            return
        ws.close()

    threading.Thread(target=expire, daemon=True).start()
    return handle


# --- 请求校验与工作流 ------------------------------------------------------

def validate(request: dict) -> dict:
    if not isinstance(request, dict):
        raise ValueError('Request must be an object')
    if request.get('workerRevision') != revision():
        raise ValueError(
            'Video worker revision mismatch; deploy the current worker first '
            '(request=' + str(request.get('workerRevision'))[:16] +
            ' deployed=' + revision()[:16] + ')')
    spec = MODELS.get(request.get('model'))
    if spec is None:
        raise ValueError('Unsupported model: ' + str(request.get('model')))
    if request.get('modelRevision') != spec['revision']:
        raise ValueError('Pinned model revision mismatch')
    if request.get('comfyRevision') != COMFY_REVISION or request.get('ggufRevision') != GGUF_REVISION:
        raise ValueError('Pinned ComfyUI/GGUF revision mismatch')
    if not re.fullmatch('[a-f0-9]{64}', str(request.get('key', ''))):
        raise ValueError('Invalid key')
    prompt = request.get('prompt', '')
    # 图生视频允许空提示词：输入图本身已经承载了内容，提示词只描述想要的运动。
    if not isinstance(prompt, str) or len(prompt) > MAX_PROMPT:
        raise ValueError('Prompt bounds')
    negative = request.get('negative', '')
    if not isinstance(negative, str) or len(negative) > MAX_PROMPT:
        raise ValueError('Negative prompt bounds')
    for field in ('width', 'height'):
        value = request.get(field)
        if type(value) is not int or not SIZE_MIN <= value <= SIZE_MAX or value % SIZE_STEP:
            raise ValueError(field + ' bounds')
    frames = request.get('frames')
    if type(frames) is not int or not MIN_FRAMES <= frames <= MAX_FRAMES or (frames - 5) % 17:
        raise ValueError('Frame count bounds')
    seed = request.get('seed')
    if type(seed) is not int or not 0 <= seed < 2 ** 63 - 1:
        raise ValueError('Seed bounds')
    steps = request.get('steps')
    if type(steps) is not int or not 1 <= steps <= STEPS_MAX:
        raise ValueError('Steps bounds')
    cfg = request.get('cfg')
    if type(cfg) not in (int, float) or not math.isfinite(cfg) or not 0 <= cfg <= 10:
        raise ValueError('CFG bounds')
    shift = request.get('shift')
    if type(shift) not in (int, float) or not math.isfinite(shift) or not 0.01 <= shift <= 20:
        raise ValueError('Shift bounds')
    strength = request.get('loraStrength', 1.0)
    if type(strength) not in (int, float) or strength != 1.0:
        raise ValueError('10Eros-Max Turbo has baked-in acceleration; loraStrength must remain 1')
    if request.get('sampler') not in SAMPLERS:
        raise ValueError('Unsupported sampler')
    if request.get('scheduler') not in SCHEDULERS:
        raise ValueError('Unsupported scheduler')
    if not re.fullmatch('[a-f0-9]{64}', str(request.get('inputSha256', ''))):
        raise ValueError('Input image hash required')
    return request


def workflow(request: dict) -> dict:
    """原生 H3 图像条件、联合音视频采样与解码；默认 CFG=1 使用 BasicGuider。"""
    spec = MODELS[request['model']]
    graph = {
        'model': {'class_type': 'UNETLoader', 'inputs': {
            'unet_name': spec['transformer'], 'weight_dtype': 'default'}},
        'shift': {'class_type': 'MiniMaxH3SigmaShift', 'inputs': {
            'model': ['model', 0], 'shift_video': request['shift'], 'shift_audio': 3.0}},
        'input': {'class_type': 'LoadImage', 'inputs': {'image': request['inputName']}},
        'encoder': {'class_type': 'CLIPLoader', 'inputs': {
            'clip_name': spec['encoder'], 'type': 'minimax', 'device': 'default'}},
        'vae': {'class_type': 'VAELoader', 'inputs': {'vae_name': spec['vae']}},
        'audio_vae': {'class_type': 'VAELoader', 'inputs': {'vae_name': spec['audio_vae']}},
        'conditioning': {'class_type': 'MiniMaxH3ImageToVideo', 'inputs': {
            'clip': ['encoder', 0], 'vae': ['vae', 0], 'prompt': request['prompt'],
            'width': request['width'], 'height': request['height'],
            'length': request['frames'], 'first_frame': ['input', 0]}},
        'guider': {'class_type': 'BasicGuider', 'inputs': {
            'model': ['shift', 0], 'conditioning': ['conditioning', 0]}},
        'noise': {'class_type': 'RandomNoise', 'inputs': {'noise_seed': request['seed']}},
        'sampler': {'class_type': 'KSamplerSelect', 'inputs': {'sampler_name': request['sampler']}},
        'sigmas': {'class_type': 'BasicScheduler', 'inputs': {
            'model': ['shift', 0], 'scheduler': request['scheduler'],
            'steps': request['steps'], 'denoise': 1.0}},
        'sample': {'class_type': 'SamplerCustomAdvanced', 'inputs': {
            'noise': ['noise', 0], 'guider': ['guider', 0], 'sampler': ['sampler', 0],
            'sigmas': ['sigmas', 0], 'latent_image': ['conditioning', 1]}},
        'decode': {'class_type': 'VAEDecode', 'inputs': {'samples': ['sample', 0], 'vae': ['vae', 0]}},
        'decode_audio': {'class_type': 'VAEDecodeAudio', 'inputs': {
            'samples': ['sample', 0], 'vae': ['audio_vae', 0]}},
        'video': {'class_type': 'CreateVideo', 'inputs': {
            'images': ['decode', 0], 'audio': ['decode_audio', 0], 'fps': float(FPS)}},
        'save': {'class_type': 'SaveVideo', 'inputs': {
            'video': ['video', 0], 'filename_prefix': 'aladin-' + request['key'][:12],
            'format': 'mp4', 'format.codec': 'h264'}},
    }
    if request['cfg'] != 1.0:
        graph['negative'] = {'class_type': 'MiniMaxH3ImageToVideo', 'inputs':
                             dict(graph['conditioning']['inputs'], prompt=request['negative'])}
        graph['guider'] = {'class_type': 'CFGGuider', 'inputs': {
            'model': ['shift', 0], 'positive': ['conditioning', 0],
            'negative': ['negative', 0], 'cfg': request['cfg']}}
    return graph


# --- 权重 ------------------------------------------------------------------

def cached(manifest: dict, root: Path) -> bool:
    if manifest.get('model') != MODEL or manifest.get('revision') != MODEL_REV:
        return False
    # 按当前钉版验，而不是按 manifest 里记的那套：换量化后旧文件还在，
    # 用旧清单判会一直命中、新权重永远下不下来（生图那边实测踩过）。
    for _repo, _revision, _hub, local, size in MODELS[MODEL]['files']:
        path = root / local
        if not path.is_file() or path.stat().st_size != size:
            return False
    return True


def ensure_weights(root: Path = ROOT) -> dict:
    """确保全套权重在本地，返回 manifest。仓库内路径带前缀，下载后统一归位。"""
    manifest_path = root / WEIGHT_MANIFEST
    manifest = json.loads(manifest_path.read_text()) if manifest_path.is_file() else {}
    if cached(manifest, root):
        say('weight cache hit for ' + MODEL)
        return manifest
    import shutil
    from huggingface_hub import hf_hub_download
    root.mkdir(parents=True, exist_ok=True)
    files = []
    for repo, revision, hub, local, size in MODELS[MODEL]['files']:
        target = root / local
        if target.is_file() and target.stat().st_size == size:
            files.append({'hub': hub, 'path': local, 'bytes': size})
            continue
        say(f'downloading {hub} ({size / 1e9:.2f} GB)')
        downloaded = Path(hf_hub_download(repo_id=repo, filename=hub, revision=revision,
                                          local_dir=str(root)))
        if downloaded.resolve() != target.resolve():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(downloaded), str(target))
        if target.stat().st_size != size:
            raise ValueError('Downloaded weight size mismatch: ' + hub)
        files.append({'hub': hub, 'path': local, 'bytes': target.stat().st_size})
    manifest = {'model': MODEL, 'repo': MODELS[MODEL]['repo'],
                'revision': MODELS[MODEL]['revision'], 'comfyRevision': COMFY_REVISION,
                'ggufNode': GGUF_REVISION, 'files': files,
                'verifiedAt': round(time.time(), 3)}
    atomic_json(manifest_path, manifest)
    return manifest


def extra_model_paths(root: Path) -> Path:
    path = Path('/tmp/extra_model_paths.yaml')
    path.write_text('aladin:\n    base_path: ' + str(root) +
                    '\n    diffusion_models: diffusion_models'
                    '\n    text_encoders: text_encoders\n    vae: vae'
                    '\n    loras: loras\n')
    return path


def start_server(root: Path):
    log = open('/tmp/comfy-server.log', 'w')
    process = subprocess.Popen(
        ['python', 'main.py', '--listen', '127.0.0.1', '--port', '8188',
         '--disable-auto-launch',
         '--extra-model-paths-config', str(extra_model_paths(root)),
         '--output-directory', '/tmp/comfy/output',
         # 不把 workflow / 提示词写进产物文件：下载或转发出去的图和视频不带创作信息
         '--disable-metadata',
         '--temp-directory', '/tmp/comfy/temp',
         '--input-directory', '/tmp/comfy/input'],
        cwd='/opt/ComfyUI', stdout=log, stderr=subprocess.STDOUT)
    deadline = time.time() + 600
    while time.time() < deadline:
        try:
            urllib.request.urlopen(SERVER + '/system_stats', timeout=2).read()
            return process, log
        except Exception:
            if process.poll() is not None:
                raise RuntimeError('ComfyUI 进程退出；日志尾部: ' + str(tail('/tmp/comfy-server.log', 5)))
            time.sleep(1)
    raise RuntimeError('ComfyUI 未能在 600s 内就绪')


def tail(path: str, lines: int) -> list:
    try:
        return Path(path).read_text(errors='replace').splitlines()[-lines:]
    except FileNotFoundError:
        return []


def submit(graph: dict, client_id: str, prompt_id: str) -> str:
    payload = json.dumps({'prompt': graph, 'client_id': client_id,
                          'prompt_id': prompt_id}).encode()
    request = urllib.request.Request(SERVER + '/prompt', data=payload,
                                     headers={'Content-Type': 'application/json'})
    try:
        body = urllib.request.urlopen(request, timeout=60).read()
    except urllib.error.HTTPError as error:
        detail = error.read().decode('utf-8', errors='replace')[:1500]
        raise RuntimeError(f'ComfyUI 拒绝该工作流：HTTP {error.code} {detail}') from None
    return json.loads(body).get('prompt_id', prompt_id)


def wait_for_history(prompt_id: str, deadline: float) -> dict | None:
    while time.time() < deadline:
        try:
            history = json.loads(urllib.request.urlopen(
                SERVER + '/history/' + prompt_id, timeout=10).read())
        except Exception:
            history = {}
        if history:
            return history
        time.sleep(1.0)
    return None


def fetch_video(prefix: str, deadline: float) -> bytes:
    """ComfyUI 保存带音轨的 MP4，再转 WebM，保留当前产物/浏览器契约。"""
    directory = Path('/tmp/comfy/output')
    while time.time() < deadline:
        matches = sorted(directory.glob(prefix + '*.mp4'), key=lambda p: p.stat().st_mtime)
        if matches:
            source = matches[-1]
            target = source.with_suffix('.webm')
            subprocess.run(['ffmpeg', '-nostdin', '-y', '-loglevel', 'error',
                            '-i', str(source), '-map', '0:v:0', '-map', '0:a:0',
                            '-c:v', 'libvpx-vp9', '-deadline', 'realtime', '-cpu-used', '4',
                            '-crf', '32', '-b:v', '0', '-pix_fmt', 'yuv420p',
                            '-c:a', 'libopus', '-b:a', '128k', str(target)],
                           check=True, timeout=240, stdout=subprocess.DEVNULL)
            return target.read_bytes()
        time.sleep(1.0)
    raise RuntimeError('没有产出视频文件：' + prefix)


def receipt_hit(output: Path, request: dict) -> dict | None:
    """回执短路：同参数重跑直接返回已有产物，不碰 GPU。"""
    receipt = output / 'result.json'
    if not receipt.is_file():
        return None
    try:
        cached_record = json.loads(receipt.read_text())
    except ValueError:
        return None
    if cached_record.get('request') != request:
        return None
    for item in cached_record.get('videos', []):
        path = output / item.get('file', '')
        if not path.is_file() or digest(path.read_bytes()) != item.get('sha256'):
            say('回执存在但产物不符；重新生成')
            return None
    return cached_record


def write_input(source: bytes, expected_sha: str) -> str:
    """把输入图写进 ComfyUI 的 input 目录，并再验一次哈希。"""
    if digest(source) != expected_sha:
        raise ValueError('Input image hash mismatch')
    if source[:8] == b'\x89PNG\r\n\x1a\n':
        extension = '.png'
    elif source[:3] == b'\xff\xd8\xff':
        extension = '.jpg'
    else:
        raise ValueError('Input image must be PNG or JPEG')
    name = 'input-' + expected_sha[:16] + extension
    directory = Path('/tmp/comfy/input')
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    if not path.is_file():
        path.write_bytes(source)
    return name


SERVER = 'http://127.0.0.1:8188'


def execute(request: dict, result_root: str, source: bytes = b'') -> dict:
    request = validate(json.loads(json.dumps(request)))
    started = time.time()
    spec = MODELS[request['model']]
    ensure_weights(ROOT)
    output = Path(result_root) / request['key']
    output.mkdir(parents=True, exist_ok=True)
    # 文件名由哈希决定，每次运行一致。必须在回执比对之前算好：它会被写进
    # record['request']，两次运行的 request 不同的话缓存永远命中不了。
    request['inputName'] = write_input(source, request['inputSha256'])
    say('input image ' + request['inputName'] + ' (' + str(len(source)) + ' bytes)')
    hit = receipt_hit(output, request)
    if hit is not None:
        say('receipt hit; returning cached result')
        return hit

    process, _log = start_server(ROOT)
    try:
        graph = workflow(request)
        client_id = 'aladin-' + request['key'][:16]
        prompt_id = str(uuid.uuid4())          # ComfyUI 只接受规范 UUID
        handle = watch_async(SERVER, prompt_id, classify(graph), timeout=2400.0,
                             client_id=client_id)
        if not handle.ready.wait(timeout=30):
            raise RuntimeError('WebSocket 订阅未能在 30s 内建立')
        progress({'kind': 'queued', 'job_key': request['key']})
        submit(graph, client_id, prompt_id)

        history = wait_for_history(prompt_id, time.time() + 2400)
        if history is None:
            raise RuntimeError('ComfyUI 未在期限内产出结果')
        entry = history[prompt_id]
        if entry.get('status', {}).get('status_str') == 'error':
            raise RuntimeError('ComfyUI 执行失败: ' + json.dumps(entry.get('status'))[:500])

        prefix = 'aladin-' + request['key'][:12]
        data = fetch_video(prefix, time.time() + 300)
        if len(data) > MAX_VIDEO:
            raise ValueError('视频超出上限')
        name = 'video-01.webm'
        temporary = output / (name + '.tmp')
        temporary.write_bytes(data)
        temporary.replace(output / name)
        elapsed = round(time.time() - started, 2)
        record = {'schemaVersion': 1, 'request': request, 'model': spec['repo'],
                  'elapsedSeconds': elapsed, 'quantization': spec['transformer'],
                  'videos': [{'index': 1, 'file': name, 'sha256': digest(data),
                              'bytes': len(data), 'format': 'webm', 'fps': FPS,
                              'frames': request['frames'],
                              'prompt': request['prompt'], 'seed': request['seed']}],
                  'serverLogTail': tail('/tmp/comfy-server.log', 8)}
        atomic_json(output / 'result.json', record)
        progress({'kind': 'encoded', 'bytes': len(data), 'seconds': elapsed})
        return record
    except Exception as error:
        raise RuntimeError(str(error) + ' | ComfyUI 日志尾部: ' +
                           ' / '.join(tail('/tmp/comfy-server.log', 6))) from None
    finally:
        process.terminate()
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            process.kill()
