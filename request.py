"""构造发给 Modal 的生成请求。

本地（幂等键、展示）与容器内（校验 workerRevision 与模型钉版）都要用同一份逻辑，
所以放在顶层模块，两个环境都 import，避免两份实现漂移。
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

WORKER_PATH = Path(__file__).resolve().parent / 'aladin' / 'worker.py'

# 与 Modal 镜像里钉住的 ComfyUI / ComfyUI-GGUF 提交号一致（见 aladin_modal_app.py）。
# 容器会拿这两个值跟自己比对，不一致直接拒绝——镜像换了这里必须同步。
COMFY_REVISION = 'c194dd00cd42aa18d9dbf27d977bf6b85d9ea565'
GGUF_REVISION = 'edd981b10e107d3b8f58e16c498f2d08f631bc47'


def repair_region(value, mode='edit'):
    """Normalized rectangle, shared by form, JSON API and GPU worker."""
    if value is None or value == '':
        return None
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            raise ValueError('修复区域必须是 [左, 上, 右, 下]') from None
    if (mode != 'edit' or not isinstance(value, list) or len(value) != 4
            or any(type(v) not in (int, float) or not math.isfinite(v) for v in value)):
        raise ValueError('局部修复仅支持指令编辑，区域需为四个 0–1 数值')
    x0, y0, x1, y1 = value
    if not (0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1):
        raise ValueError('修复区域超出图片或为空')
    return list(value)

MODES = ('txt2img', 'img2img', 'edit')
# 文生图与图生图共用一套权重；指令编辑是另一套（连文本编码器和 VAE 都不通用）
MODEL_FOR_MODE = {'txt2img': 'qwen-image-2.1', 'img2img': 'qwen-image-2.1',
                 'edit': 'qwen-image-edit-2509'}

MODELS = {
    # 文生图 / 图生图：固定作者明确标注的 UC 文件，不使用 base 分支的普通 Q8。
    'qwen-image-2.1': {
        'repo': 'abenzerps/Qwen-Image-2.1-Uncensored-GGUF',
        'revision': '1206d38bb47ef93961bfb77bc2c700d43a25860e',
        'transformer': 'qwen-image-2.1-UC-Q8_0.gguf',
        'encoder': 'text_encoders/qwen3vl_8b_int8_convrot.safetensors',
        'vae': 'vae/qwen_image_2.1_vae_bf16.safetensors',
    },
    # 指令编辑：transformer 来自 QuantStack 的 GGUF，编码器/VAE 来自 ComfyUI 官方重打包
    'qwen-image-edit-2509': {
        'repo': 'QuantStack/Qwen-Image-Edit-2509-GGUF',
        'revision': '84a3006979126011422eeeefe0c9485ddf431ef5',
        'transformer': 'Qwen-Image-Edit-2509-Q4_K_M.gguf',
        'encoder': 'qwen_2.5_vl_7b_fp8_scaled.safetensors',
        'encoder_repo': 'Comfy-Org/Qwen-Image_ComfyUI',
        'encoder_revision': '7beb7b647f04469fbe64ba8adc2bb0d7e5e9f73f',
        'encoder_hub': 'split_files/text_encoders/qwen_2.5_vl_7b_fp8_scaled.safetensors',
        'vae': 'qwen_image_vae.safetensors',
        'vae_repo': 'Comfy-Org/Qwen-Image_ComfyUI',
        'vae_revision': '7beb7b647f04469fbe64ba8adc2bb0d7e5e9f73f',
        'vae_hub': 'split_files/vae/qwen_image_vae.safetensors',
    },
}


def worker_revision() -> str:
    """容器内 worker.revision() 就是 worker.py 自身的 sha256。

    本地提前算好并放进请求，理由有两条：
    1. request 的完整内容参与结果目录的 receipt 比对，容器若改写它，
       同参数重跑将永远命中不了缓存。
    2. 部署与请求版本不一致时，容器会直接以 revision mismatch 拒绝，这是想要的。
    """
    return hashlib.sha256(WORKER_PATH.read_bytes()).hexdigest()


def model_config(mode: str) -> dict:
    if mode not in MODEL_FOR_MODE:
        raise ValueError('未知模式: ' + str(mode))
    return MODELS[MODEL_FOR_MODE[mode]]


def storage_key(prompt: str, images: int, seed: int, width: int, height: int,
                steps: int, cfg: float, sampler: str, scheduler: str,
                negative: str = '', mode: str = 'txt2img',
                input_sha256: str = '', denoise: float = 1.0, region=None) -> str:
    """64 位 hex 结果目录名。

    内容是**全部影响输出的参数**，因此同参数重跑会命中容器内的结果回执，天然幂等。
    input_sha256 必须参与：否则同一句指令配两张不同的输入图会算出同一个键，
    被唯一约束拦下，第二张图永远提交不了。
    """
    shape = json.dumps({
        'prompt': prompt, 'images': images, 'seed': seed, 'width': width,
        'height': height, 'steps': steps, 'cfg': cfg, 'sampler': sampler,
        'scheduler': scheduler, 'negative': negative, 'mode': mode,
        'inputSha256': input_sha256, 'denoise': denoise,
        **({'region': repair_region(region, mode)} if region is not None else {}),
        'workerRevision': worker_revision(), 'modelConfig': model_config(mode),
        'comfyRevision': COMFY_REVISION, 'ggufRevision': GGUF_REVISION,
    }, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(shape.encode()).hexdigest()


def storage_key_for(prompt: str, images: int, params: dict, mode: str = 'txt2img',
                    input_sha256: str = '') -> str:
    """从 params 字典算幂等键。

    pipeline 与 API 都用它。归一化（prompt strip、denoise 只在 img2img 生效）
    只此一处——曾经两边各写一遍，任何一处走样都会让 409 反查查不到任务。
    """
    from aladin.prompt_defaults import effective_prompt
    prompt = effective_prompt(prompt, params)
    if mode == 'txt2img' and params.get('model', 'qwen-image-2.1') != 'qwen-image-2.1':
        from aladin.extra_image_request import build as build_extra
        return build_extra(prompt, images, params)['key']
    return storage_key(
        prompt=prompt.strip(), images=images, seed=params['seed'],
        width=params['width'], height=params['height'], steps=params['steps'],
        cfg=params['cfg'], sampler=params['sampler'], scheduler=params['scheduler'],
        negative=params.get('negative', ''), mode=mode, input_sha256=input_sha256,
        denoise=params.get('denoise', 1.0) if mode == 'img2img' else 1.0,
        region=params.get('region'))


def build(key: str, prompt: str, images: int, seed: int, width: int, height: int,
          steps: int, cfg: float, sampler: str, scheduler: str,
          negative: str = '', mode: str = 'txt2img',
          input_sha256: str = '', denoise: float = 1.0, region=None) -> dict:
    model = model_config(mode)
    return {
        'workerRevision': worker_revision(),
        'comfyRevision': COMFY_REVISION,
        'ggufRevision': GGUF_REVISION,
        'mode': mode,
        'model': model['repo'],
        'modelRevision': model['revision'],
        'quantization': model['transformer'],
        'key': key,
        'prompts': [prompt] * images,
        'negative': negative,
        'width': width,
        'height': height,
        'seed': seed,
        'steps': steps,
        'cfg': cfg,
        'sampler': sampler,
        'scheduler': scheduler,
        'denoise': denoise,
        'inputSha256': input_sha256,
        **({'region': repair_region(region, mode)} if region is not None else {}),
    }


def build_variants(key: str, variants: list[dict], sampler: str, scheduler: str) -> dict:
    """批量请求：每张图自带 prompt/尺寸/步数/CFG/种子（planner 规划出来的形状）。

    sampler/scheduler 仍是整批共享的——它们描述采样过程，不是"这张图"的属性；
    由调用方传入而不是在这里 import settings：本模块要能在容器里 import，
    容器只挂了本文件与 worker.py，多一个 import 就多一处装配依赖。
    """
    model = model_config('txt2img')
    return {
        'workerRevision': worker_revision(),
        'comfyRevision': COMFY_REVISION,
        'ggufRevision': GGUF_REVISION,
        'mode': 'txt2img',
        'model': model['repo'],
        'modelRevision': model['revision'],
        'quantization': model['transformer'],
        'key': key,
        'variants': [{'prompt': item['prompt'],
                      'negative': item.get('negative', ''),
                      'width': item['width'], 'height': item['height'],
                      'steps': item['steps'], 'cfg': item['cfg'], 'seed': item['seed']}
                     for item in variants],
        'sampler': sampler,
        'scheduler': scheduler,
    }


def storage_key_variants(variants: list[dict]) -> str:
    """批量任务的幂等键：任一条 prompt/参数/种子不同，就是另一个任务。"""
    shape = json.dumps(
        [{key: item.get(key) for key in ('prompt', 'negative', 'width', 'height',
                                         'steps', 'cfg', 'seed')}
         for item in variants], sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(shape.encode()).hexdigest()
