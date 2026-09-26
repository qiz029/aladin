"""Modal 内的 LTX 图生视频工作流（LTX-2.3 与 LTX-2.5 两套，各自一个 Modal app）。

图照搬 Comfy-Org/workflow_templates 的 video_ltx2_3_i2v / video_ltx2_5_i2v：
半分辨率 8 步 → 潜空间 2× 放大 → 3 步精修，视频与音频联合生成，distilled 固定 CFG 1。
两套的差异（加载器、引导器、采样器、解码分块）都收在 MODELS 里。
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

# v0.37.0：LTX-2.5（PR #15499，v0.32.0 起）及其 int8 / Gemma4 修复都在内。
# 容器会拿它跟请求比对，不一致直接拒绝。
COMFY_REVISION = '73c9bad4d21e7addbe1d13bc92eee0f1431b017d'

LTX23_REV = '1d756cd27fa11c0896c4dfee093cd1bf36c7f7a1'      # Lightricks/LTX-2.3-fp8
LTX23_UPSCALER_REV = '5948be4ced3a4493d1f836df64378ff136ddb770'   # Lightricks/LTX-2.3
COMFY_LTX23_REV = 'f20f3a54378001e5e6d642acc84719cc78addf4b'      # Comfy-Org/ltx-2.3
COMFY_LTX2_REV = 'ccde4ba417d7900669fd56dd292a883cee11ff37'       # Comfy-Org/ltx-2
LTX25_REV = '5e6e71018ee1756ed329b697a7b4aedc934dfce9'      # Lightricks/LTX-2.5（需同意协议）

# 模板默认的负向提示词；distilled 固定 CFG 1 时它不参与采样，但引导器需要这路条件。
NEGATIVE = 'pc game, console game, video game, cartoon, childish, ugly'
STAGE1_SIGMAS = '1.0, 0.99375, 0.9875, 0.98125, 0.975, 0.909375, 0.725, 0.421875, 0.0'
STAGE2_SIGMAS = '0.85, 0.7250, 0.4219, 0.0'

MODELS = {
    'ltx-2.3': {
        'label': 'LTX-2.3', 'app': 'aladin-video-ltx23-v1', 'volume': 'aladin-ltx23-models-v1',
        'repo': 'Lightricks/LTX-2.3-fp8', 'revision': LTX23_REV,
        'checkpoint': 'ltx-2.3-22b-dev-fp8.safetensors',
        'text_encoder': 'gemma_3_12B_it_fp4_mixed.safetensors',
        'distill_lora': 'ltx_2.3_22b_distilled_1.1_lora_dynamic_fro09_avg_rank_111_bf16.safetensors',
        'upscaler': 'ltx-2.3-spatial-upscaler-x2-1.1.safetensors',
        'sampler': 'euler', 'decode': (768, 64, 4096, 4), 'gated': False,
        'files': [
            ('Lightricks/LTX-2.3-fp8', LTX23_REV, 'ltx-2.3-22b-dev-fp8.safetensors',
             'checkpoints/ltx-2.3-22b-dev-fp8.safetensors', 29145431166),
            ('Comfy-Org/ltx-2', COMFY_LTX2_REV, 'split_files/text_encoders/gemma_3_12B_it_fp4_mixed.safetensors',
             'text_encoders/gemma_3_12B_it_fp4_mixed.safetensors', 9447702218),
            ('Comfy-Org/ltx-2.3', COMFY_LTX23_REV,
             'split_files/loras/ltx_2.3_22b_distilled_1.1_lora_dynamic_fro09_avg_rank_111_bf16.safetensors',
             'loras/ltx_2.3_22b_distilled_1.1_lora_dynamic_fro09_avg_rank_111_bf16.safetensors', 2741024390),
            ('Lightricks/LTX-2.3', LTX23_UPSCALER_REV, 'ltx-2.3-spatial-upscaler-x2-1.1.safetensors',
             'latent_upscale_models/ltx-2.3-spatial-upscaler-x2-1.1.safetensors', 995743560),
        ],
    },
    'ltx-2.5': {
        'label': 'LTX-2.5', 'app': 'aladin-video-ltx25-v1', 'volume': 'aladin-ltx25-models-v1',
        'repo': 'Lightricks/LTX-2.5', 'revision': LTX25_REV,
        'transformer': 'ltx-2.5-22b-distilled-transformer-comfy-int8-convrot.safetensors',
        'text_encoder': 'gemma4-12b-with-proj-ltx-2.5-comfy-int8-convrot.safetensors',
        'vae': 'ltx-2.5-video-vae-bf16.safetensors', 'audio_vae': 'ltx-2.5-audio-vae-bf16.safetensors',
        'upscaler': 'ltx-2.5-latent-spatial-upscaler-x2-bf16-1.0.safetensors',
        'sampler': 'euler_ancestral', 'decode': (512, 64, 64, 16), 'gated': True,
        'files': [
            ('Lightricks/LTX-2.5', LTX25_REV,
             'diffusion_models/ltx-2.5-22b-distilled-transformer-comfy-int8-convrot.safetensors',
             'diffusion_models/ltx-2.5-22b-distilled-transformer-comfy-int8-convrot.safetensors', 21504034224),
            ('Lightricks/LTX-2.5', LTX25_REV,
             'text_encoders/gemma4-12b-with-proj-ltx-2.5-comfy-int8-convrot.safetensors',
             'text_encoders/gemma4-12b-with-proj-ltx-2.5-comfy-int8-convrot.safetensors', 15372969374),
            ('Lightricks/LTX-2.5', LTX25_REV, 'vae/ltx-2.5-video-vae-bf16.safetensors',
             'vae/ltx-2.5-video-vae-bf16.safetensors', 1472223346),
            ('Lightricks/LTX-2.5', LTX25_REV, 'vae/ltx-2.5-audio-vae-bf16.safetensors',
             'vae/ltx-2.5-audio-vae-bf16.safetensors', 364866540),
            ('Lightricks/LTX-2.5', LTX25_REV,
             'latent_upscale_models/ltx-2.5-latent-spatial-upscaler-x2-bf16-1.0.safetensors',
             'latent_upscale_models/ltx-2.5-latent-spatial-upscaler-x2-bf16-1.0.safetensors', 995778752),
        ],
    },
}
DEFAULT_MODEL = 'ltx-2.5'
# 容器里由部署时的环境变量决定本 app 跑哪一套；宿主侧不设，按请求里的 model 选。
MODEL = os.environ.get('ALADIN_VIDEO_MODEL', DEFAULT_MODEL)

MAX_PROMPT = 2000
MIN_FRAMES, MAX_FRAMES = 9, 481     # 8n+1；481 帧 = 20 秒（LTX-2.5 官方上限）
FPS = 24
# 第一段在半分辨率上采样，所以最终宽高要是 64 的倍数（半分辨率仍是 32 的倍数）
SIZE_MIN, SIZE_MAX, SIZE_STEP = 256, 1920, 64
MAX_VIDEO = 192 * 1024 * 1024
MAX_LORAS = 6
LORA_FILE = re.compile(r'^(civitai-\d+|hf-[0-9a-f]{16})\.safetensors$')
WEIGHT_MANIFEST = 'weights.json'
ROOT = Path('/models')
DEFAULTS = {'width': 1024, 'height': 576, 'frames': 121, 'seed': 0}


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


# 与 worker.py 的 Timings 同一份协议（本镜像只挂这一个文件，所以各留一份）：
# 导入时刻近似容器启动时刻，调用计数 > 0 说明复用了热容器。
CONTAINER_STARTED = time.time()
_CALLS = [0]


class Timings:
    """一次调用的分段耗时，写进 result.json 的 `timings`；节点耗时来自 `executing` 切换。"""

    def __init__(self):
        self.started = time.time()
        self.call_index = _CALLS[0]
        _CALLS[0] += 1
        self.last = self.started
        self.phases: dict = {}
        self.nodes: list = []
        self.classes: dict = {}
        self.prompt_id = None
        self._current = None
        progress({'kind': 'container', 'at': round(self.started, 3),
                  'callIndex': self.call_index,
                  'containerStartedAt': round(CONTAINER_STARTED, 3)})

    def mark(self, phase: str) -> None:
        now = time.time()
        self.phases[phase] = round(self.phases.get(phase, 0) + now - self.last, 2)
        self.last = now

    def watch(self, graph: dict, prompt_id: str) -> None:
        self.classes = {node: spec.get('class_type') for node, spec in graph.items()}
        self.prompt_id = prompt_id

    def on_event(self, event: dict) -> None:
        try:
            data = event.get('data') or {}
            if data.get('prompt_id') != self.prompt_id:
                return
            kind, now = event.get('type'), time.time()
            if kind == 'executing' or kind in ('execution_success', 'execution_error'):
                if self._current is not None:
                    node, began = self._current
                    self.nodes.append({'node': node, 'class': self.classes.get(node),
                                       'seconds': round(now - began, 2)})
                node = data.get('node') if kind == 'executing' else None
                self._current = (node, now) if node is not None else None
        except Exception:
            pass

    def record(self) -> dict:
        return {'containerId': os.environ.get('MODAL_TASK_ID', ''),
                'containerStartedAt': round(CONTAINER_STARTED, 3),
                'callIndex': self.call_index,
                'startedAt': round(self.started, 3), 'finishedAt': round(time.time(), 3),
                'phases': self.phases, 'nodes': self.nodes}


def done(record: dict) -> None:
    """结果已提交到 Volume：宿主看到这一行就立刻去取结果，不必等下一轮轮询。"""
    progress({'kind': 'done', 'at': round(time.time(), 3),
              'elapsedSeconds': record.get('elapsedSeconds')})


# --- 采样进度观察 ----------------------------------------------------------

def classify(graph: dict) -> dict:
    """两段采样各报一次进度：第一段 8 步生成，第二段 3 步精修。"""
    return {'sample1': (1, 2), 'sample2': (2, 2)}


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
                timeout: float = 1800.0, client_id: str = '', on_event=None) -> WatchHandle:
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
        if on_event is not None and isinstance(event, dict):
            on_event(event)
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

def validate_loras(loras) -> None:
    """容器只认白名单形状：固定命名的文件、64 位 sha256、有限的强度。目录与名称在宿主侧。"""
    if not isinstance(loras, list) or len(loras) > MAX_LORAS:
        raise ValueError('Invalid LoRA list')
    for item in loras:
        if not isinstance(item, dict) or set(item) != {'file', 'sha256', 'strength'}:
            raise ValueError('Invalid LoRA entry')
        if not isinstance(item['file'], str) or not LORA_FILE.match(item['file']):
            raise ValueError('Invalid LoRA file name')
        if not isinstance(item['sha256'], str) or not re.fullmatch(r'[0-9a-f]{64}', item['sha256']):
            raise ValueError('Invalid LoRA checksum')
        strength = item['strength']
        if isinstance(strength, bool) or not isinstance(strength, (int, float)) \
                or not math.isfinite(strength) or not -10 <= strength <= 10:
            raise ValueError('Invalid LoRA strength')


def validate(request: dict, model: str | None = None) -> dict:
    """model：本容器部署的那一套；宿主侧（测试）不传，只验请求自洽。"""
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
    if model is not None and request['model'] != model:
        raise ValueError('This app serves ' + model + ', not ' + str(request['model']))
    if request.get('modelRevision') != spec['revision']:
        raise ValueError('Pinned model revision mismatch')
    if request.get('comfyRevision') != COMFY_REVISION:
        raise ValueError('Pinned ComfyUI revision mismatch')
    if not re.fullmatch('[a-f0-9]{64}', str(request.get('key', ''))):
        raise ValueError('Invalid key')
    prompt = request.get('prompt', '')
    # 图生视频允许空提示词：输入图本身已经承载了内容，提示词只描述想要的运动。
    if not isinstance(prompt, str) or len(prompt) > MAX_PROMPT:
        raise ValueError('Prompt bounds')
    for field in ('width', 'height'):
        value = request.get(field)
        if type(value) is not int or not SIZE_MIN <= value <= SIZE_MAX or value % SIZE_STEP:
            raise ValueError(field + ' bounds')
    frames = request.get('frames')
    if type(frames) is not int or not MIN_FRAMES <= frames <= MAX_FRAMES or (frames - 1) % 8:
        raise ValueError('Frame count bounds')
    seed = request.get('seed')
    if type(seed) is not int or not 0 <= seed < 2 ** 63 - 1:
        raise ValueError('Seed bounds')
    validate_loras(request.get('loras', []))
    if not re.fullmatch('[a-f0-9]{64}', str(request.get('inputSha256', ''))):
        raise ValueError('Input image hash required')
    return request


def _loaders(spec: dict) -> dict:
    """各版本的加载器。统一出口：model / clip / audio_vae 节点名；VAE 见 _vae()。"""
    if 'checkpoint' in spec:          # LTX-2.3：单文件 checkpoint，dev 模型 + 0.5 distilled LoRA
        return {
            'checkpoint': {'class_type': 'CheckpointLoaderSimple', 'inputs': {
                'ckpt_name': spec['checkpoint']}},
            'clip': {'class_type': 'LTXAVTextEncoderLoader', 'inputs': {
                'text_encoder': spec['text_encoder'], 'ckpt_name': spec['checkpoint'],
                'device': 'default'}},
            'audio_vae': {'class_type': 'LTXVAudioVAELoader', 'inputs': {
                'ckpt_name': spec['checkpoint']}},
            'model': {'class_type': 'LoraLoaderModelOnly', 'inputs': {
                'model': ['checkpoint', 0], 'lora_name': spec['distill_lora'],
                'strength_model': 0.5}},
        }
    return {
        'model': {'class_type': 'UNETLoader', 'inputs': {
            'unet_name': spec['transformer'], 'weight_dtype': 'default'}},
        'clip': {'class_type': 'CLIPLoader', 'inputs': {
            'clip_name': spec['text_encoder'], 'type': 'ltxv', 'device': 'default'}},
        'vae': {'class_type': 'VAELoader', 'inputs': {'vae_name': spec['vae']}},
        'audio_vae': {'class_type': 'VAELoader', 'inputs': {'vae_name': spec['audio_vae']}},
    }


def _vae(spec: dict) -> list:
    return ['checkpoint', 2] if 'checkpoint' in spec else ['vae', 0]


def _guider(spec: dict, model, positive, negative) -> dict:
    if 'checkpoint' in spec:
        return {'class_type': 'CFGGuider', 'inputs': {
            'model': model, 'positive': positive, 'negative': negative, 'cfg': 1.0}}
    return {'class_type': 'LTXVDualCFGGuider', 'inputs': {
        'model': model, 'positive': positive, 'negative': negative,
        'video_cfg': 1.0, 'audio_cfg': 1.0}}


def workflow(request: dict) -> dict:
    """两段式图生视频 + 联合音频，逐节点对应官方模板（见模块文档）。"""
    spec = MODELS[request['model']]
    width, height, frames, fps = request['width'], request['height'], request['frames'], FPS
    graph = _loaders(spec)
    # 用户 LoRA 串在底模之后；两段采样共用同一个打过补丁的模型。节点号用 lora_ 前缀。
    model = ['model', 0]
    for index, item in enumerate(request.get('loras', [])):
        node = f'lora_{index}'
        graph[node] = {'class_type': 'LoraLoaderModelOnly', 'inputs': {
            'model': model, 'lora_name': item['file'], 'strength_model': item['strength']}}
        model = [node, 0]
    vae, audio_vae = _vae(spec), ['audio_vae', 0]
    if 'checkpoint' in spec:
        # 2.3 模板先按目标尺寸居中裁切，再把长边缩到 1536
        crop = {'class_type': 'ResizeImageMaskNode', 'inputs': {
            'input': ['input', 0], 'resize_type': 'scale dimensions',
            'resize_type.width': width, 'resize_type.height': height,
            'resize_type.crop': 'center', 'scale_method': 'lanczos'}}
        graph['crop'] = crop
        resize_source, resize_method = ['crop', 0], 'area'
    else:
        resize_source, resize_method = ['input', 0], 'lanczos'
    graph.update({
        'input': {'class_type': 'LoadImage', 'inputs': {'image': request['inputName']}},
        'resize': {'class_type': 'ResizeImageMaskNode', 'inputs': {
            'input': resize_source, 'resize_type': 'scale longer dimension',
            'resize_type.longer_size': 1536, 'scale_method': resize_method}},
        'preprocess': {'class_type': 'LTXVPreprocess', 'inputs': {
            'image': ['resize', 0], 'img_compression': 18}},
        'positive': {'class_type': 'CLIPTextEncode', 'inputs': {
            'clip': ['clip', 0], 'text': request['prompt']}},
        'negative': {'class_type': 'CLIPTextEncode', 'inputs': {
            'clip': ['clip', 0], 'text': NEGATIVE}},
        'conditioning': {'class_type': 'LTXVConditioning', 'inputs': {
            'positive': ['positive', 0], 'negative': ['negative', 0], 'frame_rate': float(fps)}},
        # 第一段：半分辨率，首帧以 0.7 强度注入
        'latent': {'class_type': 'EmptyLTXVLatentVideo', 'inputs': {
            'width': width // 2, 'height': height // 2, 'length': frames, 'batch_size': 1}},
        'first_frame1': {'class_type': 'LTXVImgToVideoInplace', 'inputs': {
            'vae': vae, 'image': ['preprocess', 0], 'latent': ['latent', 0],
            'strength': 0.7, 'bypass': False}},
        'audio_latent': {'class_type': 'LTXVEmptyLatentAudio', 'inputs': {
            'audio_vae': audio_vae, 'frames_number': frames, 'frame_rate': fps,
            'batch_size': 1}},
        'av1': {'class_type': 'LTXVConcatAVLatent', 'inputs': {
            'video_latent': ['first_frame1', 0], 'audio_latent': ['audio_latent', 0]}},
        'noise1': {'class_type': 'RandomNoise', 'inputs': {'noise_seed': request['seed']}},
        'sampler1': {'class_type': 'KSamplerSelect', 'inputs': {'sampler_name': spec['sampler']}},
        'sigmas1': {'class_type': 'ManualSigmas', 'inputs': {'sigmas': STAGE1_SIGMAS}},
        'guider1': _guider(spec, model, ['conditioning', 0], ['conditioning', 1]),
        'sample1': {'class_type': 'SamplerCustomAdvanced', 'inputs': {
            'noise': ['noise1', 0], 'guider': ['guider1', 0], 'sampler': ['sampler1', 0],
            'sigmas': ['sigmas1', 0], 'latent_image': ['av1', 0]}},
        'split1': {'class_type': 'LTXVSeparateAVLatent', 'inputs': {'av_latent': ['sample1', 0]}},
        # 第二段：潜空间 2× 放大，首帧以 1.0 强度再注入，3 步精修
        'upscaler': {'class_type': 'LatentUpscaleModelLoader', 'inputs': {
            'model_name': spec['upscaler']}},
        'upscale': {'class_type': 'LTXVLatentUpsampler', 'inputs': {
            'samples': ['split1', 0], 'upscale_model': ['upscaler', 0], 'vae': vae}},
        'first_frame2': {'class_type': 'LTXVImgToVideoInplace', 'inputs': {
            'vae': vae, 'image': ['preprocess', 0], 'latent': ['upscale', 0],
            'strength': 1.0, 'bypass': False}},
        'av2': {'class_type': 'LTXVConcatAVLatent', 'inputs': {
            'video_latent': ['first_frame2', 0], 'audio_latent': ['split1', 1]}},
        'noise2': {'class_type': 'RandomNoise', 'inputs': {'noise_seed': 42}},
        'sampler2': {'class_type': 'KSamplerSelect', 'inputs': {'sampler_name': spec['sampler']}},
        'sigmas2': {'class_type': 'ManualSigmas', 'inputs': {'sigmas': STAGE2_SIGMAS}},
        'guider2': _guider(spec, model, ['conditioning', 0], ['conditioning', 1]),
        'sample2': {'class_type': 'SamplerCustomAdvanced', 'inputs': {
            'noise': ['noise2', 0], 'guider': ['guider2', 0], 'sampler': ['sampler2', 0],
            'sigmas': ['sigmas2', 0], 'latent_image': ['av2', 0]}},
        'split2': {'class_type': 'LTXVSeparateAVLatent', 'inputs': {'av_latent': ['sample2', 0]}},
        'decode': {'class_type': 'VAEDecodeTiled', 'inputs': dict(
            zip(('tile_size', 'overlap', 'temporal_size', 'temporal_overlap'), spec['decode']),
            samples=['split2', 0], vae=vae)},
        'decode_audio': {'class_type': 'LTXVAudioVAEDecode', 'inputs': {
            'samples': ['split2', 1], 'audio_vae': audio_vae}},
        'video': {'class_type': 'CreateVideo', 'inputs': {
            'images': ['decode', 0], 'audio': ['decode_audio', 0], 'fps': float(fps)}},
        'save': {'class_type': 'SaveVideo', 'inputs': {
            'video': ['video', 0], 'filename_prefix': 'aladin-' + request['key'][:12],
            'format': 'mp4', 'format.codec': 'h264'}},
    })
    if 'checkpoint' in spec:
        # 2.3 模板：第二段的条件要裁掉第一段注入的首帧引导
        graph['crop_guides'] = {'class_type': 'LTXVCropGuides', 'inputs': {
            'positive': ['conditioning', 0], 'negative': ['conditioning', 1],
            'latent': ['split1', 0]}}
        graph['guider2'] = _guider(spec, model, ['crop_guides', 0], ['crop_guides', 1])
    return graph


# --- 权重 ------------------------------------------------------------------

def cached(manifest: dict, model: str, root: Path) -> bool:
    spec = MODELS[model]
    if manifest.get('model') != model or manifest.get('revision') != spec['revision']:
        return False
    # 按当前钉版验，而不是按 manifest 里记的那套：换量化后旧文件还在，
    # 用旧清单判会一直命中、新权重永远下不下来（生图那边实测踩过）。
    for _repo, _revision, _hub, local, size in spec['files']:
        path = root / local
        if not path.is_file() or path.stat().st_size != size:
            return False
    return True


def _download(repo: str, revision: str, hub: str, target: Path, size: int) -> None:
    import shutil
    from huggingface_hub import hf_hub_download
    say(f'downloading {repo}/{hub} ({size / 1e9:.2f} GB)')
    staging = target.parent.parent / '.hf-staging'
    downloaded = Path(hf_hub_download(repo_id=repo, filename=hub, revision=revision,
                                      local_dir=str(staging)))
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(downloaded), str(target))
    if target.stat().st_size != size:
        raise ValueError('Downloaded weight size mismatch: ' + hub)


def ensure_weights(model: str = MODEL, root: Path = ROOT) -> dict:
    """确保全套权重在本地，返回 manifest。仓库内路径带前缀，下载后统一归位。"""
    manifest_path = root / WEIGHT_MANIFEST
    manifest = json.loads(manifest_path.read_text()) if manifest_path.is_file() else {}
    if cached(manifest, model, root):
        say('weight cache hit for ' + model)
        return manifest
    spec = MODELS[model]
    root.mkdir(parents=True, exist_ok=True)
    files = []
    for repo, revision_, hub, local, size in spec['files']:
        target = root / local
        if not (target.is_file() and target.stat().st_size == size):
            _download(repo, revision_, hub, target, size)
        files.append({'hub': hub, 'path': local, 'bytes': size})
    manifest = {'model': model, 'repo': spec['repo'], 'revision': spec['revision'],
                'comfyRevision': COMFY_REVISION, 'files': files,
                'verifiedAt': round(time.time(), 3)}
    atomic_json(manifest_path, manifest)
    return manifest


def fetch_lora(repo: str, revision_: str, hub: str, file: str, size: int, sha256: str,
               root: Path = ROOT) -> dict:
    """HF 上的 LoRA 直接在容器里下载进 Volume（大文件不经本机中转）；Civitai 的走本机 loras-sync。"""
    if not LORA_FILE.match(file):
        raise ValueError('Invalid LoRA file name')
    target = root / 'loras' / file
    if not (target.is_file() and target.stat().st_size == size):
        _download(repo, revision_, hub, target, size)
    with target.open('rb') as handle:
        actual = hashlib.file_digest(handle, 'sha256').hexdigest()
    if actual != sha256:
        target.unlink()
        raise ValueError('LoRA checksum mismatch: ' + hub)
    atomic_json(target.with_suffix('.verified.json'), {'size': size, 'sha256': sha256})
    return {'file': file, 'bytes': size}


def ensure_loras(loras, root: Path = ROOT) -> None:
    """LoRA 由宿主侧的 loras-sync 预先放进模型 Volume；这里只核对，不下载。

    校验结果记在旁边的标记文件里：大文件每次都算 sha256 太慢，而 Volume 上的文件不会被改写。
    """
    for item in loras:
        target = root / 'loras' / item['file']
        if not target.is_file():
            raise ValueError('LoRA not synced to volume: ' + item['file'] +
                             '（先运行 python -m aladin loras-sync）')
        marker = target.with_suffix('.verified.json')
        stamp = {'size': target.stat().st_size, 'sha256': item['sha256']}
        if marker.exists() and json.loads(marker.read_text()) == stamp:
            continue
        with target.open('rb') as handle:
            actual = hashlib.file_digest(handle, 'sha256').hexdigest()
        if actual != item['sha256']:
            raise ValueError('LoRA checksum mismatch: ' + item['file'])
        atomic_json(marker, stamp)


def extra_model_paths(root: Path) -> Path:
    path = Path('/tmp/extra_model_paths.yaml')
    path.write_text('aladin:\n    base_path: ' + str(root) + '\n' + ''.join(
        f'    {name}: {name}\n' for name in ('checkpoints', 'diffusion_models', 'text_encoders',
                                            'vae', 'loras', 'latent_upscale_models')))
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


def lora_warnings(path: str, limit: int = 20) -> list:
    try:
        lines = Path(path).read_text(errors='replace').splitlines()
    except FileNotFoundError:
        return []
    return [line for line in lines if 'lora key not loaded' in line.lower()][:limit]


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
    request = validate(json.loads(json.dumps(request)), MODEL)
    started = time.time()
    timings = Timings()
    spec = MODELS[request['model']]
    ensure_weights(request['model'], ROOT)
    ensure_loras(request.get('loras', []), ROOT)
    timings.mark('weights')
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
    timings.mark('input')

    process, _log = start_server(ROOT)
    timings.mark('comfyBoot')
    try:
        graph = workflow(request)
        client_id = 'aladin-' + request['key'][:16]
        prompt_id = str(uuid.uuid4())          # ComfyUI 只接受规范 UUID
        timings.watch(graph, prompt_id)
        handle = watch_async(SERVER, prompt_id, classify(graph), timeout=2400.0,
                             client_id=client_id, on_event=timings.on_event)
        if not handle.ready.wait(timeout=30):
            raise RuntimeError('WebSocket 订阅未能在 30s 内建立')
        progress({'kind': 'queued', 'job_key': request['key']})
        submit(graph, client_id, prompt_id)
        timings.mark('submit')

        history = wait_for_history(prompt_id, time.time() + 2400)
        if history is None:
            raise RuntimeError('ComfyUI 未在期限内产出结果')
        entry = history[prompt_id]
        if entry.get('status', {}).get('status_str') == 'error':
            raise RuntimeError('ComfyUI 执行失败: ' + json.dumps(entry.get('status'))[:500])
        timings.mark('execute')

        prefix = 'aladin-' + request['key'][:12]
        data = fetch_video(prefix, time.time() + 300)
        if len(data) > MAX_VIDEO:
            raise ValueError('视频超出上限')
        name = 'video-01.webm'
        temporary = output / (name + '.tmp')
        temporary.write_bytes(data)
        temporary.replace(output / name)
        timings.mark('outputs')
        elapsed = round(time.time() - started, 2)
        record = {'schemaVersion': 1, 'request': request, 'model': spec['repo'],
                  'elapsedSeconds': elapsed,
                  'weights': [item[3] for item in spec['files']],
                  'videos': [{'index': 1, 'file': name, 'sha256': digest(data),
                              'bytes': len(data), 'format': 'webm', 'fps': FPS,
                              'frames': request['frames'],
                              'prompt': request['prompt'], 'seed': request['seed']}],
                  # LoRA 键对不上时 ComfyUI 只打警告不报错（2.3 的 LoRA 用在 2.5 上尤其要看这里）
                  'loraWarnings': lora_warnings('/tmp/comfy-server.log'),
                  'serverLogTail': tail('/tmp/comfy-server.log', 8),
                  'timings': timings.record()}
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
