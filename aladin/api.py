"""给 agent 用的 JSON API。

与 HTML 页面共用同一套校验（aladin/params.py）和同一条提交路径（pipeline.enqueue），
所以页面上能提交的，API 都能提交；反之亦然。没有第二份业务逻辑。

设计取舍（都是为了让 agent 好调用）：
- 提交返回 202 + 任务对象，不重定向；轮询或 wait=true 都可以。
- 撞上同参数的任务时返回 409，**并带上已存在任务的 id**——agent 可以顺下去查询，
  而不是只能从一段中文里猜。
- 能力与参数边界都自描述（GET /api/v1 与 /params），FastAPI 的 /docs 也直接可用。
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field, StrictFloat
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse, JSONResponse
from psycopg.errors import UniqueViolation

from . import billing, cleanup, db, director, gallery, params as rules, pipeline, settings
from .planner_schema import BEATS, MANGA_DEFAULT, MANGA_LAYOUTS, MANGA_MAX, MANGA_MIN, PRESETS, SHOTS, manga_layout

router = APIRouter(prefix='/api/v1')

TERMINAL_STATES = ('succeeded', 'failed')
WAIT_DEFAULT_SECONDS = 600
WAIT_MAX_SECONDS = 1800


class DirectorRequest(BaseModel):
    brief: str = Field(..., description='描述需求，最多 4000 字符')
    count: int | None = Field(None, strict=True, description='可选 1–8 张；省略由模型决定')
    rating: str | None = Field(None, description='尺度：general 日常 / suggestive 暗示 / explicit 露骨；省略用服务端默认')
    model: str = Field('qwen-image-2.1', description='目标模型：qwen-image-2.1 / pony-realism-2.2 / anima-base-1.0')
    loras: list['LoraChoice'] | None = Field(None, description='整批共用的 LoRA（仅 Pony / Anima）')
    review: bool = Field(False, description='true：规划完停在 review，调用 /jobs/{id}/approve 确认后才生成')
    preset: Literal['manga'] | None = Field(None, description='预设：manga = 日式漫画一页多格（count 为格数 4–8，默认 6）')
    creative_spec: dict | None = Field(None, description='创作要求：must_keep 必须保持 / may_change 允许变化 / change_only 本轮只改；每项最多 1000 字符')


class ArtifactReview(BaseModel):
    anatomy: Literal['pass', 'fail', 'unsure', 'not_applicable']
    matches_request: Literal['pass', 'fail', 'unsure']
    preferred: bool = False
    notes: str = Field('', max_length=2000)
    requirements: dict[Literal['must_keep', 'may_change', 'change_only'], Literal['pass', 'fail', 'unsure']] = Field(default_factory=dict)


@router.put('/jobs/{job_id}/artifacts/{name}/review')
def review_artifact(job_id: str, name: str, body: ArtifactReview):
    item = db.artifact(job_id, name)
    if item is None or not name.lower().endswith('.png'):
        raise HTTPException(status_code=404, detail='图片不存在')
    review = dict(body.model_dump(), source='human', updated_at=datetime.now(timezone.utc).isoformat())
    if not db.review_artifact(job_id, name, review):
        raise HTTPException(status_code=404, detail='图片不存在')
    return review


class LoraChoice(BaseModel):
    id: str = Field(..., description='LoRA id，见 GET /api/v1/loras')
    strength: float | None = Field(None, description='强度；省略用目录里的默认值')


DirectorRequest.model_rebuild()     # DirectorRequest 引用了在它之后定义的 LoraChoice


class ImageRequest(BaseModel):
    """Omitted generation parameters use the selected model's defaults."""
    prompt: str
    model: str = 'qwen-image-2.1'
    images: int = 1
    size: str | None = None
    negative: str | None = None
    steps: int | None = None
    cfg: float | None = None
    sampler: str | None = None
    scheduler: str | None = None
    seed: int | None = Field(None, description='省略为随机；实际值写在返回任务的 params.seed')
    loras: list[LoraChoice] | None = Field(None, description='叠加的 LoRA（仅 Pony / Anima），最多 6 个')
    rating: str | None = Field(None, description='尺度：general 日常 / suggestive 暗示 / explicit 露骨；省略用服务端默认')


class VideoRequest(BaseModel):
    """图生视频请求（base64 变体，走 /api/v1/videos/base64）。

    起始图必填；提示词只描述想要的运动，可以留空——内容由那张图承载。
    """
    image_base64: str = Field(..., description='起始图的 base64（可含 data: 前缀）')
    prompt: str = Field('', description='描述想要的运动与声音，例如 "clouds drift, slow push in, wind noise"')
    model: str = Field('ltx-2.5', description='ltx-2.5（画质）/ ltx-2.3（NSFW LoRA 生态更全）')
    duration: str = Field('normal', description='时长档位：short 2s / normal 5s / long 8s / extended 12s / max 20s')
    size: str = Field('landscape', description='尺寸预设键，见 /api/v1/params 的 video.sizes')
    seed: int | None = Field(None, description='省略为随机；实际值写在返回任务的 params.seed')
    loras: list[LoraChoice] | None = Field(None, description='叠加的 LoRA，按 model 分族（见 GET /api/v1/loras），最多 6 个')
    rating: str | None = Field(None, description='尺度：general 日常 / suggestive 暗示 / explicit 露骨；省略用服务端默认')


class EditRequest(BaseModel):
    """改图请求（base64 变体）。图片最多 8 MiB，只收 PNG/JPEG。"""
    image_base64: str = Field(..., description='输入图的 base64（可含 data: 前缀）')
    mode: str = Field('img2img', description='img2img 图生图 / edit 指令编辑')
    prompt: str = Field('', description='图生图可空；指令编辑必填')
    images: int = Field(1)
    negative: str = Field('')
    steps: int = Field(20)
    cfg: float = Field(1.0)
    sampler: str = Field('euler')
    scheduler: str = Field('simple')
    seed: int | None = Field(None, description='省略为随机；实际值写在返回任务的 params.seed')
    rating: str | None = Field(None, description='尺度：general 日常 / suggestive 暗示 / explicit 露骨；省略用服务端默认')
    denoise: float = Field(0.6, description='仅图生图：越大改动越多')
    region: list[StrictFloat] | None = Field(None, description='仅 edit：局部修复矩形 [左,上,右,下]，坐标归一化到 0–1；输出保留原尺寸及选区外像素')


def progress_view(events: list[dict] | None) -> dict | None:
    """把一个任务的进度事件折叠成 agent 好用的形状。

    ComfyUI 不按第 1、2、3 张的顺序采样（实测 2 → 3 → 1），所以整批进度要按
    「已采完几张」算，不能按「当前是第几张」算。每次新提交（queued/retry）重新计数。
    """
    latest, finished, other = None, set(), None
    for event in events or []:
        if event['kind'] in ('queued', 'retry'):
            latest, finished, other = None, set(), None
            continue
        try:
            info = json.loads(event['message'])
        except (TypeError, ValueError):
            continue
        if not isinstance(info, dict):
            continue
        if info.get('kind') == 'step':
            latest = info
            if isinstance(info.get('step'), int) and info.get('step') >= (info.get('max') or 0) > 0:
                finished.add(info.get('image'))
        else:
            other = info
    if latest is None:
        return other
    return {'image': latest.get('image'), 'total': latest.get('total'),
            'step': latest.get('step'), 'max': latest.get('max'),
            'images_done': len(finished),
            'percent': overall_percent(latest, finished)}


def overall_percent(info: dict, finished: set | None = None) -> int | None:
    """整批的进度：已采完的张数 + 当前这张的步数比例，按张数平均。

    按张数而不是按总步数加权：一句话出图里每张的步数可能不同。
    """
    step, steps = info.get('step'), info.get('max')
    images = info.get('total') or 1
    if not isinstance(step, int) or not isinstance(steps, int) or steps <= 0:
        return None
    others = len((finished or set()) - {info.get('image')})
    return round(100 * (min(others, images - 1) + min(step, steps) / steps) / images)


def _job_json(row: dict, include_progress: bool = True, extras: dict | None = None) -> dict:
    """`extras` 来自 db.job_extras；列表接口批量取一次传进来，单个任务时就地取。"""
    job_id = row['id']
    if extras is None:
        extras = db.job_extras([job_id], progress_ids=[job_id] if include_progress else None)
    artifacts = []
    for item in extras['artifacts'].get(job_id, []):
        artifacts.append({
            'name': item['name'], 'bytes': item['bytes'], 'sha256': item['sha256'],
            'seed': item['seed'], 'prompt': item['prompt'], 'params': item.get('params'),
            'review': item.get('review'),
            # 产物类型看扩展名就够了：图片 .png、视频 .webm，账本不多存一列
            'kind': 'video' if item['name'].lower().endswith('.webm') else 'image',
            'url': f'/api/v1/jobs/{job_id}/artifacts/{item["name"]}',
            'in_gallery': item['sha256'] in extras['gallery'],
        })
    body = {
        'id': job_id, 'app': row.get('app', 'image'),
        'mode': row.get('mode', 'txt2img'), 'state': row['state'],
        'prompt': row['prompt'], 'params': row['params'], 'error': row['last_error'],
        'created_at': row['created_at'], 'submitted_at': row.get('submitted_at'),
        'finished_at': row.get('finished_at'), 'attempts': row.get('attempts'),
        'artifacts': artifacts,
        'plan': row['params'].get('plan'),
        'stage': director_stage(row),
        'generation_request': row['request'],
        'input_url': (f'/api/v1/jobs/{job_id}/input') if row.get('input_path') else None,
        'links': {'self': f'/api/v1/jobs/{job_id}', 'page': f'/jobs/{job_id}',
                  'events': f'/api/v1/jobs/{job_id}/events',
                  'stream': f'/jobs/{job_id}/stream'},
    }
    if include_progress:
        body['progress'] = progress_view(extras['progress'].get(job_id))
    return jsonable_encoder(body)


def director_stage(row: dict) -> str | None:
    if row.get('mode') != 'director':
        return None
    if director.is_planning(row):
        return 'planning'
    return 'review' if row['state'] == 'review' else 'generating'


class DialogueLine(BaseModel):
    speaker: str = Field('', max_length=20)
    text: str = Field(..., max_length=60)


class PlanEdit(BaseModel):
    """review 阶段对一张图的修改；prompt 写原始提示词（不含自动标签与触发词）。"""
    prompt: str = Field(..., max_length=2000)
    negative: str = Field('', max_length=2000)
    size: str | None = Field(None, description='漫画预设下由格子决定，忽略')
    steps: int
    cfg: float
    seed: int
    rationale: str = Field('', max_length=300)
    # 以下仅漫画预设；省略则保持原格的值
    rating: Literal['general', 'suggestive', 'explicit'] | None = None
    beat: str | None = None
    shot: str | None = None
    action: str | None = Field(None, max_length=300)
    caption: str | None = Field(None, max_length=80)
    dialogue: list[DialogueLine] | None = Field(None, max_length=2)
    characters: list[int] | None = None


class ApproveRequest(BaseModel):
    variants: list[PlanEdit] | None = Field(None, description='省略 = 原样确认；给了就整体替换（可删、可改）')


@router.post('/jobs/{job_id}/approve')
def approve_plan(job_id: str, body: ApproveRequest | None = None) -> dict:
    """确认一句话出图的计划（仅 review 状态），可以带修改。之后照常排队生成。"""
    row = db.job(job_id)
    if row is None:
        raise HTTPException(status_code=404, detail='任务不存在')
    if row.get('mode') != 'director' or row['state'] != 'review':
        raise HTTPException(status_code=409, detail='只有等待确认的一句话出图任务可以确认')
    edits = (None if body is None or body.variants is None
             else [v.model_dump(exclude_none=True) for v in body.variants])
    try:
        request, params = director.approve(row, edits)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=[str(error)]) from None
    params['stage'] = 'generating'
    if not db.approve_plan(job_id, request, params):
        raise HTTPException(status_code=409, detail='计划已经确认过了')
    db.add_event(job_id, 'approved', f"计划已确认，开始生成 {params['images']} 张图片")
    db.notify(job_id)
    return _job_json(db.job(job_id))


def _submit(prompt: str, images: int, built: dict, mode: str = 'txt2img',
            input_sha256: str = '', input_path: str = '') -> tuple[str, bool]:
    """统一的提交口。撞到同参数任务时，把已存在任务的 id 一起带出去。

    enqueue 自己会算幂等键；这里先用同一份输入预先算出，用于 409 时反查。
    """
    import request as request_module
    key = request_module.storage_key_for(prompt=prompt, images=images,
                                        params=built, mode=mode,
                                        input_sha256=input_sha256)
    try:
        job_id = pipeline.enqueue(prompt=prompt.strip(), images=images, params=built,
                                  mode=mode, input_sha256=input_sha256,
                                  input_path=input_path)
        return job_id, True
    except UniqueViolation:
        existing = db.job_by_result_key(key)
        if existing is None:
            raise HTTPException(status_code=409,
                                detail='相同参数的任务已存在') from None
        return existing['id'], False


def _submit_video(prompt: str, built: dict, input_sha256: str,
                  input_path: str) -> tuple[str, bool]:
    """视频的提交口，与 _submit 同形：幂等键由 video_request 算。

    幂等键含全部影响输出的参数（含起始图哈希），所以同参数重提会被唯一约束挡下。
    """
    import video_request
    key = video_request.key_for(prompt, input_sha256, built)
    try:
        job_id = pipeline.enqueue(prompt=prompt.strip(), images=1, params=built,
                                  mode='i2v', input_sha256=input_sha256,
                                  input_path=input_path, app='video')
        return job_id, True
    except UniqueViolation:
        existing = db.job_by_result_key(key)
        if existing is None:
            raise HTTPException(status_code=409,
                                detail='相同参数的任务已存在') from None
        return existing['id'], False


@router.get('')
def capabilities() -> dict:
    """这个服务能做什么。agent 的第一步就查这个。"""
    return {
        'service': 'aladin',
        'description': '个人生图服务：文生图、图生图、指令编辑。产物存本地磁盘。',
        'modes': settings.MODES,
        'endpoints': {
            'artifact_reuse': {'method': 'GET', 'path': '/api/v1/jobs/{id}/artifacts/{name}/reuse',
                               'body': '单张图片的生成设置、目标页面与原始输入图 URL；只读取，不提交生成'},
            'people_benchmark': '/api/v1/benchmarks/people',
            'artifact_review': {'method': 'PUT', 'path': '/api/v1/jobs/{id}/artifacts/{name}/review',
                                'body': '人工评审：anatomy, matches_request, preferred, notes；可选 requirements 逐项评审创作要求'},
            'params': '/api/v1/params',
            'loras': {'method': 'GET', 'path': '/api/v1/loras',
                      'body': '文生图（Pony / Anima）与图生视频（LTX-2.3 / LTX-2.5）可带 loras: [{id, strength}]'},
            'director': {'method': 'POST', 'path': '/api/v1/director',
                         'body': 'application/json（brief；可选 count、rating、model、loras、review、preset、creative_spec）'},
            'director_approve': {'method': 'POST', 'path': '/api/v1/jobs/{id}/approve',
                                 'body': 'review 状态的一句话出图：可选 variants 整体替换计划'},
            'text_to_image': {'method': 'POST', 'path': '/api/v1/images',
                              'body': 'application/json'},
            'edit_image': {'method': 'POST', 'path': '/api/v1/edits',
                           'body': 'multipart/form-data（字段名 file）'},
            'edit_image_base64': {'method': 'POST', 'path': '/api/v1/edits/base64',
                                  'body': 'application/json（image_base64）'},
            'list_jobs': '/api/v1/jobs',
            'get_job': '/api/v1/jobs/{id}',
            'delete_job': {'method': 'DELETE', 'path': '/api/v1/jobs/{id}',
                           'body': '仅终态任务；删除本地产物，图库副本保留'},
            'job_events': '/api/v1/jobs/{id}/events',
            'job_stream': '/jobs/{id}/stream（SSE，浏览器用）',
            'gallery': {'method': 'GET', 'path': '/api/v1/gallery',
                        'query': 'q, tag, model, kind, min_stars, rating, sort（newest/oldest/stars）'},
            'gallery_facets': '/api/v1/gallery/facets',
            'gallery_update': {'method': 'PATCH', 'path': '/api/v1/gallery/{id}',
                               'body': 'application/json（tags 整体替换, stars 0–5）'},
            'billing': '/api/v1/billing',
            'image_to_video': {'method': 'POST', 'path': '/api/v1/videos',
                               'body': 'multipart/form-data（起始图字段名 file）'},
            'image_to_video_base64': {'method': 'POST', 'path': '/api/v1/videos/base64',
                                      'body': 'application/json（image_base64）'},
        },
        'job_states': ['pending', 'submitting', 'submitted', 'running',
                       'succeeded', 'failed', 'unknown'],
        'notes': [
            '提交是异步的：拿到 id 后轮询 GET /api/v1/jobs/{id}，或提交时加 wait=true。',
            '同参数重复提交返回 200 与已有任务，created=false。省略 seed 则每次随机，不会撞车。',
            '输入图片上限 %d MiB，只收 PNG/JPEG。' % (settings.MAX_UPLOAD_BYTES // (1 << 20)),
            '服务不鉴权，部署在 tailnet 内。',
            '图生视频有两套：ltx-2.5（默认，画质好）与 ltx-2.3（NSFW LoRA 多），各自一个 Modal app；'
            '产物是有声 webm，默认 5 秒 / 24fps；档位见 /api/v1/params 的 video，LoRA 见 /api/v1/loras。',
        ],
        'openapi': '/openapi.json',
        'docs': '/docs',
    }


@router.get('/billing')
def billing_status() -> dict:
    """Modal 账单快照：本期开销、credits 抵扣、余额估算（worker 每半小时刷新）。"""
    view = billing.view()
    if view is None:
        return {'available': False,
                'note': '还没有账单快照；worker 启动后会拉一次'}
    return {'available': True, **jsonable_encoder(view)}


@router.get('/benchmarks/people')
def people_benchmark():
    from pathlib import Path
    return json.loads(Path(__file__).with_name('people_cases.json').read_text())


@router.get('/loras')
def lora_catalog() -> dict:
    """可叠加的 LoRA：按底模分（pony / anima），含默认强度、区间、触发词、互斥组与内置预设。"""
    from .loras import public_catalog
    return public_catalog()


@router.get('/params')
def parameter_bounds() -> dict:
    """边界、预设与默认值。agent 用来自行构造合法请求，不必猜。"""
    from .image_models import public_models
    from .prompt_defaults import describe
    return jsonable_encoder({
        'prompt_defaults': dict(describe(), deduplicate='case_insensitive_whole_tag',
                                modes=list(settings.MODES)),
        'repair': {'mode': 'edit', 'region': '[left, top, right, bottom], normalized 0–1',
                   'method': 'context crop + instruction edit + composite', 'preserves_outside_region': True},
        'notes': ['CFG=1 时负向提示词不参与采样。', '批量候选与人工评审不能保证人体结构正确。'],
        'image_models': public_models(),
        'limits': settings.PARAM_LIMITS,
        'defaults': {'txt2img': settings.DEFAULT_PARAMS, 'edit': settings.EDIT_DEFAULT_PARAMS},
        'sizes': settings.SIZE_PRESETS,
        'samplers': settings.SAMPLERS,
        'schedulers': settings.SCHEDULERS,
        'edit_modes': settings.MODES,
        'max_upload_bytes': settings.MAX_UPLOAD_BYTES,
        'director': {'brief_chars': 4000, 'count': [1, 8], 'default_count': None,
                     'creative_spec': {'fields': ['must_keep', 'may_change', 'change_only'], 'field_chars': 1000},
                     'presets': {'manga': {'label': PRESETS['manga'], 'count': [MANGA_MIN, MANGA_MAX],
                                           'default_count': MANGA_DEFAULT,
                                           'layouts': {n: manga_layout(n) for n in MANGA_LAYOUTS},
                                           'beats': BEATS, 'shots': SHOTS}}},
        'video': {
            'models': video_models(),
            'durations': settings.VIDEO_DURATIONS,
            'sizes': settings.VIDEO_SIZES,
            'defaults': settings.VIDEO_DEFAULT_PARAMS,
            'limits': settings.VIDEO_LIMITS,
            'fps': settings.VIDEO_FPS,
        },
    })


def video_models() -> dict:
    """视频模型表（给 /api/v1/params 与视频页）：id -> 名称。"""
    from .video_worker import MODELS
    return {key: {'label': spec['label']} for key, spec in MODELS.items()}


@router.post('/videos', status_code=202)
async def create_video(file: UploadFile = File(...), prompt: str = Form(''),
                       model: str = Form('ltx-2.5'),
                       duration: str = Form('normal'), size: str = Form('landscape'),
                       seed: str = Form(''), loras: str = Form('', description='JSON 数组 [{id, strength}]'),
                       rating: str | None = Form(None),
                       wait: bool = Query(False),
                       timeout: int = Query(WAIT_DEFAULT_SECONDS)):
    """图生视频（LTX-2.3 / LTX-2.5）。起始图必须上传；提示词可留空，只描述运动与声音。"""
    seed_value, seed_error = rules.parse_seed(seed)
    if seed_error:
        raise HTTPException(status_code=422, detail=[seed_error])
    try:
        chosen = json.loads(loras) if loras.strip() else []
    except ValueError:
        raise HTTPException(status_code=422, detail=['loras 不是合法的 JSON']) from None
    built, errors = rules.video_params(prompt, duration, size, seed_value, model, chosen,
                                       rating)
    if errors:
        raise HTTPException(status_code=422, detail=errors)
    sha, relative, upload_error = await rules.save_upload(file)
    if upload_error:
        raise HTTPException(status_code=422, detail=[upload_error])
    job_id, created = _submit_video(prompt, built, sha, relative)
    return await _respond(job_id, created, wait, timeout)


@router.post('/videos/base64', status_code=202)
async def create_video_base64(body: VideoRequest, wait: bool = Query(False),
                              timeout: int = Query(WAIT_DEFAULT_SECONDS)):
    """图生视频的 JSON 变体：起始图用 base64 传，省掉 multipart。"""
    import base64
    import binascii

    payload = body.image_base64.strip()
    if payload.startswith('data:'):
        payload = payload.split(',', 1)[-1]
    try:
        data = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(status_code=422, detail=['图片不是合法的 base64']) from None
    if len(data) > settings.MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=422, detail=[
            '图片不能超过 %d MiB' % (settings.MAX_UPLOAD_BYTES // (1 << 20))])
    built, errors = rules.video_params(body.prompt, body.duration, body.size, body.seed,
                                       body.model,
                                       [c.model_dump(exclude_none=True) for c in body.loras or []],
                                       body.rating)
    if errors:
        raise HTTPException(status_code=422, detail=errors)
    stored = rules.store_bytes(data)
    if stored is None:
        raise HTTPException(status_code=422, detail=['只支持 PNG 或 JPEG'])
    sha, relative = stored
    job_id, created = _submit_video(body.prompt, built, sha, relative)
    return await _respond(job_id, created, wait, timeout)


@router.post('/images', status_code=202)
async def create_image(body: ImageRequest, wait: bool = Query(False),
                       timeout: int = Query(WAIT_DEFAULT_SECONDS)):
    """文生图。JSON 提交，立即返回任务；也可加 wait=true 等到终态。"""
    built, errors = rules.image_params(body.prompt, body.images, body.size, body.negative,
                                       body.steps, body.cfg, body.sampler,
                                       body.scheduler, body.seed, body.model,
                                       rating=body.rating,
                                       loras=[c.model_dump(exclude_none=True) for c in body.loras or []])
    if errors:
        raise HTTPException(status_code=422, detail=errors)
    job_id, created = _submit(prompt=body.prompt, images=body.images, built=built)
    return await _respond(job_id, created, wait, timeout)


@router.post('/edits/base64', status_code=202)
async def create_edit_base64(body: EditRequest, wait: bool = Query(False),
                             timeout: int = Query(WAIT_DEFAULT_SECONDS)):
    """改图的 JSON 变体：图片用 base64 传，省掉 multipart。

    适合「手里已经有图片字节、又不想拼 multipart」的 agent。两者走的是同一条路。
    """
    import base64
    import binascii

    payload = body.image_base64.strip()
    if payload.startswith('data:'):                 # 容忍 data:image/png;base64, 前缀
        payload = payload.split(',', 1)[-1]
    try:
        data = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(status_code=422, detail=['图片不是合法的 base64']) from None
    if len(data) > settings.MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=422, detail=[
            '图片不能超过 %d MiB' % (settings.MAX_UPLOAD_BYTES // (1 << 20))])
    # 先校验参数再落盘：参数不依赖图片字节，先落盘会让被拒的请求也留下文件
    built, errors = rules.edit_params(body.mode, body.prompt, body.images, body.negative,
                                      body.steps, body.cfg, body.sampler,
                                      body.scheduler, body.seed, body.denoise, body.region,
                                      body.rating)
    if not errors:
        error = rules.repair_input_error(data, built.get('region'))
        if error:
            errors.append(error)
    if errors:
        raise HTTPException(status_code=422, detail=errors)
    stored = rules.store_bytes(data)
    if stored is None:
        raise HTTPException(status_code=422, detail=['只支持 PNG 或 JPEG'])
    sha, relative = stored
    job_id, created = _submit(prompt=body.prompt, images=body.images, built=built,
                              mode=body.mode, input_sha256=sha, input_path=relative)
    return await _respond(job_id, created, wait, timeout)


@router.post('/edits', status_code=202)
async def create_edit(file: UploadFile = File(...), mode: str = Form('img2img'),
                      prompt: str = Form(''), images: int = Form(1),
                      negative: str = Form(''), steps: int = Form(20),
                      cfg: float = Form(1.0), sampler: str = Form('euler'),
                      scheduler: str = Form('simple'), seed: str = Form(''),
                      denoise: float = Form(0.6),
                      region: str = Form(''),
                      rating: str | None = Form(None),
                      wait: bool = Query(False),
                      timeout: int = Query(WAIT_DEFAULT_SECONDS)):
    """改图。image 必须上传；mode=img2img 时提示词可空，mode=edit 时必填。"""
    # 同上：先校验再落盘
    seed_value, seed_error = rules.parse_seed(seed)
    if seed_error:
        raise HTTPException(status_code=422, detail=[seed_error])
    built, errors = rules.edit_params(mode, prompt, images, negative, steps, cfg,
                                      sampler, scheduler, seed_value, denoise, region, rating)
    if errors:
        raise HTTPException(status_code=422, detail=errors)
    sha, relative, upload_error = await rules.save_upload(file, built.get('region'))
    if upload_error:
        raise HTTPException(status_code=422, detail=[upload_error])
    job_id, created = _submit(prompt=prompt, images=images, built=built, mode=mode,
                              input_sha256=sha, input_path=relative)
    return await _respond(job_id, created, wait, timeout)


async def _respond(job_id: str, created: bool, wait: bool, timeout: int):
    if wait:
        await _wait_for(job_id, min(max(timeout, 1), WAIT_MAX_SECONDS))
    row = db.job(job_id)
    if row is None:
        raise HTTPException(status_code=404, detail='任务不存在')
    body = _job_json(row)
    body['created'] = created          # false = 命中了已存在的同参数任务
    return JSONResponse(status_code=202 if created else 200, content=body)


async def _wait_for(job_id: str, timeout: int) -> None:
    """等到终态或超时。超时不算错：把当时的状态如实返回，agent 自己决定是否继续等。"""
    waited = 0.0
    while waited < timeout:
        row = db.job(job_id)
        if row is None or row['state'] in TERMINAL_STATES:
            return
        await asyncio.sleep(2)
        waited += 2


@router.get('/jobs')
def list_jobs(state: str | None = None, mode: str | None = None,
              limit: int = Query(20, ge=1, le=200)) -> list[dict]:
    rows = db.recent_jobs(limit=limit, state=state, mode=mode)
    extras = db.job_extras([row['id'] for row in rows])
    return [_job_json(row, include_progress=False, extras=extras) for row in rows]


@router.get('/jobs/{job_id}')
def get_job(job_id: str) -> dict:
    row = db.job(job_id)
    if row is None:
        raise HTTPException(status_code=404, detail='任务不存在')
    return _job_json(row)


@router.delete('/jobs/{job_id}')
def delete_job(job_id: str) -> dict:
    """删除终态任务与本地产物（图库副本保留）。进行中的任务返回 409。"""
    try:
        deleted = cleanup.delete_job(job_id)
    except cleanup.JobActive as error:
        raise HTTPException(status_code=409, detail=str(error)) from None
    if not deleted:
        raise HTTPException(status_code=404, detail='任务不存在')
    return {'deleted': job_id}


@router.get('/jobs/{job_id}/events')
def job_events(job_id: str, after: int = 0) -> list[dict]:
    if db.job(job_id) is None:
        raise HTTPException(status_code=404, detail='任务不存在')
    return jsonable_encoder([dict(e) for e in db.events(job_id, after=after)])


@router.get('/jobs/{job_id}/input')
def job_input(job_id: str):
    return _serve_input(job_id)


@router.get('/jobs/{job_id}/artifacts/{name}')
def job_artifact(job_id: str, name: str):
    row = db.artifact(job_id, name)
    if row is None:
        raise HTTPException(status_code=404, detail='产物不存在')
    path = db.data_path(row['rel_path'])
    if not path.is_file():
        raise HTTPException(status_code=404, detail='产物文件已不在本地')
    media = 'video/webm' if name.lower().endswith('.webm') else 'image/png'
    return FileResponse(path, media_type=media)


@router.get('/jobs/{job_id}/artifacts/{name}/reuse')
def artifact_reuse(job_id: str, name: str):
    from .reuse import artifact_settings
    try:
        return artifact_settings(job_id, name)
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from None
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from None


def _serve_input(job_id: str):
    row = db.job(job_id)
    if row is None or not row.get('input_path'):
        raise HTTPException(status_code=404, detail='这个任务没有输入图')
    path = settings.DATA / row['input_path']
    if not path.is_file():
        raise HTTPException(status_code=404, detail='输入图已不在本地')
    return FileResponse(path)


class GalleryUpdate(BaseModel):
    """只改给出的字段。tags 整体替换（传 [] 清空）；stars 0–5，0 表示未评分。"""
    tags: list[str] | None = Field(None, description='标签列表，整体替换；最多 20 个、每个 32 字符')
    stars: int | None = Field(None, ge=0, le=5, description='星级 0–5')


def gallery_json(item: dict) -> dict:
    return jsonable_encoder({
        'id': item['id'], 'prompt': item['prompt'], 'seed': item['seed'],
        'bytes': item['bytes'], 'sha256': item['sha256'],
        'kind': 'video' if item['rel_path'].lower().endswith('.webm') else 'image',
        'tags': item.get('tags') or [], 'stars': item.get('stars') or 0,
        'model': item.get('model'), 'mode': item.get('mode'), 'rating': item.get('rating'),
        'source_job': item.get('source_job'), 'created_at': item['at'],
        'url': f'/api/v1/gallery/{item["id"]}/file'})


@router.get('/gallery')
def list_gallery(q: str = '', tag: str = '', model: str = '',
                 kind: Literal['', 'image', 'video'] = '',
                 min_stars: int = Query(0, ge=0, le=5), rating: str = '',
                 sort: Literal['newest', 'oldest', 'stars'] = 'newest') -> list[dict]:
    """收藏检索：q 搜提示词（不分大小写），其余为精确筛选，可组合。"""
    return [gallery_json(item) for item in db.gallery_search(
        q=q.strip(), tag=tag, model=model, kind=kind, min_stars=min_stars,
        rating=rating, sort=sort)]


@router.get('/gallery/facets')
def gallery_facets() -> dict:
    """筛选项：已用过的标签、模型、尺度及数量。"""
    facets = db.gallery_facets()
    return {'tags': [{'tag': t, 'count': n} for t, n in facets['tags']],
            'models': [{'model': m, 'label': gallery.MODEL_LABELS.get(m, m), 'count': n}
                       for m, n in facets['models']],
            'ratings': [{'rating': r, 'count': n} for r, n in facets['ratings']]}


@router.patch('/gallery/{item_id}')
def update_gallery(item_id: int, body: GalleryUpdate) -> dict:
    tags = None
    if body.tags is not None:
        tags, error = gallery.normalize_tags(body.tags)
        if error:
            raise HTTPException(status_code=422, detail=[error])
    item = db.update_gallery_item(item_id, tags=tags, stars=body.stars)
    if item is None:
        raise HTTPException(status_code=404, detail='图库项不存在')
    return gallery_json(item)


@router.get('/gallery/{item_id}/file')
def gallery_file(item_id: int):
    item = db.gallery_item(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail='图库项不存在')
    path = db.data_path(item['rel_path'])
    if not path.is_file():
        raise HTTPException(status_code=404, detail='收藏文件已不在本地')
    media = 'video/webm' if item['rel_path'].lower().endswith('.webm') else 'image/png'
    return FileResponse(path, media_type=media)


@router.post('/gallery', status_code=201)
def add_to_gallery(job_id: str = Form(...), name: str = Form(...)) -> dict:
    """把某个任务的产物加进收藏（独立副本，删原任务不影响它）。图片和视频都收。"""
    row = db.artifact(job_id, name)
    if row is None:
        raise HTTPException(status_code=404, detail='产物不存在')
    # promote 返回 False 表示这个产物已经在收藏里（收藏按 sha256 去重）
    try:
        added = gallery.promote(job_id, name)
    except ValueError as error:              # 类型不在白名单里
        raise HTTPException(status_code=422, detail=str(error)) from None
    item = next((entry for entry in db.gallery_items()
                 if entry['sha256'] == row['sha256']), None)
    return {'added': added, 'id': item['id'] if item else None}


@router.delete('/gallery/{item_id}')
def remove_gallery(item_id: int) -> dict:
    if not gallery.remove(item_id):
        raise HTTPException(status_code=404, detail='图库项不存在')
    return {'removed': item_id}


def submit_director(brief: str, count: int | None = None, rating: str | None = None,
                    model: str | None = None, loras=None, review: bool = False,
                    preset: str | None = None, creative_spec=None) -> tuple[str, bool]:
    built, errors = rules.director_params(brief, count, rating, model, loras, review, preset, creative_spec)
    if errors:
        raise HTTPException(status_code=422, detail=errors)
    try:
        return pipeline.enqueue(brief.strip(), built['count'] or 1, built, mode='director'), True
    except UniqueViolation:
        row = db.job_by_result_key(director.build(brief, built['count'], built['rating'], built['model'],
                                                  built['loras'], built.get('preset'), built.get('creative_spec'))['key'])
        if row is None:
            raise HTTPException(status_code=409, detail='相同需求任务已存在') from None
        return row['id'], False


@router.post('/director', status_code=202)
async def create_director(body: DirectorRequest, wait: bool = Query(False),
                          timeout: int = Query(WAIT_DEFAULT_SECONDS)):
    job_id, created = submit_director(
        body.brief, body.count, body.rating, body.model,
        [c.model_dump(exclude_none=True) for c in body.loras or []], body.review, body.preset, body.creative_spec)
    return await _respond(job_id, created, wait, timeout)
