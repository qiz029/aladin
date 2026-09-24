"""收藏：把生成产物「提升」为独立副本（长期保留）。

收藏必须是**独立副本**，不能只存一个指向 `data/jobs/` 的路径。生成历史是可清理的，
若收藏只存引用，清理历史就会把它一起弄丢——那和"长期保留"矛盾。
副本按 sha256 命名、**沿用原扩展名**（图片 .png、视频 .webm），
因此同一个产物重复加入只会有一份文件，页面也能靠扩展名决定用 <img> 还是 <video>。
"""
from __future__ import annotations

import shutil
from pathlib import Path

from . import db, settings

# 允许长期保留的产物类型。白名单而不是黑名单：文件名来自容器产物清单，
# 放进来一个 .html/.svg 就会被同源提供给浏览器。
ALLOWED_SUFFIXES = ('.png', '.jpg', '.jpeg', '.webm')


def promote(job_id: str, name: str) -> bool:
    """把某个产物复制进收藏。已收藏过则返回 False；类型不在白名单里抛 ValueError。"""
    row = db.artifact(job_id, name)
    if row is None:
        raise LookupError('产物不存在')
    source = db.data_path(row['rel_path'])
    if not source.is_file():
        raise FileNotFoundError('产物文件不在本地')

    suffix = Path(name).suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        raise ValueError('不支持的产物类型: ' + (suffix or '(无扩展名)'))
    target = settings.GALLERY_DIR / f'{row["sha256"][:16]}{suffix}'
    if not target.is_file():
        settings.GALLERY_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)     # 复制而非移动：生成历史里的原件保留
    job = db.job(job_id) or {}
    return db.add_to_gallery(
        rel_path=str(target.relative_to(settings.DATA)), prompt=row['prompt'],
        seed=row['seed'], sha256=row['sha256'], size=row['bytes'],
        source_job=job_id, **source_of(job))


# 收藏里按模型筛选时显示的名字；key 与 source_of() 的取值一致
MODEL_LABELS = {'qwen-image-2.1': 'Qwen-Image 2.1', 'anima-base-1.0': 'Anima Base 1.0',
                'pony-realism-2.2': 'Pony Realism 2.2',
                'qwen-image-edit-2509': 'Qwen-Image-Edit 2509', '10eros-max': '10Eros-Max 视频'}
MAX_TAGS = 20
MAX_TAG_CHARS = 32


def source_of(job: dict) -> dict:
    """收藏时从任务抄下来源：任务之后可以被删，归类不能跟着丢。与迁移 0009 的回填规则一致。"""
    if not job:
        return {'model': None, 'mode': None, 'rating': None}
    params = job.get('params') or {}
    if (job.get('app') or 'image') == 'video':
        model = '10eros-max'
    elif job.get('mode') == 'edit':
        model = 'qwen-image-edit-2509'
    else:
        model = params.get('model') or 'qwen-image-2.1'
    return {'model': model, 'mode': job.get('mode'), 'rating': params.get('rating')}


def normalize_tags(values) -> tuple[list[str], str]:
    """去空白、合并连续空格、大小写不敏感去重，保留第一次出现的写法。返回 (标签, 错误)。"""
    if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
        return [], '标签必须是字符串数组'
    tags, seen = [], set()
    for value in values:
        tag = ' '.join(value.split())
        if not tag:
            continue
        if len(tag) > MAX_TAG_CHARS:
            return [], f'单个标签最多 {MAX_TAG_CHARS} 字符'
        if tag.casefold() not in seen:
            seen.add(tag.casefold())
            tags.append(tag)
    if len(tags) > MAX_TAGS:
        return [], f'最多 {MAX_TAGS} 个标签'
    return tags, ''


def remove(item_id: int) -> bool:
    """从图库移除。只删除图库自己那份副本；生成历史里的原件不动。"""
    row = db.remove_from_gallery(item_id)
    if row is None:
        return False
    path = db.data_path(row['rel_path'])
    # 同一张图（sha256 唯一）不会有第二条，但生成历史里的文件路径不同，不受影响
    if path.is_file() and not db.gallery_has_path(row['rel_path']):
        path.unlink()
    return True
