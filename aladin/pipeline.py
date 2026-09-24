"""任务编排。

规则（见 ADR-0002）：
- **api 只 enqueue**：插一行 `pending` 就返回，不碰 Modal。
- **只有 worker 提交**：认领 → spawn → 记 call_id。
- **有 call_id 就绝不重新 spawn**：只能 `FunctionCall.from_id` 接管。
- **重试前先查 Volume 回执**：容器可能早就跑完了，只是调用记录过期。
"""
from __future__ import annotations

import hashlib
import json
import os
import socket
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import modal

import request as request_module

from . import billing, db, director, metadata, settings
from .prompt_defaults import prepare

PROGRESS_PREFIX = '[aladin-progress]'
WORKER_ID = f'{socket.gethostname()}:{os.getpid()}'
LEASE_SECONDS = 90
# 任务通常 1 分钟内结束，前几次轮询会很快命中
POLL_BACKOFF = (15, 30, 60)
RECOVERY_INTERVAL = 30      # 秒；周期性清理中断的提交
PURGE_INTERVAL = 60         # 秒；删除任务后清理 Modal 结果 Volume 上的副本
PURGE_MAX_ATTEMPTS = 10
TRANSIENT_BACKOFF = 30      # 秒；本机到 Modal 的连接抖动后多久再查

_WATCHERS: dict[str, threading.Thread] = {}
_WATCH_LOCK = threading.Lock()


def _target(app: str) -> tuple[str, str, str, str]:
    """app -> (Modal app 名, 函数名, 结果 Volume 名, result.json 里的产物字段)。

    两个纵向切片的差异只在这四个值上；提交、轮询、重试、回执的逻辑完全共用。
    """
    if app == 'video':
        return (settings.APP_VIDEO, settings.FUNCTION_VIDEO,
                settings.VOLUME_RESULTS_VIDEO, 'videos')
    return settings.APP_IMAGE, settings.FUNCTION_IMAGE, settings.VOLUME_RESULTS, 'images'


def _is_transient(error: BaseException) -> bool:
    """本机 ↔ Modal 的传输层错误：说明「这次没查到」，不说明任务本身失败。

    只认 Modal 客户端与 gRPC 的类型。容器内抛出的异常会以原类型重抛
    （例如 ComfyUI 连不上时的内置 ConnectionError），那是确定性的，不能算在这里。
    """
    import grpclib.exceptions

    exceptions = modal.exception
    return isinstance(error, (exceptions.ConnectionError, exceptions.ClientClosed,
                              grpclib.exceptions.GRPCError,
                              grpclib.exceptions.StreamTerminatedError,
                              grpclib.exceptions.ProtocolError))


def _in(seconds: int) -> datetime:
    return datetime.now(timezone.utc) + timedelta(seconds=seconds)


def _short(error: BaseException) -> str:
    return f'{type(error).__name__}: {error}'.replace('\n', ' ')[:1500]


def parse_progress(message: str) -> dict | None:
    """从容器的一行 stdout 里提取结构化进度。

    worker 的协议固定为 `[aladin-progress] {json}`；其它日志行忽略。
    """
    line = message.strip()
    if not line.startswith(PROGRESS_PREFIX):
        return None
    payload = line[len(PROGRESS_PREFIX):].strip()
    try:
        value = json.loads(payload)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


# --- api 侧 -----------------------------------------------------------------

def enqueue(prompt: str, images: int, params: dict, mode: str = 'txt2img',
            input_sha256: str = '', input_path: str = '', app: str = 'image') -> str:
    """登记一个待执行的任务。**不提交**，提交由 worker 认领后做。

    input_sha256 只在有输入图时非空：它参与幂等键，所以不同的输入图不会被同一个键挡住。
    app 决定这条任务属于哪个纵向切片：请求体、Modal 函数与结果 Volume 都跟着它走，
    账本仍然只有一张表。
    """
    if mode == 'director':
        from .params import director_params
        built, errors = director_params(prompt, params.get('count'), params.get('rating'))
        if errors:
            raise ValueError('；'.join(errors))
        request = director.build(prompt, built['count'], built['rating'])
        return db.create_job(prompt.strip(), built, request, request['key'], mode='director')
    params = prepare(prompt, params)
    effective = params['prompt_defaults']['effective']
    if len(effective) > 2000:
        raise ValueError('加上默认标签后的提示词最多 2000 字符')
    if app == 'video':
        import video_request
        request = video_request.build(
            effective, input_sha256, negative=params.get('negative', ''),
            width=params['width'], height=params['height'], frames=params['frames'],
            steps=params['steps'], cfg=params['cfg'], shift=params['shift'],
            seed=params['seed'], sampler=params['sampler'],
            scheduler=params['scheduler'],
            loraStrength=float(params.get('loraStrength', 1.0)))
        key = request['key']
    elif mode == 'txt2img' and params.get('model', 'qwen-image-2.1') != 'qwen-image-2.1':
        from .extra_image_request import build as build_extra
        request = build_extra(effective, images, params)
        key = request['key']
    else:
        denoise = params.get('denoise', 1.0) if mode == 'img2img' else 1.0
        key = request_module.storage_key_for(prompt=prompt, images=images, params=params,
                                             mode=mode, input_sha256=input_sha256)
        request = request_module.build(
            key=key, prompt=effective, images=images, seed=params['seed'],
            width=params['width'], height=params['height'], steps=params['steps'],
            cfg=params['cfg'], sampler=params['sampler'],
            scheduler=params['scheduler'], negative=params.get('negative', ''),
            mode=mode, input_sha256=input_sha256, denoise=denoise,
            region=params.get('region'))
    return db.create_job(prompt=prompt, params=dict(params, images=images),
                         request=request, result_key=key, mode=mode,
                         input_sha256=input_sha256 or None,
                         input_path=input_path or None, app=app)


# --- worker 侧 --------------------------------------------------------------

def run_forever(poll_interval: float = 2.0, stop: threading.Event | None = None) -> None:
    """worker 主循环。三个阶段各自认领一行，互不阻塞。"""
    print(f'worker {WORKER_ID} 启动', flush=True)
    last_recovery = 0.0
    last_billing = 0.0
    last_purge = 0.0
    while not (stop is not None and stop.is_set()):
        # 周期性恢复：只在启动时跑一次的话，其它 worker 崩溃留下的 submitting
        # 会永远卡住（本进程不会再启动）。
        if time.monotonic() - last_recovery > RECOVERY_INTERVAL:
            _recover_interrupted()
            last_recovery = time.monotonic()
        # 账单快照也周期性刷新；失败只打日志，不影响任务循环
        if time.monotonic() - last_billing > settings.BILLING_REFRESH_SECONDS:
            _refresh_billing()
            last_billing = time.monotonic()
        if time.monotonic() - last_purge > PURGE_INTERVAL:
            _purge_remote()
            last_purge = time.monotonic()
        try:
            worked = _submit_one() | _poll_one() | _resolve_unknown_one()
        except Exception as error:                  # 单轮出错不能拖垮整个 worker
            print('循环异常: ' + _short(error), flush=True)
            worked = False
        if not worked:
            time.sleep(poll_interval)


def _refresh_billing() -> None:
    """拉一次 Modal 账单写快照表。失败不影响任务循环。"""
    try:
        payload = billing.refresh()
    except Exception as error:      # 网络/凭据问题都不该拖垮 worker
        print('账单刷新失败: ' + _short(error), flush=True)
        return
    print('账单快照: 本期计量 $%.2f，credits 已抵扣 $%.2f'
          % (payload['metered_cost'], payload['credits_applied']), flush=True)


def _purge_remote(limit: int = 20) -> int:
    """删掉已删除任务在 Modal 结果 Volume 上的目录。失败留待下一轮，封顶重试次数。"""
    try:
        with db.connect() as connection:
            rows = connection.execute(
                'SELECT * FROM remote_purge WHERE attempts < %s ORDER BY id LIMIT %s',
                (PURGE_MAX_ATTEMPTS, limit)).fetchall()
    except Exception as error:
        print('远端清理清单读取失败: ' + _short(error), flush=True)
        return 0
    done = 0
    for row in rows:
        try:
            try:
                modal.Volume.from_name(row['volume']).remove_file(row['path'], recursive=True)
            except modal.exception.NotFoundError:
                pass                             # 本来就不在（例如从没跑到出图）：视为完成
            with db.connect() as connection:
                connection.execute('DELETE FROM remote_purge WHERE id = %s', (row['id'],))
            done += 1
        except Exception as error:
            with db.connect() as connection:
                connection.execute(
                    'UPDATE remote_purge SET attempts = attempts + 1, last_error = %s WHERE id = %s',
                    (_short(error), row['id']))
    if done:
        print(f'已清理 Modal 上 {done} 个已删除任务的结果目录', flush=True)
    return done


def _recover_interrupted() -> int:
    """把提交阶段中断的任务（spawn 可能已发生但 call_id 没写库）转成 unknown。

    这是设计里唯一无法消除的不确定窗口，靠后续的回执检查兜底。
    """
    with db.connect() as connection:
        cursor = connection.execute(
            "UPDATE jobs SET state = 'unknown',"
            " last_error = COALESCE(last_error, '提交阶段中断：spawn 可能已发生')"
            " WHERE state = 'submitting' AND lease_expires_at < now()")
        count = cursor.rowcount
    if count:
        print(f'接管 {count} 个中断的提交', flush=True)
    return count


def _submit_one() -> bool:
    row = db.claim("state = 'pending'", (), WORKER_ID, new_state='submitting',
                   lease_seconds=LEASE_SECONDS)
    if row is None:
        return False
    job_id = row['id']
    # 尝试次数在**认领时**就记，而不是 spawn 成功后记：否则 spawn 一直失败时
    # attempts 永远是 0，重试就没有上限了。
    attempt = row['attempts'] + 1
    db.update_job(job_id, attempts=attempt)
    db.add_event(job_id, 'start', f'第 {attempt}/{row["max_attempts"]} 次提交')
    db.notify(job_id)
    # 输入图的字节不进 DB：提交时才从磁盘读出来，作为第二个参数发给容器。
    # 放在 spawn 之前读：文件不在就立刻失败，不必等容器起来。
    source = b''
    if row.get('input_path'):
        path = settings.DATA / row['input_path']
        if not path.is_file():
            _fail(job_id, '输入图已不在本地: ' + row['input_path'])
            return True
        source = path.read_bytes()
    try:
        target_app, target_function, _volume, _field = _target(row.get('app') or 'image')
        if row['request'].get('modelId'):
            from .image_models import MODELS
            target_app = MODELS[row['request']['modelId']]['app']
        if director.is_planning(row):
            function = modal.Function.from_name(director.APP, 'plan')
            call = function.spawn(row['request'])
        else:
            function = modal.Function.from_name(target_app, target_function)
            call = function.spawn(row['request'], source)
    except Exception as error:
        # spawn 失败通常是暂时性的（网络 / Modal 抖动），走 unknown 而不是直接判死
        _to_unknown(job_id, '提交失败: ' + _short(error))
        return True
    db.update_job(job_id, state='submitted', call_id=call.object_id,
                  submitted_at=datetime.now(timezone.utc),
                  next_poll_at=_in(POLL_BACKOFF[0]))
    db.add_event(job_id, 'queued', '已提交到 Modal（call_id ' + call.object_id + '）')
    db.notify(job_id)
    _watch(job_id, call)
    return True


def _poll_one() -> bool:
    row = db.claim(
        "state IN ('submitted','running')"
        " AND (next_poll_at IS NULL OR next_poll_at <= now())",
        (), WORKER_ID)
    if row is None:
        return False
    _poll_row(row)
    return True


def _poll_row(row: dict) -> str:
    job_id = row['id']
    state = row['state']
    # 不变量 1：有 call_id 就接管，绝不重新 spawn
    call = modal.FunctionCall.from_id(row['call_id'])
    _watch(job_id, call)
    try:
        result = call.get(timeout=0)
    except TimeoutError:
        backoff = POLL_BACKOFF[min(max(row['attempts'] - 1, 0), len(POLL_BACKOFF) - 1)]
        fields: dict[str, Any] = {'next_poll_at': _in(backoff),
                                  'lease_expires_at': _in(LEASE_SECONDS)}
        if state == 'submitted':
            fields['state'] = 'running'
            db.add_event(job_id, 'running', 'Modal 已受理，等待容器执行')
            db.notify(job_id)
        db.update_job(job_id, **fields)
        return 'running'
    except modal.exception.OutputExpiredError:
        return _to_unknown(job_id, '调用记录已过期')
    except modal.exception.DeserializationError:
        # 容器已经跑完，只是返回值在本地解不开（实测出现过）；Volume 清单/回执才是权威
        if director.is_planning(row):
            return _to_unknown(job_id, '规划返回值无法反序列化，改查回执')
        result = None
    except Exception as error:
        if _is_transient(error):
            # 查询失败 ≠ 任务失败：容器可能还在跑、还在计费。保持状态，稍后再查；
            # 不转 unknown，否则会在容器仍在运行时重新 spawn。
            db.update_job(job_id, next_poll_at=_in(TRANSIENT_BACKOFF),
                          lease_expires_at=_in(LEASE_SECONDS), last_error=_short(error))
            db.add_event(job_id, 'warn', '查询 Modal 失败，稍后重试: ' + _short(error))
            db.notify(job_id)
            return state
        # 容器内抛出的异常是确定性的：重试只会撞同一个错误
        _fail(job_id, _short(error))
        return 'failed'

    if director.is_planning(row):
        try:
            return _accept_plan(row, result)
        except Exception as error:
            _fail(job_id, _short(error))
            return 'failed'

    try:
        _download(job_id, row['result_key'],
                  result if isinstance(result, dict) else {}, row.get('app') or 'image')
    except Exception as error:
        _fail(job_id, _short(error))
        return 'failed'
    db.finish_job(job_id, 'succeeded')
    db.add_event(job_id, 'succeeded', '完成')
    db.notify(job_id)
    return 'succeeded'


def _resolve_unknown_one() -> bool:
    row = db.claim("state = 'unknown'", (), WORKER_ID)
    if row is None:
        return False
    job_id = row['id']
    # 不变量 2：先查回执。容器可能早就跑完了，只是调用记录过期。
    app = row.get('app') or 'image'
    if director.is_planning(row):
        try:
            volume = modal.Volume.from_name(director.RESULTS_VOLUME)
            receipt = json.loads(_read_bytes(volume, row['result_key'] + '/plan.json'))
            if receipt.get('request') == row['request']:
                _accept_plan(row, receipt['plan'])
                return True
        except Exception as error:
            db.add_event(job_id, 'warn', '规划回执暂不可用: ' + _short(error))
    elif _receipt_exists(row['result_key'], app):
        try:
            _download(job_id, row['result_key'], {}, app)
            db.finish_job(job_id, 'succeeded')
            db.add_event(job_id, 'succeeded', '从 Volume 回执恢复')
            db.notify(job_id)
            return True
        except Exception as error:
            db.add_event(job_id, 'warn', '回执存在但拉取失败: ' + _short(error))
    if row['attempts'] < row['max_attempts']:
        db.update_job(job_id, state='pending', call_id=None, next_poll_at=None,
                      lease_owner=None, lease_expires_at=None)
        db.add_event(job_id, 'retry',
                     f"第 {row['attempts'] + 1}/{row['max_attempts']} 次尝试")
        db.notify(job_id)
        return True
    _fail(job_id, '重试次数已用尽；' + (row['last_error'] or ''))
    return True


def _watch(job_id: str, call: Any) -> None:
    """为任务起一个日志流线程；已经在跑就不重复起。"""
    watcher_key = job_id + ':' + str(getattr(call, 'object_id', ''))
    with _WATCH_LOCK:
        existing = _WATCHERS.get(watcher_key)
        if existing is not None and existing.is_alive():
            return
        # 顺手清掉已结束的线程，长跑的 worker 不会无限攒条目
        for key in [key for key, thread in _WATCHERS.items() if not thread.is_alive()]:
            del _WATCHERS[key]
        thread = threading.Thread(target=_watch_loop, args=(job_id, call),
                                  name='watch-' + job_id[:8], daemon=True)
        _WATCHERS[watcher_key] = thread
        thread.start()


def _watch_loop(job_id: str, call: Any) -> None:
    """把容器进度写成事件。

    流断了不要紧——轮询是正确性的兜底，这里只是让进度更实时。
    """
    try:
        for entry in call.logs.stream(timeout=300):
            info = parse_progress(entry.message)
            if info is None:
                continue
            # 重开流可能重放旧行；用 dedupe_key 让重放无害。键里带 call_id：
            # 重试是新的一次调用，否则它的第 1..N 步会被上一次的同名键全部吞掉
            dedupe = None
            if info.get('kind') == 'artifact':
                # 先落地再发事件：页面收到事件就去刷新产物区，这时账本里必须已经有这一张
                _fetch_early(job_id, info)
                dedupe = f"artifact:{getattr(call, 'object_id', '')}:{info.get('file')}"
            elif info.get('kind') == 'queued':
                dedupe = f"started:{getattr(call, 'object_id', '')}"
            elif info.get('kind') == 'step':
                dedupe = (f"step:{getattr(call, 'object_id', '')}:{info.get('node')}:"
                          f"{info.get('image')}:{info.get('step')}")
            if db.add_event(job_id, 'progress',
                            json.dumps(info, ensure_ascii=False), dedupe_key=dedupe):
                db.notify(job_id)
    except Exception as error:
        # 流断了只影响实时性（下次轮询会重开），但要留下痕迹，否则进度卡住时无从查起
        print(f'进度流中断 {job_id[:8]}: {_short(error)}', flush=True)


def _to_unknown(job_id: str, reason: str) -> str:
    db.update_job(job_id, state='unknown', last_error=reason, next_poll_at=None)
    db.add_event(job_id, 'unknown', reason)
    db.notify(job_id)
    return 'unknown'


def _fail(job_id: str, reason: str) -> None:
    db.finish_job(job_id, 'failed', reason)
    db.add_event(job_id, 'failed', reason)
    db.notify(job_id)


# --- 产物 -------------------------------------------------------------------

def _receipt_exists(result_key: str, app: str = 'image') -> bool:
    _app_name, _function, volume_name, _field = _target(app)
    try:
        _read_bytes(modal.Volume.from_name(volume_name), f'{result_key}/result.json')
        return True
    except Exception:
        return False


def _download(job_id: str, result_key: str, record: dict, app: str = 'image') -> None:
    """把 Volume 里的产物拉到本地。**Volume 的 result.json 是权威清单**。

    容器返回值曾多次无法在本地反序列化（实测），所以先信 Volume，再退回返回值。
    图片清单在 `images`、视频在 `videos`，字段名由 _target() 给出。
    """
    _app_name, _function, volume_name, field = _target(app)
    volume = modal.Volume.from_name(volume_name)
    images = _read_manifest(volume, result_key, field) or record.get(field) or []
    directory = settings.JOBS_DIR / job_id
    directory.mkdir(parents=True, exist_ok=True)
    found = 0
    for item in images:
        if not isinstance(item, dict) or 'file' not in item:
            continue
        name = item['file']
        # 清单来自容器输出；只接受普通文件名，避免 ../ 之类写到 DATA 之外
        if not isinstance(name, str) or not name or os.path.basename(name) != name:
            raise RuntimeError(f'产物文件名不合法: {name!r}')
        # 容器已关掉元数据；这里再剥一次，兜住 Volume 上的旧回执。账本记落盘文件的真实哈希
        data = metadata.strip(_read_bytes(volume, f'{result_key}/{name}'), name)
        (directory / name).write_bytes(data)
        rel = str((directory / name).relative_to(settings.DATA))
        db.add_artifact(job_id, name, rel, hashlib.sha256(data).hexdigest(), len(data),
                        item.get('seed', 0), item.get('prompt', ''),
                        # 产物级参数：批量里每张图可能各不相同，逐张记下来才能精确复现
                        item.get('params'))
        found += 1
    if found == 0:
        raise RuntimeError('任务已完成但没找到任何产物')


def _fetch_early(job_id: str, info: dict) -> bool:
    """逐张回传：容器提交了某一张后，先把这一张拉回来，页面不必等整批结束。

    只是提前量。失败不影响任务：整批结束后的 _download 以 result.json 为准重新拉全部。
    """
    try:
        row = db.job(job_id)
        if row is None or row['state'] in db.FINISHED_STATES:
            return False
        name, sha = info.get('file'), info.get('sha256')
        if not isinstance(name, str) or not name or os.path.basename(name) != name:
            raise RuntimeError(f'产物文件名不合法: {name!r}')
        target = settings.JOBS_DIR / job_id / name
        if target.is_file():
            return False                    # 日志流重放：已经拉过了（整批结束时会按清单再核一遍）
        _app, _function, volume_name, _field = _target(row.get('app') or 'image')
        data = _read_bytes(modal.Volume.from_name(volume_name), f"{row['result_key']}/{name}")
        if hashlib.sha256(data).hexdigest() != sha:
            raise RuntimeError('Volume 上的文件与通知的 sha256 不符（可能还没提交完）')
        data = metadata.strip(data, name)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(name + '.part')
        temporary.write_bytes(data)
        temporary.replace(target)
        db.add_artifact(job_id, name, str(target.relative_to(settings.DATA)),
                        hashlib.sha256(data).hexdigest(), len(data),
                        info.get('seed', 0), info.get('prompt', ''), info.get('params'))
        return True
    except Exception as error:
        print(f'逐张拉取失败 {job_id[:8]}: {_short(error)}', flush=True)
        return False


def _read_manifest(volume: Any, result_key: str, field: str = 'images') -> list | None:
    try:
        payload = _read_bytes(volume, f'{result_key}/result.json')
    except Exception:
        return None
    try:
        return json.loads(payload).get(field)
    except ValueError:
        return None


def _read_bytes(volume: Any, path: str) -> bytes:
    buffer = bytearray()
    for chunk in volume.read_file(path):
        buffer.extend(chunk)
    return bytes(buffer)


def _accept_plan(row: dict, plan: dict) -> str:
    request, params = director.image_batch(row, plan)
    if db.accept_plan(row, request, params):
        db.add_event(row['id'], 'planned', f"规划完成，开始生成 {params['images']} 张图片")
        db.notify(row['id'])
    return 'pending'
