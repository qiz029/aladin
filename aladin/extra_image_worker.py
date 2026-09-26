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
from .extra_image_request import build, build_variants


def _check_variant(spec, variant, index):
    """一张图的参数：与宿主侧 params.image_params 同一套边界。"""
    prompt = variant.get('prompt')
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 2000:
        raise ValueError(f'Invalid prompt (image {index})')
    if not isinstance(variant.get('negative'), str) or len(variant['negative']) > 2000:
        raise ValueError(f'Invalid negative prompt (image {index})')
    size = spec['sizes'].get(variant.get('size'))
    if not size or (variant.get('width'), variant.get('height')) != (size['width'], size['height']):
        raise ValueError(f'Invalid size (image {index})')
    steps = variant.get('steps')
    if type(steps) is not int or not spec['steps'][0] <= steps <= spec['steps'][1]:
        raise ValueError(f'Invalid steps (image {index})')
    cfg = variant.get('cfg')
    if isinstance(cfg, bool) or not isinstance(cfg, (int, float)) or not math.isfinite(cfg) or not 0 <= cfg <= 10:
        raise ValueError(f'Invalid CFG (image {index})')
    seed = variant.get('seed')
    if type(seed) is not int or not 0 <= seed <= 2**63 - 1:
        raise ValueError(f'Invalid seed (image {index})')
    validate_loras(variant.get('loras', []))


def validate(request):
    model = request.get('modelId')
    if model not in MODELS or model == DEFAULT_MODEL:
        raise ValueError('Unsupported model')
    spec = MODELS[model]
    if 'variants' in request:
        # 一句话出图：每张图自带参数与 LoRA
        variants = request['variants']
        if not isinstance(variants, list) or not 1 <= len(variants) <= 8:
            raise ValueError('Invalid image count')
        if not isinstance(request.get('key'), str) or not re.fullmatch(r'[0-9a-f]{64}', request['key']):
            raise ValueError('Invalid key')
        if request.get('sampler') not in spec['samplers'] or request.get('scheduler') not in spec['schedulers']:
            raise ValueError('Unsupported sampler/scheduler')
        for index, variant in enumerate(variants, start=1):
            if not isinstance(variant, dict):
                raise ValueError(f'Invalid image {index}')
            _check_variant(spec, variant, index)
        if build_variants(request['key'], model, variants, request['sampler'], request['scheduler']) != request:
            raise ValueError('Request or worker revision mismatch')
        return request
    params = request['params']
    prompts = request['prompts']
    if not isinstance(prompts, list) or not 1 <= len(prompts) <= 8:
        raise ValueError('Invalid image count')
    if len(set(prompts)) != 1 or params['model'] != model:
        raise ValueError('Inconsistent request')
    for index, variant in enumerate(variants_of(request), start=1):
        _check_variant(spec, variant, index)
    if params['sampler'] not in spec['samplers'] or params['scheduler'] not in spec['schedulers']:
        raise ValueError('Unsupported sampler/scheduler')
    rebuilt_params = dict(params, loras=[dict(item) for item in request.get('loras', [])])
    if build(prompts[0], len(prompts), rebuilt_params) != request:
        raise ValueError('Request or worker revision mismatch')
    return request


def variants_of(request):
    """两种请求形状归一成「每张图一套参数」：页面文生图（同一提示词 × N，种子递增）与一句话出图的批量。

    归一之后，工作流、执行与产物记录只处理这一种形状。
    """
    if 'variants' in request:
        return [dict(item) for item in request['variants']]
    p = request['params']
    return [dict(prompt=prompt, negative=p['negative'], size=p['size'], width=p['width'],
                 height=p['height'], steps=p['steps'], cfg=p['cfg'], seed=p['seed'] + index,
                 loras=list(request.get('loras', [])))
            for index, prompt in enumerate(request['prompts'])]


def sampling(request):
    source = request if 'variants' in request else request['params']
    return source['sampler'], source['scheduler']


def save_node(index):
    return str(15 + index * 6)


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
    graph = {}
    if request['modelId'] == 'anima-base-1.0':
        graph.update({
            '1': dict(class_type='UNETLoader', inputs=dict(unet_name='anima-base-v1.0.safetensors', weight_dtype='default')),
            '2': dict(class_type='CLIPLoader', inputs=dict(clip_name='qwen_3_06b_base.safetensors', type='stable_diffusion', device='default')),
            '3': dict(class_type='VAELoader', inputs=dict(vae_name='qwen_image_vae.safetensors'))})
        base_model, base_clip, vae = ['1', 0], ['2', 0], ['3', 0]
    else:
        graph.update({
            '1': dict(class_type='CheckpointLoaderSimple', inputs=dict(ckpt_name='ponyRealism_v22MainVAE.safetensors')),
            '2': dict(class_type='CLIPSetLastLayer', inputs=dict(clip=['1', 1], stop_at_clip_layer=-2))})
        base_model, base_clip, vae = ['1', 0], ['2', 0], ['1', 2]
    sampler_name, scheduler = sampling(request)
    # LoRA 串在加载器之后：每个 LoraLoader 同时改模型与文本编码器，后一个接前一个的输出。
    # 叠加是加法，顺序不影响结果。同一套 LoRA 的图共用一条链；节点号用 200+ 以免与采样节点冲突。
    chains, next_node = {}, 200
    for index, variant in enumerate(variants_of(request)):
        loras = variant.get('loras', [])
        signature = json.dumps(loras, sort_keys=True)
        if signature not in chains:
            model, clip = base_model, base_clip
            for item in loras:
                node = str(next_node)
                next_node += 1
                graph[node] = dict(class_type='LoraLoader', inputs=dict(
                    model=model, clip=clip, lora_name=item['file'],
                    strength_model=item['strength'], strength_clip=item['strength']))
                model, clip = [node, 0], [node, 1]
            chains[signature] = (model, clip)
        model, clip = chains[signature]
        positive, negative, latent, sampler, decode, save = (str(10 + index * 6 + n) for n in range(6))
        graph[positive] = dict(class_type='CLIPTextEncode', inputs=dict(text=variant['prompt'], clip=clip))
        graph[negative] = dict(class_type='CLIPTextEncode', inputs=dict(text=variant['negative'], clip=clip))
        graph[latent] = dict(class_type='EmptyLatentImage', inputs=dict(width=variant['width'], height=variant['height'], batch_size=1))
        graph[sampler] = dict(class_type='KSampler', inputs=dict(model=model, positive=[positive, 0], negative=[negative, 0], latent_image=[latent, 0], seed=variant['seed'], steps=variant['steps'], cfg=variant['cfg'], sampler_name=sampler_name, scheduler=scheduler, denoise=1.0))
        graph[decode] = dict(class_type='VAEDecode', inputs=dict(samples=[sampler, 0], vae=vae))
        graph[save] = dict(class_type='SaveImage', inputs=dict(images=[decode, 0], filename_prefix='aladin'))
    assert save == save_node(index)
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
    timings = runtime.Timings()
    output = Path(result_root) / request['key']
    output.mkdir(parents=True, exist_ok=True)
    cached = runtime.receipt_hit(output, request)
    if cached is not None:
        return cached
    root = Path('/models')
    ensure_weights(request['modelId'], root)
    variants = variants_of(request)
    ensure_loras([item for variant in variants for item in variant.get('loras', [])], root)
    timings.mark('weights')
    process, log = start_server(root)
    timings.mark('comfyBoot')
    try:
        graph = workflow(request)
        prompt_id, client_id = str(uuid.uuid4()), 'aladin-' + request['key'][:16]
        timings.watch(graph, prompt_id)
        handle = runtime.watch_async(runtime.SERVER, prompt_id, runtime.classify(graph), timeout=1800, client_id=client_id,
                                     on_event=timings.on_event)
        if not handle.ready.wait(timeout=30):
            raise RuntimeError('Progress subscription failed')
        runtime.submit(graph, client_id, prompt_id)
        timings.mark('submit')
        history = runtime.wait_for_history(prompt_id, time.time() + 1500)
        if not history or history[prompt_id].get('status', {}).get('status_str') == 'error':
            raise RuntimeError('ComfyUI execution failed: ' + str(history)[:1500])
        timings.mark('execute')
        images = []
        sampler_name, scheduler = sampling(request)
        for index, variant in enumerate(variants):
            item = history[prompt_id]['outputs'][save_node(index)]['images'][0]
            data = runtime.fetch_image(item, time.time() + 120)
            if len(data) > runtime.MAX_IMAGE:
                raise ValueError('Image exceeds size limit')
            name = f'image-{index+1:02d}.png'
            tmp = output / (name + '.tmp')
            tmp.write_bytes(data)
            tmp.replace(output / name)
            # 产物级参数：逐张记下，批量里每张都可能不同
            params = {k: variant[k] for k in ('negative', 'size', 'width', 'height', 'steps', 'cfg', 'seed')}
            params.update(model=request['modelId'], sampler=sampler_name, scheduler=scheduler)
            if variant.get('loras'):
                params['loras'] = variant['loras']
            if request['modelId'] == 'pony-realism-2.2':
                params['clip_skip'] = 2
            images.append(dict(index=index+1, file=name, sha256=runtime.digest(data), bytes=len(data), format='png', prompt=variant['prompt'], seed=variant['seed'], params=params))
        timings.mark('outputs')
        record = dict(schemaVersion=1, request=request, mode='txt2img', model=request['model'], elapsedSeconds=round(time.time()-started, 2), images=images,
                      serverLogTail=runtime.tail('/tmp/comfy-server.log', 8), timings=timings.record())
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
