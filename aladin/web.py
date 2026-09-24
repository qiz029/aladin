"""本地 web：应用列表 → 生成 → 历史 → 图库。

单进程 FastAPI，服务端渲染 + 一小段原生 JS（进度用 SSE）。
所有写操作都经 pipeline，web 层不直接碰 Modal。
"""
from __future__ import annotations

import io
import json
import queue
import zipfile
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               RedirectResponse, StreamingResponse)
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from psycopg.errors import UniqueViolation

from . import api, cleanup, db, events, gallery, params as rules, pipeline, settings
from .web_origin import PrivateCacheMiddleware, PublicWebOriginMiddleware

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / 'templates'))
from .prompt_defaults import describe as prompt_policy
from .prompt_defaults import family_for as prompt_family
TEMPLATES.env.globals['prompt_policy'] = prompt_policy
TEMPLATES.env.globals['prompt_family'] = prompt_family
app = FastAPI(title='aladin')
app.add_middleware(PublicWebOriginMiddleware, public_url=settings.PUBLIC_WEB_URL)
app.add_middleware(PrivateCacheMiddleware)
app.mount('/static', StaticFiles(directory=Path(__file__).parent / 'static'), name='static')

APPS = [
    {'slug': 'director', 'name': '一句话出图',
     'description': '描述想法，自动规划构图与参数，一次交付全部图片与生成记录。'},
    {
        'slug': 'image',
        'name': '生成图片',
        'description': 'Qwen-Image 2.1（GGUF Q8_0）文生图，产物存本地。',
    },
    {
        'slug': 'edit',
        'name': '改图',
        'description': '给一张图：图生图做变体，或下指令让 Qwen-Image-Edit 照着改。',
    },
    {
        'slug': 'video',
        'name': '生成视频',
        'description': '10Eros-Max：给一张图，生成 24fps 有声视频。',
    },
]


def _job_views(rows: list[Any]) -> list[dict]:
    """一批任务的页面视图。产物、进度、图库状态一次查齐，见 db.job_extras。"""
    extras = db.job_extras([row['id'] for row in rows],
                           progress_ids=[row['id'] for row in rows
                                         if row['state'] not in db.FINISHED_STATES])
    return [_job_view(row, extras) for row in rows]


def _elapsed(row: Any) -> str:
    """已结束任务的总用时（创建 → 结束），含排队与冷启动。"""
    start, end = row.get('created_at'), row.get('finished_at')
    if not start or not end:
        return ''
    seconds = max(int((end - start).total_seconds()), 0)
    return f'{seconds // 60}:{seconds % 60:02d}'


def _job_view(row: Any, extras: dict | None = None) -> dict:
    from .image_models import MODELS, DEFAULT_MODEL
    if extras is None:
        extras = db.job_extras([row['id']], progress_ids=[row['id']])
    params = row['params']          # JSONB 取出来已经是 dict
    artifacts = []
    for item in extras['artifacts'].get(row['id'], []):
        view = dict(item)
        # 产物类型看扩展名：账本不多存一列，视频就是 .webm
        view['kind'] = 'video' if item['name'].lower().endswith('.webm') else 'image'
        artifacts.append(view)
    progress = api.progress_view(extras['progress'].get(row['id']))
    return {'id': row['id'], 'state': row['state'], 'prompt': row['prompt'],
            'model_label': MODELS.get(params.get('model', DEFAULT_MODEL), {}).get('label', ''),
            'params': params, 'error': row['last_error'],
            'kind': 'video' if (row.get('app') or 'image') == 'video' else 'image',
            'created_at': row['created_at'], 'artifacts': artifacts,
            'gallery': {a['sha256'] for a in artifacts if a['sha256'] in extras['gallery']},
            'mode': row.get('mode', 'txt2img'),
            'mode_label': settings.MODES.get(row.get('mode', 'txt2img'), '文生图'),
            # 列表页只关心百分比；非采样步的进度（排队、下载等）不显示
            'progress': ({'percent': progress['percent']}
                         if progress and progress.get('percent') else None),
            'when': row['created_at'].strftime('%m-%d %H:%M') if row.get('created_at') else '',
            'input_path': row.get('input_path'),
            'created_iso': row['created_at'].isoformat() if row.get('created_at') else '',
            'elapsed': _elapsed(row),
            'finished': row['state'] in db.FINISHED_STATES}


app.include_router(api.router)     # /api/v1：给 agent 用的 JSON 接口


@app.on_event('startup')
def _startup() -> None:
    db.init()
    settings.ensure_dirs()
    events.start_listener()   # NOTIFY -> SSE 的推送通道
    # api 不执行任务：提交与接管都在 worker 进程里（见 ADR-0002）


@app.get('/', response_class=HTMLResponse)
def index(request: Request):
    running = db.recent_jobs(limit=8, states=db.ACTIVE_STATES)
    return TEMPLATES.TemplateResponse(request, 'index.html', {
        'apps': APPS,
        'running': _job_views(running),
        'recent': [dict(item) for item in db.recent_artifacts(limit=12, app='image')],
        'videos': [dict(item) for item in db.recent_artifacts(limit=4, app='video')],
    })


@app.get('/apps/edit', response_class=HTMLResponse)
def edit_app(request: Request):
    return TEMPLATES.TemplateResponse(request, 'edit.html', {
        'defaults': settings.EDIT_DEFAULT_PARAMS,
        'samplers': settings.SAMPLERS,
        'schedulers': settings.SCHEDULERS,
        'limits': settings.PARAM_LIMITS,
        'max_mb': settings.MAX_UPLOAD_BYTES // (1 << 20),
        'recent': [dict(item) for item in db.recent_artifacts(limit=12, app='image')],
    })


@app.get('/jobs/{job_id}/compare', response_class=HTMLResponse)
def compare_images(request: Request, job_id: str, other: str = ''):
    rows = [db.job(job_id)]
    if other and other != job_id:
        rows.append(db.job(other))
    if any(row is None for row in rows):
        raise HTTPException(status_code=404, detail='任务不存在')
    jobs = [api._job_json(row, include_progress=False) for row in rows]
    return TEMPLATES.TemplateResponse(request, 'compare.html', {'jobs': jobs, 'job_id': job_id, 'other': other})


@app.get('/jobs/{job_id}/review.json')
def export_review(job_id: str):
    row = db.job(job_id)
    if row is None:
        raise HTTPException(status_code=404, detail='任务不存在')
    return JSONResponse(api._job_json(row, include_progress=False), headers={
        'Content-Disposition': f'attachment; filename="review-{job_id}.json"'})


@app.post('/apps/edit/jobs')
async def create_edit_job(file: UploadFile = File(...), mode: str = Form('img2img'),
                          prompt: str = Form(''), images: int = Form(1),
                          negative: str = Form(''), steps: int = Form(20),
                          cfg: float = Form(1.0), sampler: str = Form('euler'),
                          scheduler: str = Form('simple'), seed: str = Form(''),
                          denoise: float = Form(0.6), region: str = Form(''),
                          rating: str | None = Form(None)):
    if mode not in ('img2img', 'edit'):
        raise HTTPException(status_code=400, detail='未知的改图模式')
    seed_value, seed_error = rules.parse_seed(seed)
    if seed_error:
        raise HTTPException(status_code=400, detail=seed_error)
    params, errors = rules.edit_params(mode, prompt, images, negative, steps, cfg,
                                  sampler, scheduler, seed_value, denoise, region, rating)
    if errors:
        raise HTTPException(status_code=400, detail='；'.join(errors))
    sha, relative, upload_error = await rules.save_upload(file, params.get('region'))
    if upload_error:
        raise HTTPException(status_code=400, detail=upload_error)
    try:
        job_id = pipeline.enqueue(prompt=prompt.strip(), images=images, params=params,
                                  mode=mode, input_sha256=sha, input_path=relative)
    except UniqueViolation:
        raise HTTPException(
            status_code=409,
            detail='这张图配这套参数已经提交过；改动任一参数（种子或重绘幅度）后再试',
        ) from None
    except Exception as failure:
        raise HTTPException(status_code=500, detail=str(failure)[:300]) from None
    return RedirectResponse(url=f'/jobs/{job_id}', status_code=303)


@app.get('/jobs/{job_id}/input')
def job_input(job_id: str):
    """把输入图回显给任务页，这样「这个产物是从哪张图来的」一目了然。"""
    row = db.job(job_id)
    if row is None or not row.get('input_path'):
        raise HTTPException(status_code=404, detail='这个任务没有输入图')
    path = settings.DATA / row['input_path']
    if not path.is_file():
        raise HTTPException(status_code=404, detail='输入图已不在本地')
    return FileResponse(path)


@app.get('/apps/video', response_class=HTMLResponse)
def video_app(request: Request):
    return TEMPLATES.TemplateResponse(request, 'video.html', {
        'defaults': settings.VIDEO_DEFAULT_PARAMS,
        'durations': settings.VIDEO_DURATIONS,
        'sizes': settings.VIDEO_SIZES,
        'samplers': settings.VIDEO_SAMPLERS,
        'schedulers': settings.SCHEDULERS,
        'limits': settings.VIDEO_LIMITS,
        'fps': settings.VIDEO_FPS,
        'max_mb': settings.MAX_UPLOAD_BYTES // (1 << 20),
        'recent': [dict(item) for item in db.recent_artifacts(limit=8, app='video')],
    })


@app.post('/apps/video/jobs')
async def create_video_job(file: UploadFile = File(...), prompt: str = Form(''),
                           duration: str = Form('normal'), size: str = Form('landscape'),
                           negative: str = Form(''), steps: int = Form(6),
                           cfg: float = Form(1.0), shift: float = Form(12.0),
                           sampler: str = Form('res_multistep'), scheduler: str = Form('simple'),
                           seed: str = Form(''), lora_strength: float = Form(1.0),
                           rating: str | None = Form(None)):
    seed_value, seed_error = rules.parse_seed(seed)
    if seed_error:
        raise HTTPException(status_code=400, detail=seed_error)
    built, errors = rules.video_params(prompt, negative, duration, size, steps, cfg,
                                       shift, seed_value, sampler, scheduler, lora_strength,
                                       rating)
    if errors:
        raise HTTPException(status_code=400, detail='；'.join(errors))
    sha, relative, upload_error = await rules.save_upload(file)
    if upload_error:
        raise HTTPException(status_code=400, detail=upload_error)
    try:
        job_id = pipeline.enqueue(prompt=prompt.strip(), images=1, params=built,
                                  mode='i2v', input_sha256=sha, input_path=relative,
                                  app='video')
    except UniqueViolation:
        import video_request
        existing = db.job_by_result_key(video_request.key_for(prompt, sha, built))
        if existing is None:
            raise HTTPException(
                status_code=409,
                detail='这张图配这套参数已经提交过；改时长、尺寸或种子后再试') from None
        return RedirectResponse(url=f'/jobs/{existing["id"]}', status_code=303)
    except Exception as failure:
        raise HTTPException(status_code=500, detail=str(failure)[:300]) from None
    return RedirectResponse(url=f'/jobs/{job_id}', status_code=303)


def image_context(defaults=None, recent=None):
    from .image_models import MODELS, DEFAULT_MODEL, public_models
    model = (defaults or {}).get('model', DEFAULT_MODEL)
    spec = MODELS[model]
    from .loras import public_catalog
    return dict(lora_catalog=public_catalog(),
                defaults=dict(spec['defaults'], model=model, **{k:v for k,v in (defaults or {}).items() if k != 'model'}),
                sizes=spec['sizes'], samplers=spec['samplers'], schedulers=spec['schedulers'],
                limits=dict(settings.PARAM_LIMITS, steps=spec['steps']), image_models=public_models(),
                model_hint=spec['hint'], model_label=spec['label'], recent=recent or [])


@app.get('/apps/image', response_class=HTMLResponse)
def image_app(request: Request):
    return TEMPLATES.TemplateResponse(request, 'image.html', image_context(
        recent=[dict(item) for item in db.recent_artifacts(limit=12, app='image')]))


@app.post('/apps/image/jobs')
def create_job(prompt: str = Form(''), images: int = Form(1), size: str | None = Form(None),
               negative: str | None = Form(None), steps: int | None = Form(None),
               cfg: float | None = Form(None), sampler: str | None = Form(None),
               scheduler: str | None = Form(None), seed: str = Form(''),
               model: str = Form('qwen-image-2.1'), negative_provided: bool = Form(False),
               rating: str | None = Form(None), loras: str = Form('')):
    if negative_provided and negative is None:
        negative = ''
    seed_value, seed_error = rules.parse_seed(seed)
    if seed_error:
        raise HTTPException(status_code=400, detail=seed_error)
    try:
        # 表单把 LoRA 选择序列化成 JSON 放在隐藏字段里；与 API 的 loras 字段同形
        chosen = json.loads(loras) if loras.strip() else []
    except ValueError:
        raise HTTPException(status_code=400, detail='LoRA 选择格式不对') from None
    params, errors = rules.image_params(prompt, images, size, negative, steps,
                                       cfg, sampler, scheduler, seed_value, model,
                                       rating=rating, loras=chosen)
    if errors:
        raise HTTPException(status_code=400, detail='；'.join(errors))
    try:
        job_id = pipeline.enqueue(prompt=prompt.strip(), images=images, params=params)
    except UniqueViolation:
        # 结果目录按参数去重；同参数重跑没有意义，直接告诉用户怎么继续
        raise HTTPException(
            status_code=409,
            detail='相同参数的生成已经存在，改动任一参数（如种子）后再提交',
        ) from None
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error)[:300]) from None
    return RedirectResponse(url=f'/jobs/{job_id}', status_code=303)


@app.get('/jobs', response_class=HTMLResponse)
def jobs_page(request: Request, state: str | None = None):
    filters = {'running': '进行中', 'succeeded': '成功', 'failed': '失败'}
    # 「进行中」是一组状态：排队、提交中、运行中、等待重试都算，只挑 running 会漏掉排队的
    if state == 'running':
        rows = db.recent_jobs(limit=60, states=db.ACTIVE_STATES)
    else:
        rows = db.recent_jobs(limit=60, state=state)
    views = _job_views(rows)
    return TEMPLATES.TemplateResponse(request, 'jobs.html', {
        'jobs': views, 'state': state, 'filters': filters,
        'running': sum(1 for view in views if view['state'] not in ('succeeded', 'failed')),
    })


@app.post('/jobs/{job_id}/vary')
def vary_job(job_id: str):
    """按原任务再来一张：只把种子 +1，其余参数（含输入图）照搬。

    输入图存在本地，所以改图任务也能一键重跑，不需要重新上传。
    """
    row = db.job(job_id)
    if row is None:
        raise HTTPException(status_code=404, detail='任务不存在')
    if row.get('mode') == 'director':
        return RedirectResponse(url='/apps/director', status_code=303)
    params = dict(row['params'])
    params['seed'] = int(params.get('seed', 0)) + 1
    mode = row.get('mode') or 'txt2img'
    app = row.get('app') or 'image'
    try:
        new_id = pipeline.enqueue(prompt=row['prompt'], images=params.get('images', 1),
                                  params=params, mode=mode,
                                  input_sha256=row.get('input_sha256') or '',
                                  input_path=row.get('input_path') or '', app=app)
    except UniqueViolation:
        existing = db.job_by_result_key(_vary_key(row, params, mode))
        if existing is None:
            raise HTTPException(
                status_code=409,
                detail='种子的下一个值也已存在，去任务列表看看') from None
        # 这里必须 return：写成 raise X if ... else Y 会把 RedirectResponse 当异常抛
        return RedirectResponse(url=f'/jobs/{existing["id"]}', status_code=303)
    return RedirectResponse(url=f'/jobs/{new_id}', status_code=303)


def _vary_key(row: dict, params: dict, mode: str) -> str:
    if (row.get('app') or 'image') == 'video':
        import video_request
        return video_request.key_for(row['prompt'], row.get('input_sha256') or '', params)
    import request as request_module
    return request_module.storage_key_for(
        prompt=row['prompt'], images=params.get('images', 1), params=params,
        mode=mode, input_sha256=row.get('input_sha256') or '')


@app.post('/jobs/{job_id}/delete')
def delete_job(job_id: str):
    """删除任务与本地产物；加进图库的副本保留。"""
    try:
        deleted = cleanup.delete_job(job_id)
    except cleanup.JobActive as error:
        raise HTTPException(status_code=409, detail=str(error)) from None
    if not deleted:
        raise HTTPException(status_code=404, detail='任务不存在')
    return RedirectResponse(url='/jobs', status_code=303)


@app.get('/jobs/{job_id}', response_class=HTMLResponse)
def job_page(request: Request, job_id: str):
    row = db.job(job_id)
    if row is None:
        raise HTTPException(status_code=404, detail='任务不存在')
    return TEMPLATES.TemplateResponse(request, 'job.html',
                                      {'job': _job_view(row)})


@app.get('/jobs/{job_id}/state')
def job_state(job_id: str, after: int = 0):
    row = db.job(job_id)
    if row is None:
        raise HTTPException(status_code=404, detail='任务不存在')
    # jsonable_encoder：JSONB 里的时间戳是 datetime，直接丢给 JSONResponse 会报错
    return JSONResponse(jsonable_encoder({
        'state': row['state'], 'error': row['last_error'],
        'events': db.events(job_id, after=after),
        'artifacts': db.artifacts(job_id)}))


@app.get('/jobs/{job_id}/stream')
def job_stream(job_id: str, request: Request):
    """SSE：进度单向推送。

    NOTIFY 负责实时性，30s 兜底负责正确性——通知丢了也只会晚一点。
    `Last-Event-ID` 让浏览器断线重连时从上次的事件续上。
    """
    if db.job(job_id) is None:
        raise HTTPException(status_code=404, detail='任务不存在')
    cursor = int(request.headers.get('last-event-id') or 0)

    def generator():
        channel = events.subscribe(job_id)
        seen = cursor
        try:
            while True:
                for event in db.events(job_id, after=seen):
                    seen = event['id']
                    payload = {'kind': event['kind'], 'message': event['message'],
                               'at': event['at'].isoformat()}
                    yield (f'id: {seen}\ndata: '
                           f'{json.dumps(payload, ensure_ascii=False)}\n\n')
                row = db.job(job_id)
                if row is not None and row['state'] in ('succeeded', 'failed'):
                    yield ('data: ' + json.dumps(
                        {'kind': 'end', 'state': row['state'],
                         'error': row['last_error']}, ensure_ascii=False) + '\n\n')
                    return
                try:
                    channel.get(timeout=30)      # 被 NOTIFY 唤醒
                except queue.Empty:
                    pass                         # 兜底：到点自己再查一次
        finally:
            events.unsubscribe(job_id, channel)

    return StreamingResponse(generator(), media_type='text/event-stream',
                             headers={'Cache-Control': 'no-cache'})


def _media_type(name: str) -> str:
    """产物 MIME：视频是 webm，其余按 PNG 处理。"""
    return 'video/webm' if name.lower().endswith('.webm') else 'image/png'


@app.get('/jobs/{job_id}/artifacts/{name}')
def artifact_file(job_id: str, name: str):
    row = db.artifact(job_id, name)
    if row is None:
        raise HTTPException(status_code=404, detail='产物不存在')
    path = db.data_path(row['rel_path'])
    if not path.is_file():
        raise HTTPException(status_code=404, detail='文件已不在本地')
    return FileResponse(path, media_type=_media_type(name), filename=name)


@app.get('/jobs/{job_id}/download')
def download_job(job_id: str):
    # 账本里可能有记录而本地文件已被清理；只对真实存在的文件提供下载
    rows = [row for row in db.artifacts(job_id) if db.data_path(row['rel_path']).is_file()]
    if not rows:
        raise HTTPException(status_code=404, detail='没有可下载的产物')
    job_row = db.job(job_id)
    if len(rows) == 1 and (job_row or {}).get('mode') != 'director':
        path = db.data_path(rows[0]['rel_path'])
        return FileResponse(path, media_type=_media_type(rows[0]['name']),
                            filename=rows[0]['name'])
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        if job_row is not None:
            archive.writestr('generation.json', json.dumps(api._job_json(job_row), ensure_ascii=False, indent=2))
        for row in rows:
            archive.write(db.data_path(row['rel_path']), arcname=row['name'])
    buffer.seek(0)
    return StreamingResponse(buffer, media_type='application/zip',
                             headers={'Content-Disposition': f'attachment; filename="aladin-{job_id[:8]}.zip"'})


@app.post('/gallery')
def add_to_gallery(job_id: str = Form(...), name: str = Form(...)):
    """把产物收进收藏（图片和视频都可以）。"""
    try:
        added = gallery.promote(job_id, name)
    except LookupError:
        raise HTTPException(status_code=404, detail='产物不存在') from None
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail='产物文件不在本地') from None
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from None
    return JSONResponse({'added': added, 'already': not added})


@app.get('/gallery', response_class=HTMLResponse)
def gallery_page(request: Request, kind: str = '', q: str = '', tag: str = '', model: str = '',
                 min_stars: int = 0, rating: str = '', sort: str = 'newest'):
    """筛选与 /api/v1/gallery 同一个查询（db.gallery_search）；页面只是多了渲染。"""
    from .prompt_defaults import RATINGS
    kind = kind if kind in ('image', 'video') else ''
    min_stars = min(max(min_stars, 0), 5)
    items = []
    for item in db.gallery_search(q=q.strip(), tag=tag, model=model, kind=kind,
                                  min_stars=min_stars, rating=rating, sort=sort):
        row = dict(item)
        row['when'] = row['at'].strftime('%m-%d %H:%M')
        # 类型看副本的扩展名：账本不多存一列，页面靠它决定 <img> 还是 <video>
        row['kind'] = 'video' if row['rel_path'].lower().endswith('.webm') else 'image'
        row['model_label'] = gallery.MODEL_LABELS.get(row.get('model'), row.get('model') or '')
        items.append(row)
    filters = {'kind': kind, 'q': q.strip(), 'tag': tag, 'model': model,
               'min_stars': min_stars, 'rating': rating, 'sort': sort}
    return TEMPLATES.TemplateResponse(request, 'gallery.html', {
        'items': items, 'filters': filters, 'facets': db.gallery_facets(),
        'model_labels': gallery.MODEL_LABELS, 'ratings': RATINGS,
        'filtered': any(v for k, v in filters.items() if k != 'sort'),
        'total': len(db.gallery_items())})


@app.get('/gallery/{item_id}/file')
def gallery_file(item_id: int):
    row = db.gallery_item(item_id)
    if row is None:
        raise HTTPException(status_code=404, detail='图库条目不存在')
    path = db.data_path(row['rel_path'])
    if not path.is_file():
        raise HTTPException(status_code=404, detail='文件已不在本地')
    return FileResponse(path, media_type=_media_type(row['rel_path']))


@app.get('/gallery/{item_id}/download')
def gallery_download(item_id: int):
    row = db.gallery_item(item_id)
    if row is None:
        raise HTTPException(status_code=404, detail='图库条目不存在')
    path = db.data_path(row['rel_path'])
    if not path.is_file():
        raise HTTPException(status_code=404, detail='文件已不在本地')
    suffix = '.webm' if row['rel_path'].lower().endswith('.webm') else '.png'
    return FileResponse(path, media_type=_media_type(row['rel_path']),
                        filename=f'aladin-{row["seed"]}{suffix}')


@app.delete('/gallery/{item_id}')
def gallery_delete(item_id: int):
    if not gallery.remove(item_id):
        raise HTTPException(status_code=404, detail='图库条目不存在')
    return JSONResponse({'removed': True})


@app.get('/apps/director', response_class=HTMLResponse)
def director_app(request: Request):
    return TEMPLATES.TemplateResponse(request, 'director.html', {})


@app.post('/apps/director/jobs')
def create_director_job(brief: str = Form(''), rating: str | None = Form(None)):
    job_id, _ = api.submit_director(brief, rating=rating)
    return RedirectResponse(url=f'/jobs/{job_id}', status_code=303)


@app.get('/jobs/{job_id}/artifacts/{name}/reuse', response_class=HTMLResponse)
def reuse_artifact(request: Request, job_id: str, name: str):
    row = db.job(job_id)
    item = next((a for a in db.artifacts(job_id) if a['name'] == name), None)
    if row is None or item is None or name.lower().endswith('.webm'):
        raise HTTPException(status_code=404, detail='图片不存在')
    params = dict(item.get('params') or row['params'])
    from .image_models import MODELS, DEFAULT_MODEL
    params.setdefault('model', row['params'].get('model', DEFAULT_MODEL))
    spec = MODELS[params['model']]
    size = next((key for key, value in spec['sizes'].items()
                 if (value['width'], value['height']) == (params.get('width'), params.get('height'))), None)
    if size is None:
        raise HTTPException(status_code=422, detail='此图片尺寸不在文生图预设内，请使用原改图入口')
    defaults = dict(spec['defaults'], **{k: v for k, v in params.items() if k != 'size'})
    # 产物级参数里的 LoRA 只有文件与强度；选择要按任务级记录（带 id）回填
    defaults.update(size=size, prompt=item['prompt'], seed=item['seed'],
                    loras=[{'id': l['id'], 'strength': l['strength']}
                           for l in row['params'].get('loras') or []])
    return TEMPLATES.TemplateResponse(request, 'image.html', image_context(defaults))
