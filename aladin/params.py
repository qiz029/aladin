"""表单与 API 共用的参数校验和上传处理。

放在这里而不是 web.py：验证规则必须只有一份。曾经 web 与容器各写一份边界，
改一边忘一边就会变成「本地放过、容器拒绝」这种最难查的失败。
"""
from __future__ import annotations

import hashlib
import secrets

from fastapi import UploadFile

from . import settings
from .image_models import MODELS, DEFAULT_MODEL
from .prompt_defaults import default_rating, family_for, rating_error, validate_length

MODES = ('img2img', 'edit')
# 随机种子取 32 位：所有采样器都安全，批量里 seed+i 也不会越界
RANDOM_SEED_BOUND = 2 ** 32


def resolve_seed(seed: int | None) -> int:
    """None 表示「随机」：在提交时定下真实值并写进 params，复现与幂等键都用这个值。"""
    return secrets.randbelow(RANDOM_SEED_BOUND) if seed is None else seed


def parse_seed(text: str | None) -> tuple[int | None, str]:
    """表单里的种子：留空为随机，否则必须是非负整数。返回 (种子, 错误)。"""
    text = (text or '').strip()
    if not text:
        return None, ''
    if not text.isdigit():
        return None, '随机种子需为非负整数，留空表示随机'
    return int(text), ''


def image_params(prompt: str, images: int, size: str, negative: str, steps: int,
                 cfg: float, sampler: str, scheduler: str,
                 seed: int | None, model: str = DEFAULT_MODEL, *, prompt_tags=None,
                 rating: str | None = None, loras=None) -> tuple[dict, list[str]]:
    """文生图参数。尺寸是预设键，解析成宽高后仍按 PARAM_LIMITS 校验一遍。"""
    spec = MODELS.get(model, MODELS[DEFAULT_MODEL])
    defaults = spec['defaults']
    size = size if size is not None else defaults['size']
    negative = negative if negative is not None else defaults['negative']
    steps = steps if steps is not None else defaults['steps']
    cfg = cfg if cfg is not None else defaults['cfg']
    sampler = sampler if sampler is not None else defaults['sampler']
    scheduler = scheduler if scheduler is not None else defaults['scheduler']
    limits = dict(settings.PARAM_LIMITS, steps=spec['steps'])
    seed = resolve_seed(seed)
    errors: list[str] = []
    if model not in MODELS:
        errors.append('未知的生图模型')
    prompt = (prompt or '').strip()
    if not prompt:
        errors.append('提示词不能为空')
    elif len(prompt) > limits['prompt_chars']:
        errors.append(f'提示词最多 {limits["prompt_chars"]} 字符')
    if not limits['images'][0] <= images <= limits['images'][1]:
        errors.append(f'张数需在 {limits["images"][0]}–{limits["images"][1]} 之间')
    if len(negative or '') > limits['negative_chars']:
        errors.append(f'负向提示词最多 {limits["negative_chars"]} 字符')
    # 用户选的是预设键；这里校验的是预设配置本身没写错，否则要等到容器里才失败
    preset = spec['sizes'].get(size)
    if preset is None:
        errors.append('不支持的尺寸')
        preset = spec['sizes'][settings.DEFAULT_PARAMS['size']]
    width, height = preset['width'], preset['height']
    low, high = limits['size']
    for label, value in (('宽', width), ('高', height)):
        if not low <= value <= high or value % limits['size_multiple']:
            errors.append(
                f'{label}预设 {value} 不在 {low}–{high} 或不是'
                f' {limits["size_multiple"]} 的倍数')
    if not limits['steps'][0] <= steps <= limits['steps'][1]:
        errors.append(f'步数需在 {limits["steps"][0]}–{limits["steps"][1]} 之间')
    if not limits['cfg'][0] <= cfg <= limits['cfg'][1]:
        errors.append(f'CFG 需在 {limits["cfg"][0]}–{limits["cfg"][1]} 之间')
    if sampler not in spec['samplers']:
        errors.append('不支持的采样器')
    if scheduler not in spec['schedulers']:
        errors.append('不支持的调度器')
    if not limits['seed'][0] <= seed <= limits['seed'][1]:
        errors.append('随机种子超出范围')
    rating = _rating(rating, errors)
    from .loras import resolve, triggers_for
    chosen, lora_errors = resolve(loras, model)
    errors.extend(lora_errors)
    params = {'model': model, 'negative': negative or '', 'size': size, 'width': width,
              'height': height, 'steps': steps, 'cfg': cfg,
              'sampler': sampler, 'scheduler': scheduler, 'seed': seed,
              'rating': rating, 'prompt_family': family_for(model)}
    if chosen:
        # 触发词是这次任务的一部分：冻结进 prompt_defaults，复现与「再来一张」都沿用
        params['loras'] = chosen
        params['lora_triggers'] = triggers_for(chosen)
    return validate_length(prompt, params, limits['prompt_chars'], errors, prompt_tags), errors


def edit_params(mode: str, prompt: str, images: int, negative: str, steps: int,
                cfg: float, sampler: str, scheduler: str, seed: int | None,
                denoise: float, region=None, rating: str | None = None) -> tuple[dict, list[str]]:
    """改图参数。不定尺寸：输出尺寸跟输入图走，由容器吸附到受支持的档位。

    所以 params 里 width/height 记为 None——记一个假尺寸会让账本说谎。
    """
    limits = settings.PARAM_LIMITS
    seed = resolve_seed(seed)
    errors: list[str] = []
    if mode not in MODES:
        errors.append('未知的改图模式')
    prompt = (prompt or '').strip()
    if mode == 'edit' and not prompt:
        errors.append('指令编辑必须写清楚要改什么')
    if len(prompt) > limits['prompt_chars']:
        errors.append(f'提示词最多 {limits["prompt_chars"]} 字符')
    if not limits['images'][0] <= images <= limits['images'][1]:
        errors.append(f'张数需在 {limits["images"][0]}–{limits["images"][1]} 之间')
    if len(negative or '') > limits['negative_chars']:
        errors.append(f'负向提示词最多 {limits["negative_chars"]} 字符')
    if not limits['steps'][0] <= steps <= limits['steps'][1]:
        errors.append(f'步数需在 {limits["steps"][0]}–{limits["steps"][1]} 之间')
    if not limits['cfg'][0] <= cfg <= limits['cfg'][1]:
        errors.append(f'CFG 需在 {limits["cfg"][0]}–{limits["cfg"][1]} 之间')
    if sampler not in settings.SAMPLERS:
        errors.append('不支持的采样器')
    if scheduler not in settings.SCHEDULERS:
        errors.append('不支持的调度器')
    if not limits['seed'][0] <= seed <= limits['seed'][1]:
        errors.append('随机种子超出范围')
    if not 0 < denoise <= 1:
        errors.append('重绘幅度需在 0–1 之间')
    params = {'negative': negative or '', 'width': None, 'height': None,
              'steps': steps, 'cfg': cfg, 'sampler': sampler,
              'scheduler': scheduler, 'seed': seed, 'denoise': denoise,
              'rating': _rating(rating, errors), 'prompt_family': family_for()}
    from request import repair_region
    try:
        parsed = repair_region(region, mode)
        if parsed is not None:
            params['region'] = parsed
    except ValueError as error:
        errors.append(str(error))
    return validate_length(prompt, params, limits['prompt_chars'], errors), errors


def video_params(prompt: str, duration: str, size: str, seed: int | None,
                 model: str | None = None, loras=None,
                 rating: str | None = None) -> tuple[dict, list[str]]:
    """图生视频参数。边界与 aladin/video_worker.py 的 validate() 同一套。

    时长在实现上就是帧数（24fps、8n+1），页面上给的是「秒」的档位。distilled 模型的
    步数、CFG、采样器由官方模板固定，不对外开放。LoRA 按模型（ltx-2.3 / ltx-2.5）分族。
    """
    from .loras import resolve, triggers_for
    from .video_worker import MODELS

    limits = settings.VIDEO_LIMITS
    seed = resolve_seed(seed)
    errors: list[str] = []
    prompt = (prompt or '').strip()
    model = model or settings.VIDEO_DEFAULT_PARAMS['model']
    if model not in MODELS:
        errors.append('不支持的视频模型：' + str(model))
        model = settings.VIDEO_DEFAULT_PARAMS['model']
    # 允许空提示词：输入图已经承载了内容，提示词只是描述想要的运动。
    if len(prompt) > limits['prompt_chars']:
        errors.append(f'提示词最多 {limits["prompt_chars"]} 字符')
    slot = settings.VIDEO_DURATIONS.get(duration)
    if slot is None:
        errors.append('不支持的时长')
        slot = settings.VIDEO_DURATIONS[settings.VIDEO_DEFAULT_PARAMS['duration']]
    frames = slot['frames']
    low, high = limits['frames']
    if not low <= frames <= high or (frames - 1) % 8:
        errors.append(f'帧数需在 {low}–{high} 之间且满足 8n+1')
    preset = settings.VIDEO_SIZES.get(size)
    if preset is None:
        errors.append('不支持的尺寸')
        preset = settings.VIDEO_SIZES[settings.VIDEO_DEFAULT_PARAMS['size']]
    width, height = preset['width'], preset['height']
    size_low, size_high = limits['size']
    for label, value in (('宽', width), ('高', height)):
        if not size_low <= value <= size_high or value % limits['size_multiple']:
            errors.append(f'{label} {value} 不在 {size_low}–{size_high} 或不是'
                          f' {limits["size_multiple"]} 的倍数')
    if not limits['seed'][0] <= seed <= limits['seed'][1]:
        errors.append('随机种子超出范围')
    chosen, lora_errors = resolve(loras, model)
    errors.extend(lora_errors)
    params = {'model': model, 'duration': duration, 'size': size,
              'width': width, 'height': height, 'frames': frames,
              'fps': settings.VIDEO_FPS, 'seconds': round(frames / settings.VIDEO_FPS, 2),
              'seed': seed,
              # 账本只有 image_count 一列，视频固定记 1，免得模板与统计要分叉
              'images': 1,
              'rating': _rating(rating, errors), 'prompt_family': family_for(app='video')}
    if chosen:
        params['loras'] = chosen
        params['lora_triggers'] = triggers_for(chosen)
    return validate_length(prompt, params, limits['prompt_chars'], errors), errors


def repair_input_error(data: bytes, region) -> str:
    if region is None:
        return ''
    from .worker import repair_source
    try:
        repair_source(data, region)
    except Exception as error:
        return '局部修复图片无效：' + str(error)[:200]
    return ''


async def save_upload(upload: UploadFile, region=None) -> tuple[str, str, str]:
    """落盘上传的输入图，返回 (sha256, 相对路径, 错误)。

    边读边判大小：先 read() 再检查上限等于没有上限——一个 2 GB 的文件会先被整个吃进内存。
    按内容寻址存储，同一张图重复上传不会写第二份。
    """
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await upload.read(1 << 20)
        if not chunk:
            break
        total += len(chunk)
        if total > settings.MAX_UPLOAD_BYTES:
            limit = settings.MAX_UPLOAD_BYTES // (1 << 20)
            return '', '', f'图片不能超过 {limit} MiB'
        chunks.append(chunk)
    data = b''.join(chunks)
    error = repair_input_error(data, region)
    if error:
        return '', '', error
    stored = store_bytes(data)
    if stored is None:
        return '', '', '只支持 PNG 或 JPEG'
    return stored[0], stored[1], ''


def store_bytes(data: bytes) -> tuple[str, str] | None:
    """按内容寻址落盘，返回 (sha256, 相对路径)；不是 PNG/JPEG 时返回 None。

    base64 与 multipart 两条入口共用它，魔数判断只此一处。
    """
    if data[:8] == b'\x89PNG\r\n\x1a\n':
        suffix = '.png'
    elif data[:3] == b'\xff\xd8\xff':
        suffix = '.jpg'
    else:
        return None
    sha = hashlib.sha256(data).hexdigest()
    relative = 'uploads/' + sha + suffix
    path = settings.DATA / relative
    if not path.is_file():
        path.write_bytes(data)
    return sha, relative


def _rating(rating, errors: list[str]) -> str:
    """校验并归一尺度：非法值记一条错误，并退回默认值，让后续拼提示词不至于崩。"""
    error = rating_error(rating)
    if error:
        errors.append(error)
        return default_rating()
    return rating or default_rating()


def director_params(brief: str, count: int | None = None, rating: str | None = None,
                    model: str | None = None, loras=None,
                    review: bool = False, preset: str | None = None,
                    creative_spec=None) -> tuple[dict, list[str]]:
    """一句话出图：需求、张数、尺度、目标模型、LoRA（整批共用）、是否先看计划再生成、预设。

    预设 manga 的张数就是一页的格数（4–8，默认 6），版式随格数定。
    """
    from .planner_schema import (MANGA_DEFAULT, MANGA_LAYOUTS, MANGA_MAX, MANGA_MIN,
                                 MAX_BRIEF_CHARS, MAX_IMAGES, MIN_IMAGES, PRESETS)
    errors = []
    if preset is not None and preset not in PRESETS:
        errors.append('未知的预设：' + ' / '.join(PRESETS))
        preset = None
    brief = (brief or '').strip()
    if not brief:
        errors.append('请描述你想要的画面')
    elif len(brief) > MAX_BRIEF_CHARS:
        errors.append(f'需求最多 {MAX_BRIEF_CHARS} 字符')
    if preset == 'manga':
        count = MANGA_DEFAULT if count is None else count
        if type(count) is not int or count not in MANGA_LAYOUTS:
            errors.append(f'漫画格数需在 {MANGA_MIN}–{MANGA_MAX} 之间')
    elif count is not None and (type(count) is not int or not MIN_IMAGES <= count <= MAX_IMAGES):
        errors.append(f'张数需在 {MIN_IMAGES}–{MAX_IMAGES} 之间，或留空由模型决定')
    model = model or DEFAULT_MODEL
    if model not in MODELS:
        errors.append('未知的生图模型')
        model = DEFAULT_MODEL
    from .loras import resolve
    chosen, lora_errors = resolve(loras, model)
    errors.extend(lora_errors)
    if preset == 'manga':
        from .loras import BY_ID
        # LoRA 对每一格都生效：体位 / 视角类会让整页同一个姿势，故事就没了
        posed = [BY_ID[l['id']]['name'] for l in chosen if BY_ID[l['id']]['kind'] == 'pose']
        if posed:
            errors.append('漫画预设不能用体位 / 视角类 LoRA（会让每一格同一个姿势）：' + '、'.join(posed))
        # 漫画生成器类 LoRA 一张图就出一整页多格，放进逐格分镜里会变成「格中格」
        paged = [BY_ID[l['id']]['name'] for l in chosen if BY_ID[l['id']].get('group') == 'comic']
        if paged:
            errors.append('漫画预设已经逐格分镜，不能再用整页漫画生成器 LoRA：' + '、'.join(paged))
    if not isinstance(review, bool):
        errors.append('review 必须是布尔值')
        review = False
    built = {'count': count, 'stage': 'planning', 'rating': _rating(rating, errors),
             'model': model, 'loras': chosen, 'review': review}
    if preset:
        built['preset'] = preset
    from .planner_schema import creative_spec as validate_spec
    try:
        spec = validate_spec(creative_spec)
        if spec:
            built['creative_spec'] = spec
    except ValueError as error:
        errors.append(str(error))
    return built, errors
