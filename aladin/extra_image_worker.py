"""Anima and Pony graphs; shared Comfy transport, isolated model caches."""
import hashlib
import json
import math
import re
from pathlib import Path
import shutil
import subprocess
import time
import urllib.request
import uuid

from . import worker as runtime
from .image_models import MODELS, DEFAULT_MODEL, COMFY_REVISION
from .extra_image_request import build


def validate(request):
    model = request.get('modelId')
    if model not in MODELS or model == DEFAULT_MODEL:
        raise ValueError('Unsupported model')
    spec = MODELS[model]
    params = request['params']
    prompts = request['prompts']
    if not isinstance(prompts, list) or not 1 <= len(prompts) <= 8:
        raise ValueError('Invalid image count')
    if any(not isinstance(p, str) or not p.strip() or len(p) > 2000 for p in prompts):
        raise ValueError('Invalid prompt')
    if len(set(prompts)) != 1 or params['model'] != model:
        raise ValueError('Inconsistent request')
    if not isinstance(params['negative'], str) or len(params['negative']) > 2000:
        raise ValueError('Invalid negative prompt')
    size = spec['sizes'].get(params['size'])
    if not size or (params['width'], params['height']) != (size['width'], size['height']):
        raise ValueError('Invalid size')
    if type(params['steps']) is not int or not spec['steps'][0] <= params['steps'] <= spec['steps'][1]:
        raise ValueError('Invalid steps')
    if not math.isfinite(params['cfg']) or not 0 <= params['cfg'] <= 10:
        raise ValueError('Invalid CFG')
    if type(params['seed']) is not int or not 0 <= params['seed'] <= 2**63 - 1:
        raise ValueError('Invalid seed')
    if params['sampler'] not in spec['samplers'] or params['scheduler'] not in spec['schedulers']:
        raise ValueError('Unsupported sampler/scheduler')
    validate_loras(request.get('loras', []))
    rebuilt_params = dict(params, loras=[dict(item) for item in request.get('loras', [])])
    if build(prompts[0], len(prompts), rebuilt_params) != request:
        raise ValueError('Request or worker revision mismatch')
    return request


LORA_FILE = re.compile(r'^civitai-\d+\.safetensors$')


def validate_loras(loras):
    """容器只认白名单形状：固定命名的文件、64 位 sha256、有限的强度。目录与名称在宿主侧。"""
    if not isinstance(loras, list) or len(loras) > 6:
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


def ensure_loras(loras, root):
    """LoRA 由宿主侧的 loras-sync 预先放进模型 Volume；这里只核对，不下载。

    校验结果记在旁边的标记文件里：大文件每次都算 sha256 太慢，而 Volume 上的文件不会被改写。
    """
    for item in loras:
        target = root / 'loras' / item['file']
        if not target.is_file():
            raise ValueError('LoRA not synced to volume: ' + item['file'] + '（先运行 python -m aladin loras-sync）')
        marker = target.with_suffix('.verified.json')
        stamp = dict(size=target.stat().st_size, sha256=item['sha256'])
        if marker.exists() and json.loads(marker.read_text()) == stamp:
            continue
        with target.open('rb') as handle:
            actual = hashlib.file_digest(handle, 'sha256').hexdigest()
        if actual != item['sha256']:
            raise ValueError('LoRA checksum mismatch: ' + item['file'])
        runtime.atomic_json(marker, stamp)


def ensure_weights(model, root):
    from huggingface_hub import hf_hub_download
    spec = MODELS[model]
    verified = []
    for hub, local, size, sha in spec['files']:
        target = root / local
        marker = target.with_suffix('.verified.json')
        stamp = dict(size=size, sha256=sha)
        if target.exists() and target.stat().st_size == size and marker.exists() and json.loads(marker.read_text()) == stamp:
            verified.append(local)
            continue
        downloaded = Path(hf_hub_download(spec['repo'], hub, revision=spec['revision'], cache_dir=str(root / 'hub')))
        with downloaded.open('rb') as handle:
            actual = hashlib.file_digest(handle, 'sha256').hexdigest()
        if downloaded.stat().st_size != size or actual != sha:
            raise ValueError('Weight checksum mismatch: ' + hub)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(downloaded, target)
        runtime.atomic_json(marker, stamp)
        verified.append(local)
    return dict(model=model, revision=spec['revision'], files=verified)


def workflow(request):
    p = request['params']
    graph = {}
    if request['modelId'] == 'anima-base-1.0':
        graph.update({
            '1': dict(class_type='UNETLoader', inputs=dict(unet_name='anima-base-v1.0.safetensors', weight_dtype='default')),
            '2': dict(class_type='CLIPLoader', inputs=dict(clip_name='qwen_3_06b_base.safetensors', type='stable_diffusion', device='default')),
            '3': dict(class_type='VAELoader', inputs=dict(vae_name='qwen_image_vae.safetensors'))})
        model, clip, vae = ['1', 0], ['2', 0], ['3', 0]
    else:
        graph.update({
            '1': dict(class_type='CheckpointLoaderSimple', inputs=dict(ckpt_name='ponyRealism_v22MainVAE.safetensors')),
            '2': dict(class_type='CLIPSetLastLayer', inputs=dict(clip=['1', 1], stop_at_clip_layer=-2))})
        model, clip, vae = ['1', 0], ['2', 0], ['1', 2]
    # LoRA 串在加载器之后：每个 LoraLoader 同时改模型与文本编码器，后一个接前一个的输出。
    # 叠加是加法，顺序不影响结果；节点号用 200+ 以免与采样节点冲突。
    for index, item in enumerate(request.get('loras', [])):
        node = str(200 + index)
        graph[node] = dict(class_type='LoraLoader', inputs=dict(
            model=model, clip=clip, lora_name=item['file'],
            strength_model=item['strength'], strength_clip=item['strength']))
        model, clip = [node, 0], [node, 1]
    graph['4'] = dict(class_type='CLIPTextEncode', inputs=dict(text=p['negative'], clip=clip))
    graph['5'] = dict(class_type='EmptyLatentImage', inputs=dict(width=p['width'], height=p['height'], batch_size=1))
    for index, prompt in enumerate(request['prompts']):
        positive, sampler, decode, save = (str(10 + index * 4 + n) for n in range(4))
        graph[positive] = dict(class_type='CLIPTextEncode', inputs=dict(text=prompt, clip=clip))
        graph[sampler] = dict(class_type='KSampler', inputs=dict(model=model, positive=[positive, 0], negative=['4', 0], latent_image=['5', 0], seed=p['seed'] + index, steps=p['steps'], cfg=p['cfg'], sampler_name=p['sampler'], scheduler=p['scheduler'], denoise=1.0))
        graph[decode] = dict(class_type='VAEDecode', inputs=dict(samples=[sampler, 0], vae=vae))
        graph[save] = dict(class_type='SaveImage', inputs=dict(images=[decode, 0], filename_prefix='aladin'))
    return graph


def start_server(root):
    config = Path('/tmp/extra-image-models.yaml')
    config.write_text('aladin:\n    base_path: ' + str(root) + '\n' + ''.join(f'    {name}: {name}\n' for name in ('diffusion_models', 'text_encoders', 'vae', 'checkpoints', 'loras')))
    log = open('/tmp/comfy-server.log', 'w')
    process = subprocess.Popen(['python', 'main.py', '--listen', '127.0.0.1', '--port', '8188', '--disable-auto-launch', '--extra-model-paths-config', str(config), '--output-directory', '/tmp/comfy/output', '--disable-metadata'], cwd='/opt/ComfyUI', stdout=log, stderr=subprocess.STDOUT)
    try:
        deadline = time.time() + 600
        while time.time() < deadline:
            try:
                urllib.request.urlopen(runtime.SERVER + '/system_stats', timeout=2).read()
                return process, log
            except Exception:
                if process.poll() is not None:
                    raise RuntimeError('ComfyUI exited: ' + str(runtime.tail('/tmp/comfy-server.log', 10)))
                time.sleep(1)
        raise RuntimeError('ComfyUI startup timed out')
    except BaseException:
        process.kill()
        process.wait()
        log.close()
        raise


def execute(request, result_root):
    validate(request)
    started = time.time()
    output = Path(result_root) / request['key']
    output.mkdir(parents=True, exist_ok=True)
    cached = runtime.receipt_hit(output, request)
    if cached is not None:
        return cached
    root = Path('/models')
    ensure_weights(request['modelId'], root)
    ensure_loras(request.get('loras', []), root)
    process, log = start_server(root)
    try:
        graph = workflow(request)
        prompt_id, client_id = str(uuid.uuid4()), 'aladin-' + request['key'][:16]
        handle = runtime.watch_async(runtime.SERVER, prompt_id, runtime.classify(graph), timeout=1800, client_id=client_id)
        if not handle.ready.wait(timeout=30):
            raise RuntimeError('Progress subscription failed')
        runtime.submit(graph, client_id, prompt_id)
        history = runtime.wait_for_history(prompt_id, time.time() + 1500)
        if not history or history[prompt_id].get('status', {}).get('status_str') == 'error':
            raise RuntimeError('ComfyUI execution failed: ' + str(history)[:1500])
        images = []
        for index, prompt in enumerate(request['prompts']):
            item = history[prompt_id]['outputs'][str(13 + index * 4)]['images'][0]
            data = runtime.fetch_image(item, time.time() + 120)
            if len(data) > runtime.MAX_IMAGE:
                raise ValueError('Image exceeds size limit')
            name = f'image-{index+1:02d}.png'
            tmp = output / (name + '.tmp')
            tmp.write_bytes(data)
            tmp.replace(output / name)
            seed = request['params']['seed'] + index
            params = dict(request['params'], seed=seed)
            if request.get('loras'):
                params['loras'] = request['loras']
            if request['modelId'] == 'pony-realism-2.2':
                params['clip_skip'] = 2
            images.append(dict(index=index+1, file=name, sha256=runtime.digest(data), bytes=len(data), format='png', prompt=prompt, seed=seed, params=params))
        record = dict(schemaVersion=1, request=request, mode='txt2img', model=request['model'], elapsedSeconds=round(time.time()-started, 2), images=images)
        runtime.atomic_json(output / 'result.json', record)
        return record
    except Exception as error:
        raise RuntimeError(str(error) + ' | ' + ' / '.join(runtime.tail('/tmp/comfy-server.log', 8))) from None
    finally:
        process.terminate()
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        log.close()
