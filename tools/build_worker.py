from pathlib import Path
import os

header = '''"""容器内执行体：跑一个受限的 ComfyUI 工作流，边跑边发结构化进度。

`watch` 段由 probe/watchdog.py 派生（单一真相源），已由探针 0002 在真实容器里验证：
- 必须声明 clientId 且与提交时的 extra_data["client_id"] 一致，否则收不到任何 progress
- prompt_id 必须是规范 UUID
- 需显式传 --extra-model-paths-config，否则找不到 Volume 里的模型
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import queue
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
import os

PREFIX = '[aladin-progress]'

COMFY_REVISION = 'c194dd00cd42aa18d9dbf27d977bf6b85d9ea565'
GGUF_REVISION = 'edd981b10e107d3b8f58e16c498f2d08f631bc47'

MODES = ['txt2img', 'img2img', 'edit']
MODEL_FOR_MODE = {'txt2img': 'qwen-image-2.1', 'img2img': 'qwen-image-2.1',
                 'edit': 'qwen-image-edit-2509'}

# 每个模型一套权重。指令编辑连文本编码器（Qwen2.5-VL-7B）和 VAE 都与文生图
# （Qwen3-VL-8B）不通用，所以整套独立钉版。
# files 每项：(repo, revision, 仓库内路径, 本地路径, 精确字节数)
# 文生图 / 图生图使用作者明确标注的 UC Q8_0（7.6GB），与 base 文件分开缓存。
# 换档只改 transformer 与 files 里的文件名/字节数，两者必须一起改。
MODELS = {
    'qwen-image-2.1': {
        'repo': 'abenzerps/Qwen-Image-2.1-Uncensored-GGUF',
        'revision': '1206d38bb47ef93961bfb77bc2c700d43a25860e',
        'transformer': 'qwen-image-2.1-UC-Q8_0.gguf',
        'encoder': 'qwen3vl_8b_int8_convrot.safetensors',
        'vae': 'qwen_image_2.1_vae_bf16.safetensors',
        'files': [
            ('abenzerps/Qwen-Image-2.1-Uncensored-GGUF', '1206d38bb47ef93961bfb77bc2c700d43a25860e',
             # 仓库里 GGUF 在根目录；本地要落到 diffusion_models/ 才被 ComfyUI 找到。
             'qwen-image-2.1-UC-Q8_0.gguf',
             'diffusion_models/qwen-image-2.1-UC-Q8_0.gguf', 7591557920),
            ('abenzerps/Qwen-Image-2.1-Uncensored-GGUF', '1206d38bb47ef93961bfb77bc2c700d43a25860e',
             'text_encoders/qwen3vl_8b_int8_convrot.safetensors',
             'text_encoders/qwen3vl_8b_int8_convrot.safetensors', 9350798360),
            ('abenzerps/Qwen-Image-2.1-Uncensored-GGUF', '1206d38bb47ef93961bfb77bc2c700d43a25860e',
             'vae/qwen_image_2.1_vae_bf16.safetensors',
             'vae/qwen_image_2.1_vae_bf16.safetensors', 675509688),
        ],
    },
    'qwen-image-edit-2509': {
        'repo': 'QuantStack/Qwen-Image-Edit-2509-GGUF',
        'revision': '84a3006979126011422eeeefe0c9485ddf431ef5',
        'transformer': 'Qwen-Image-Edit-2509-Q4_K_M.gguf',
        'encoder': 'qwen_2.5_vl_7b_fp8_scaled.safetensors',
        'vae': 'qwen_image_vae.safetensors',
        'files': [
            ('QuantStack/Qwen-Image-Edit-2509-GGUF', '84a3006979126011422eeeefe0c9485ddf431ef5',
             'Qwen-Image-Edit-2509-Q4_K_M.gguf',
             'diffusion_models/Qwen-Image-Edit-2509-Q4_K_M.gguf', 13065746976),
            ('Comfy-Org/Qwen-Image_ComfyUI', '7beb7b647f04469fbe64ba8adc2bb0d7e5e9f73f',
             'split_files/text_encoders/qwen_2.5_vl_7b_fp8_scaled.safetensors',
             'text_encoders/qwen_2.5_vl_7b_fp8_scaled.safetensors', 9384670680),
            ('Comfy-Org/Qwen-Image_ComfyUI', '7beb7b647f04469fbe64ba8adc2bb0d7e5e9f73f',
             'split_files/vae/qwen_image_vae.safetensors',
             'vae/qwen_image_vae.safetensors', 253806246),
        ],
    },
}
SAMPLERS = ['euler', 'euler_ancestral', 'dpmpp_2m', 'dpmpp_2m_sde']
SCHEDULERS = ['simple', 'normal', 'beta']
MAX_PROMPT = 2000
MAX_IMAGES = 8
MAX_IMAGE = 32 * 1024 * 1024
WEIGHT_MANIFEST = 'weights.json'


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

'''

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
watch = Path('probe/watchdog.py').read_text()
# 只保留 ws_url / classify / format_progress / WatchHandle / watch_async，
# 去掉依赖独立模块的那些部分（这里用同一个文件内的 print，直接可用）。
watch = watch.replace('from __future__ import annotations\n\n', '')
watch = watch.replace('import json\nimport threading\nimport time\n\n', '')
watch = watch[:watch.index('def watch(')]
# 统一进度协议：内联块改用本文件顶部的 progress() 发 JSON
watch = watch.replace('''        line = format_progress(event, prompt_id, mapping)
        if line is None:
            return
        print(line, flush=True)
        emitted[0] += 1
        if on_line is not None:
            on_line(line)''', '''        info = progress_info(event, prompt_id, mapping)
        if info is None:
            return
        progress(dict(info, kind='step'))
        emitted[0] += 1''')

body = '''

# --- ComfyUI 交互 ---------------------------------------------------------

SERVER = 'http://127.0.0.1:8188'


def variants_of(request: dict) -> list[dict]:
    """把两种请求形状归一成「每张图一套参数」。

    老形状 = 一套共享参数 + `prompts[]`（页面上的"生成几张"）；
    新形状 = `variants[]`，每张图自带 prompt/尺寸/步数/CFG/种子（planner 规划出来的批量）。
    归一之后 workflow / execute / 账本只看这一种形状，两个入口不会各写一套逻辑。
    """
    raw = request.get('variants')
    if isinstance(raw, list):
        return [dict(item) for item in raw]
    prompts = request.get('prompts') or []
    return [{'prompt': prompt, 'negative': request.get('negative', ''),
             'width': request.get('width'), 'height': request.get('height'),
             'steps': request.get('steps'), 'cfg': request.get('cfg'),
             # 老形状的多样性是「种子按序号递增」，这里保持一模一样
             'seed': request.get('seed') + index}
            for index, prompt in enumerate(prompts)]


def validate(request: dict) -> dict:
    if not isinstance(request, dict):
        raise ValueError('Request must be an object')
    from request import repair_region
    if 'region' in request:
        request['region'] = repair_region(request['region'], request.get('mode', 'txt2img'))
        if request['region'] is None:
            raise ValueError('Repair region must be a nonempty rectangle')
    if request.get('workerRevision') != revision():
        raise ValueError(
            'Worker revision mismatch; deploy the current worker first '
            '(request=' + str(request.get('workerRevision'))[:16] +
            ' deployed=' + revision()[:16] + ')')
    mode = request.get('mode', 'txt2img')
    if mode not in MODES:
        raise ValueError('Unsupported mode: ' + str(mode))
    spec = MODELS[MODEL_FOR_MODE[mode]]
    if request.get('model') != spec['repo']:
        raise ValueError('Unexpected model for mode ' + mode)
    if request.get('modelRevision') != spec['revision']:
        raise ValueError('Pinned model revision mismatch')
    if request.get('quantization') != spec['transformer']:
        raise ValueError('Unexpected quantization')
    if request.get('comfyRevision') != COMFY_REVISION or request.get('ggufRevision') != GGUF_REVISION:
        raise ValueError('Pinned ComfyUI/GGUF revision mismatch')
    if not re.fullmatch('[a-f0-9]{64}', str(request.get('key', ''))):
        raise ValueError('Invalid key')
    variants = variants_of(request)
    if not 1 <= len(variants) <= MAX_IMAGES:
        raise ValueError('Variant count bounds')

    # 图生图允许空提示词（就是「以这张图为起点再画一遍」），
    # 文生图和指令编辑必须有提示词，否则没有可执行的内容。
    def bad_prompt(value):
        return (not isinstance(value, str) or len(value) > MAX_PROMPT
                or (mode != 'img2img' and not value.strip()))

    # 逐条校验：每张图都可能有自己的尺寸/步数/CFG/种子，任何一条越界都拒掉整批
    for index, variant in enumerate(variants, start=1):
        if bad_prompt(variant.get('prompt')):
            raise ValueError(f'Prompt bounds (variant {index})')
        negative = variant.get('negative')
        if negative is not None and (not isinstance(negative, str)
                                     or len(negative) > MAX_PROMPT):
            raise ValueError(f'Negative prompt bounds (variant {index})')
        seed = variant.get('seed')
        if type(seed) is not int or not 0 <= seed < 2 ** 63:
            raise ValueError(f'Seed bounds (variant {index})')
        steps = variant.get('steps')
        if type(steps) is not int or not 1 <= steps <= 40:
            raise ValueError(f'Steps bounds (variant {index})')
        cfg = variant.get('cfg')
        if type(cfg) not in (int, float) or not math.isfinite(cfg) or not 0 <= cfg <= 10:
            raise ValueError(f'CFG bounds (variant {index})')
        if mode == 'txt2img':
            # 文生图自己定尺寸；另两种模式的尺寸由输入图决定（FluxKontextImageScale 吸附档位）
            for field in ('width', 'height'):
                value = variant.get(field)
                if type(value) is not int or not 512 <= value <= 1536 or value % 8:
                    raise ValueError(f'{field} bounds (variant {index})')
    if mode == 'img2img':
        denoise = request.get('denoise')
        if type(denoise) not in (int, float) or not 0 < float(denoise) <= 1:
            raise ValueError('Denoise bounds')
    if mode != 'txt2img' and not re.fullmatch('[a-f0-9]{64}', str(request.get('inputSha256', ''))):
        raise ValueError('Input image hash required')
    if request.get('sampler') not in SAMPLERS:
        raise ValueError('Unsupported sampler')
    if request.get('scheduler') not in SCHEDULERS:
        raise ValueError('Unsupported scheduler')
    return request


def branch(index: int, request: dict, variant: dict) -> dict:
    """一张图的节点：尺寸/步数/CFG/种子/提示词全部来自这一条 variant。"""
    mode = request.get('mode', 'txt2img')
    prompt = variant['prompt']
    # 指令编辑要把模型接在 CFGNorm -> ModelSamplingAuraFlow 之后（官方 2509 配方）
    sampler_model = ['shift', 0] if mode == 'edit' else ['model', 0]
    if mode == 'txt2img':
        latent = {'class_type': 'EmptyLatentImage',
                  'inputs': {'width': variant['width'], 'height': variant['height'],
                             'batch_size': 1}}
    else:
        # 采样起点是输入图编码后的潜变量，不是空潜变量：这正是编辑与图生图的关键差别
        latent = {'class_type': 'VAEEncode',
                  'inputs': {'pixels': ['scale', 0], 'vae': ['vae', 0]}}
    # 图生图的「起点重叠度」；编辑必须从头采样（参考信息在 conditioning 里）
    denoise = float(request.get('denoise', 1.0)) if mode == 'img2img' else 1.0
    if mode == 'edit':
        encode = lambda text: {'class_type': 'TextEncodeQwenImageEditPlus',
                               'inputs': {'prompt': text, 'clip': ['encoder', 0],
                                          'vae': ['vae', 0], 'image1': ['scale', 0]}}
    else:
        encode = lambda text: {'class_type': 'CLIPTextEncode',
                               'inputs': {'text': text, 'clip': ['encoder', 0]}}
    nodes = {
        'text' + str(index): encode(prompt),
        # 负向也按图各编一份：批量里每条可以有各自的 negative
        'negtext' + str(index): encode(variant.get('negative') or ''),
        'latent' + str(index): latent,
        'sampler' + str(index): {'class_type': 'KSampler',
                                 'inputs': {'seed': variant['seed'],
                                            'steps': variant['steps'],
                                            'cfg': variant['cfg'],
                                            'sampler_name': request['sampler'],
                                            'scheduler': request['scheduler'],
                                            'denoise': denoise,
                                            'model': sampler_model,
                                            'positive': ['text' + str(index), 0],
                                            'negative': ['negtext' + str(index), 0],
                                            'latent_image': ['latent' + str(index), 0]}},
        'decode' + str(index): {'class_type': 'VAEDecode',
                               'inputs': {'samples': ['sampler' + str(index), 0],
                                          'vae': ['vae', 0]}},
        'save' + str(index): {'class_type': 'SaveImage',
                              'inputs': {'filename_prefix': 'aladin-' + str(index + 1).zfill(2),
                                         'images': ['decode' + str(index), 0]}},
    }
    return nodes


def workflow(request: dict) -> dict:
    mode = request.get('mode', 'txt2img')
    spec = MODELS[MODEL_FOR_MODE[mode]]
    graph = {
        'model': {'class_type': 'UnetLoaderGGUF',
                  'inputs': {'unet_name': spec['transformer']}},
        'encoder': {'class_type': 'CLIPLoader',
                    'inputs': {'clip_name': spec['encoder'], 'type': 'qwen_image'}},
        'vae': {'class_type': 'VAELoader', 'inputs': {'vae_name': spec['vae']}},
    }
    if mode != 'txt2img':
        # 输入图统一吸附到一个受支持的尺寸档位，顺带解决任意宽高比
        graph['input'] = {'class_type': 'LoadImage',
                              'inputs': {'image': request['inputName']}}
        graph['scale'] = {'class_type': 'FluxKontextImageScale',
                              'inputs': {'image': ['input', 0]}}
    if mode == 'edit':
        graph['cfgnorm'] = {'class_type': 'CFGNorm',
                                'inputs': {'model': ['model', 0], 'strength': 1.0}}
        graph['shift'] = {'class_type': 'ModelSamplingAuraFlow',
                              'inputs': {'model': ['cfgnorm', 0], 'shift': 3.0}}
    for index, variant in enumerate(variants_of(request)):
        graph.update(branch(index, request, variant))
    return graph


def save_nodes(request: dict) -> list:
    return ['save' + str(index) for index in range(len(variants_of(request)))]


def cached(manifest: dict, model_name: str, root: Path) -> bool:
    # 老 manifest 的 model 字段存的是仓库名，新的是模型名；两者都认，
    # 否则升级后会白白重下 Volume 里已经缓存好的十几 GB。
    if manifest.get('model') not in {model_name, MODELS[model_name]['repo']}:
        return False
    # 必须拿**当前**钉版的 files 来验，不能拿 manifest 里记的那套：换量化
    # （Q4_K_M → Q8_0）时磁盘上旧文件还在、旧 manifest 也自洽，用它判会一直命中，
    # 新权重永远下不下来（实测：ComfyUI 直接报 unet_name not in list）。
    for _repo, _revision, _hub, local, size in MODELS[model_name]['files']:
        path = root / local
        if not path.is_file() or path.stat().st_size != size:
            return False
    return True


def ensure_weights(root: Path, model_name: str) -> dict:
    """确保某个模型的全套权重都在本地，返回 manifest。

    按模型而非 revision 分组：编辑模型与文生图来自不同仓库、各有自己的 revision。
    仓库内路径可能带前缀（Comfy-Org 的是 split_files/），下载后统一归位到本地布局，
    否则 extra_model_paths 指不到。
    """
    spec = MODELS[model_name]
    manifest_path = root / (WEIGHT_MANIFEST if model_name == 'qwen-image-2.1'
                            else 'weights-' + model_name + '.json')
    manifest = json.loads(manifest_path.read_text()) if manifest_path.is_file() else {}
    if cached(manifest, model_name, root):
        say('weight cache hit for ' + model_name)
        return manifest
    import shutil
    from huggingface_hub import hf_hub_download
    root.mkdir(parents=True, exist_ok=True)
    files = []
    for repo, revision, hub, local, size in spec['files']:
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
        files.append({'hub': hub, 'path': local, 'bytes': target.stat().st_size})
    manifest = {'model': model_name, 'repo': spec['repo'],
                'revision': spec['revision'], 'quantization': spec['transformer'],
                'comfyRevision': COMFY_REVISION, 'ggufNode': GGUF_REVISION,
                'files': files, 'verifiedAt': round(time.time(), 3)}
    atomic_json(manifest_path, manifest)
    return manifest


def extra_model_paths(root: Path) -> Path:
    path = Path('/tmp/extra_model_paths.yaml')
    path.write_text('aladin:\\n    base_path: ' + str(root) +
                    '\\n    diffusion_models: diffusion_models'
                    '\\n    text_encoders: text_encoders\\n    vae: vae\\n')
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
                raise RuntimeError('ComfyUI 进程退出；日志尾部: ' + tail('/tmp/comfy-server.log', 5))
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


def fetch_image(item: dict, deadline: float) -> bytes:
    query = urllib.parse.urlencode({'filename': item['filename'],
                                    'subfolder': item.get('subfolder', ''),
                                    'type': item.get('type', 'output')})
    while time.time() < deadline:
        try:
            data = urllib.request.urlopen(SERVER + '/view?' + query, timeout=60).read()
            if data:
                return data
        except Exception:
            time.sleep(0.5)
    raise RuntimeError('无法取回图像 ' + str(item))


def receipt_hit(output: Path, request: dict) -> dict | None:
    """回执短路：同参数重跑直接返回已有结果，不碰 GPU。

    这是安全重试的前提——没有它，worker 的每次重试都等于重烧一次 GPU。
    产物也要逐个校验 sha256；对不上就当作没有回执，重新生成。
    """
    receipt = output / 'result.json'
    if not receipt.is_file():
        return None
    try:
        cached = json.loads(receipt.read_text())
    except ValueError:
        return None
    if cached.get('request') != request:
        return None
    for item in cached.get('images', []):
        path = output / item.get('file', '')
        if not path.is_file() or digest(path.read_bytes()) != item.get('sha256'):
            say('回执存在但产物不符；重新生成')
            return None
    return cached


def write_input(source: bytes, expected_sha: str) -> str:
    """把输入图写进 ComfyUI 的 input 目录，返回给 LoadImage 用的文件名。

    这里再验一次哈希：容器拒收与请求声明不符的字节，避免「这张图」和
    「账本上记的那张图」不是同一张。
    """
    if digest(source) != expected_sha:
        raise ValueError('Input image hash mismatch')
    if source[:8] == b'\\x89PNG\\r\\n\\x1a\\n':
        extension = '.png'
    elif source[:3] == b'\\xff\\xd8\\xff':
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


def repair_source(source: bytes, region: list):
    """Edit a context crop; composite only inside the selected rectangle."""
    import io
    from PIL import Image, ImageOps
    with Image.open(io.BytesIO(source)) as image:
        if image.width * image.height > 16_000_000:
            raise ValueError('局部修复输入最多 1600 万像素')
        oriented = ImageOps.exif_transpose(image)
        original = oriented.convert('RGBA' if 'A' in oriented.getbands() or 'transparency' in oriented.info else 'RGB')
    w, h = original.size
    x0, y0, x1, y1 = region
    box = (int(x0*w), int(y0*h), min(w, math.ceil(x1*w)), min(h, math.ceil(y1*h)))
    if box[2]-box[0] < 8 or box[3]-box[1] < 8:
        raise ValueError('修复区域至少为 8×8 像素')
    pad = max(32, int(max(box[2]-box[0], box[3]-box[1]) * .5))
    context = (max(0, box[0]-pad), max(0, box[1]-pad), min(w, box[2]+pad), min(h, box[3]+pad))
    buffer = io.BytesIO()
    original.crop(context).convert('RGB').save(buffer, format='PNG')
    return buffer.getvalue(), (original, box, context)


def repair_composite(data: bytes, state) -> bytes:
    import io
    from PIL import Image, ImageDraw, ImageFilter
    original, box, context = state
    with Image.open(io.BytesIO(data)) as generated:
        crop = generated.convert('RGB').resize((context[2]-context[0], context[3]-context[1]), Image.Resampling.LANCZOS)
    patch = crop.crop((box[0]-context[0], box[1]-context[1], box[2]-context[0], box[3]-context[1]))
    # Feather inward; the paste bounds guarantee no pixels outside box change.
    mask = Image.new('L', patch.size, 0)
    inset = max(1, min(12, min(patch.size)//8))
    ImageDraw.Draw(mask).rectangle((inset, inset, patch.width-1-inset, patch.height-1-inset), fill=255)
    mask = mask.filter(ImageFilter.GaussianBlur(inset/2))
    result = original.copy()
    result.paste(patch, box[:2], mask)
    buffer = io.BytesIO()
    result.save(buffer, format='PNG')
    return buffer.getvalue()


class EarlyPublisher:
    """逐张回传：某张图的 SaveImage 一执行完就取图、写 Volume、提交，再通知宿主去拉。

    纯体验优化。任何一步失败只记一行日志——整批结束后的正常流程会补上这张，
    宿主也仍以 result.json 为权威清单。
    """

    def __init__(self, prompt_id: str, nodes: list, store, commit=None):
        self.prompt_id = prompt_id
        self.nodes = list(nodes)
        self.store = store
        self.commit = commit
        self.done: dict = {}
        self.queue: queue.Queue = queue.Queue()
        self.thread = threading.Thread(target=self._run, daemon=True, name='early-publish')
        self.thread.start()

    def on_event(self, event: dict) -> None:
        # 没有 commit 就没法让宿主提前读到文件：退回整批结束后统一回传
        if self.commit is None or event.get('type') != 'executed':
            return
        data = event.get('data') or {}
        if data.get('prompt_id') != self.prompt_id or data.get('node') not in self.nodes:
            return
        images = (data.get('output') or {}).get('images') or []
        if images:
            self.queue.put((self.nodes.index(data['node']), images[0]))

    def _run(self) -> None:
        while True:
            job = self.queue.get()
            if job is None:
                return
            index, item = job
            if index in self.done:
                continue
            try:
                record = self.store(index, item)
                if self.commit is not None:
                    self.commit()
                self.done[index] = record
                progress(dict({key: record[key] for key in
                               ('index', 'file', 'sha256', 'bytes', 'seed', 'prompt', 'params')},
                              kind='artifact', total=len(self.nodes)))
            except Exception as error:
                say('逐张回传失败，整批结束后照常回传: ' + str(error)[:300])

    def close(self, timeout: float = 180.0) -> None:
        """等排队中的回传做完。之后主流程再补没回传成功的那几张，避免两边同时写一个文件。"""
        self.queue.put(None)
        self.thread.join(timeout)


def execute(request: dict, result_root: str, source: bytes = b'', commit=None) -> dict:
    """`commit` 是结果 Volume 的提交函数（由 Modal app 传入）；给了才会逐张回传。"""
    request = validate(json.loads(json.dumps(request)))
    started = time.time()
    mode = request.get('mode', 'txt2img')
    spec = MODELS[MODEL_FOR_MODE[mode]]
    root = Path('/models/qwen-image')
    ensure_weights(root, MODEL_FOR_MODE[mode])
    output = Path(result_root) / request['key']
    output.mkdir(parents=True, exist_ok=True)
    repair_state = None
    if mode != 'txt2img':
        # 文件名由哈希决定，每次运行都一致。必须赶在回执比对之前算好：
        # 它会被写进 record[request]，若两次运行的 request 不同，缓存永远命中不了。
        request['inputName'] = write_input(source, request['inputSha256'])
        if request.get('region') is not None:
            crop, repair_state = repair_source(source, request['region'])
            request['inputName'] = write_input(crop, digest(crop))
        say('input image ' + request['inputName'] + ' (' + str(len(source)) + ' bytes)')
    cached = receipt_hit(output, request)
    if cached is not None:
        say('receipt hit; returning cached result')
        return cached

    process, _log = start_server(root)
    try:
        graph = workflow(request)
        nodes = save_nodes(request)
        client_id = 'aladin-' + request['key'][:16]
        prompt_id = str(uuid.uuid4())          # ComfyUI 只接受规范 UUID
        mapping = classify(graph)
        variants = variants_of(request)

        def store(index: int, item: dict) -> dict:
            """取回第 index 张、写进结果目录，返回产物清单里的那一项。"""
            variant = variants[index]
            data = fetch_image(item, time.time() + 120)
            if repair_state is not None:
                data = repair_composite(data, repair_state)
            if len(data) > MAX_IMAGE:
                raise ValueError('图像超出上限')
            name = 'image-' + str(index + 1).zfill(2) + '.png'
            temporary = output / (name + '.tmp')
            temporary.write_bytes(data)
            temporary.replace(output / name)
            return {'index': index + 1, 'file': name, 'sha256': digest(data),
                    'bytes': len(data), 'format': 'png',
                    'prompt': variant['prompt'], 'seed': variant['seed'],
                    # 产物级参数：批量里每张都可能不同，账本必须逐张记下来
                    'params': {'prompt': variant['prompt'],
                               'negative': variant.get('negative') or '',
                               'width': variant.get('width'),
                               'height': variant.get('height'),
                               'steps': variant['steps'], 'cfg': variant['cfg'],
                               'seed': variant['seed'],
                               'sampler': request['sampler'],
                               'scheduler': request['scheduler'],
                               **({'region': request['region'],
                                   'width': repair_state[0].width,
                                   'height': repair_state[0].height,
                                   'repairMethod': 'context-crop-edit-composite-v1'}
                                  if repair_state is not None else {})}}

        early = EarlyPublisher(prompt_id, nodes, store, commit)
        handle = watch_async(SERVER, prompt_id, mapping, timeout=1800.0,
                             client_id=client_id, on_event=early.on_event)
        if not handle.ready.wait(timeout=30):
            raise RuntimeError('WebSocket 订阅未能在 30s 内建立')
        progress({'kind': 'queued', 'job_key': request['key']})
        submit(graph, client_id, prompt_id)

        history = wait_for_history(prompt_id, time.time() + 1500)
        if history is None:
            raise RuntimeError('ComfyUI 未在期限内产出结果')
        entry = history[prompt_id]
        if entry.get('status', {}).get('status_str') == 'error':
            raise RuntimeError('ComfyUI 执行失败: ' + json.dumps(entry.get('status'))[:500])

        early.close()
        images = []
        for index, node in enumerate(nodes):
            if index in early.done:
                images.append(early.done[index])
                continue
            item = entry['outputs'][node]['images'][0]
            images.append(store(index, item))
        record = {'schemaVersion': 1, 'request': request, 'mode': mode,
                  'model': spec['repo'], 'quantization': spec['transformer'],
                  'elapsedSeconds': round(time.time() - started, 2),
                  'images': images, 'serverLogTail': tail('/tmp/comfy-server.log', 8)}
        atomic_json(output / 'result.json', record)
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
'''

Path('aladin/worker.py').write_text(header + watch + body)
print('worker.py written')
