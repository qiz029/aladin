"""生成耗时报表：把 jobs.timings（容器内分段）和 job_events（宿主侧时刻）拼成逐任务的分段。

    python -m aladin timings [--since 2026-09-25] [--app image|video] [--limit 50]
    python -m aladin timings --backfill     给老任务补上 result.json 里的 elapsedSeconds

分段（秒）：
    wait     创建 → 提交到 Modal（worker 认领）；一句话出图另有 plan（规划）
    modal    提交 → 容器开始执行：Modal 调度、冷启动、排队（max_containers=1 时串行）
    weights / boot / exec / out   容器内：权重检查、ComfyUI 启动、执行、写产物
    load / encode / sample / decode   exec 里按 ComfyUI 节点归类的耗时。ComfyUI 惰性加载权重：
             加载器节点只登记，真正读权重进显存算在第一个用到它的节点上——
             文本编码器的加载落在 encode，扩散模型的加载落在第一次 sample
    tail     容器执行完 → 宿主发现完成（含 Volume 提交与轮询间隔）
    dl       从 Volume 下载产物到本地
宿主时刻与容器时刻来自两台机器的时钟，modal / tail 含时钟偏差（通常 < 1 秒）。
"""
from __future__ import annotations

import re
import statistics
from datetime import datetime

from . import db

COLUMNS = ['id', 'created', 'model', 'mode', 'n', 'reuse', 'e2e', 'wait', 'plan', 'modal',
           'weights', 'boot', 'exec', 'load', 'encode', 'sample', 'decode', 'out', 'tail', 'dl']
SUMMARY = ['e2e', 'modal', 'boot', 'exec', 'encode', 'sample', 'tail']


def node_group(class_type: str | None) -> str:
    name = (class_type or '').lower()
    if 'loader' in name:
        return 'load'
    if 'sampl' in name:
        return 'sample'
    if 'decode' in name:
        return 'decode'
    if 'encode' in name:
        return 'encode'
    return 'other'


def _epoch(value: datetime | None) -> float | None:
    return value.timestamp() if value is not None else None


def _diff(end: float | None, start: float | None) -> float | None:
    return None if end is None or start is None else round(end - start, 1)


def model_of(job: dict) -> str:
    params, request = job.get('params') or {}, job.get('request') or {}
    name = params.get('model') or request.get('modelId') or request.get('model') or ''
    return str(name).split('/')[-1].replace('-Uncensored-GGUF', '')


def breakdown(job: dict, events: list[dict]) -> dict:
    """一个任务的分段。`events` 是该任务的全部事件（按 id 升序）。"""
    timings = job.get('timings') or {}
    container = timings.get('container') or {}
    phases = container.get('phases') or {}
    at = {}
    for event in events:
        at.setdefault(event['kind'], []).append(_epoch(event['at']))
    queued = at.get('queued') or []
    planned = (at.get('planned') or [None])[0]
    # 一句话出图先规划再生成：生成阶段的提交是规划完成之后的那一次
    spawned = next((t for t in reversed(queued) if planned is None or t >= planned), None)
    created, finished = _epoch(job['created_at']), _epoch(job.get('finished_at'))
    started = container.get('startedAt') or (at.get('container') or [None])[-1]
    groups: dict[str, float] = {}
    for node in container.get('nodes') or []:
        group = node_group(node.get('class'))
        groups[group] = groups.get(group, 0) + (node.get('seconds') or 0)
    index = container.get('callIndex')
    return {
        'id': job['id'][:8], 'created': job['created_at'].strftime('%m-%d %H:%M'),
        'app': job['app'], 'model': model_of(job), 'mode': job['mode'],
        'n': job['image_count'],
        'reuse': '' if index is None else ('cold' if index == 0 else f'warm{index}'),
        'e2e': _diff(finished, created),
        'wait': _diff(queued[0] if queued else None, created),
        'plan': _diff(planned, queued[0] if queued else None) if planned else None,
        'modal': _diff(started, spawned),
        'weights': phases.get('weights'), 'boot': phases.get('comfyBoot'),
        'exec': phases.get('execute', timings.get('comfyExecuteSeconds')),
        'load': round(groups['load'], 1) if 'load' in groups else None,
        'encode': round(groups['encode'], 1) if 'encode' in groups else None,
        'sample': round(groups['sample'], 1) if 'sample' in groups else None,
        'decode': round(groups['decode'], 1) if 'decode' in groups else None,
        'out': phases.get('outputs'),
        'tail': _diff(timings.get('detectedAt'), container.get('finishedAt')),
        'dl': timings.get('downloadSeconds'),
    }


def rows(since: str | None = None, app: str | None = None, limit: int = 50) -> list[dict]:
    where, args = ["state = 'succeeded'"], []
    if since:
        where.append('created_at >= %s')
        args.append(since)
    if app:
        where.append('app = %s')
        args.append(app)
    with db.connect() as connection:
        jobs = connection.execute(
            'SELECT * FROM (SELECT * FROM jobs WHERE ' + ' AND '.join(where) +
            ' ORDER BY created_at DESC LIMIT %s) recent ORDER BY created_at',
            (*args, limit)).fetchall()
        events = connection.execute(
            'SELECT job_id, kind, at FROM job_events WHERE job_id = ANY(%s) ORDER BY id',
            ([job['id'] for job in jobs],)).fetchall()
    by_job: dict[str, list] = {}
    for event in events:
        by_job.setdefault(event['job_id'], []).append(event)
    return [breakdown(job, by_job.get(job['id'], [])) for job in jobs]


def _cell(value) -> str:
    if value is None:
        return '-'
    return f'{value:.1f}' if isinstance(value, float) else str(value)


def table(items: list[dict], columns: list[str]) -> str:
    cells = [[_cell(item.get(column)) for column in columns] for item in items]
    widths = [max([len(column)] + [len(row[i]) for row in cells]) for i, column in enumerate(columns)]
    lines = ['  '.join(column.ljust(widths[i]) for i, column in enumerate(columns))]
    lines += ['  '.join(value.ljust(widths[i]) for i, value in enumerate(row)) for row in cells]
    return '\n'.join(lines)


def summary(items: list[dict]) -> list[dict]:
    """按 模型 × 冷热 分组，给每个分段的中位数与 p90。"""
    groups: dict[tuple, list[dict]] = {}
    for item in items:
        reuse = item['reuse'][:4] if item['reuse'] else '?'
        groups.setdefault((item['model'], reuse), []).append(item)
    result = []
    for (model, reuse), members in sorted(groups.items()):
        entry = {'model': model, 'reuse': reuse, 'jobs': len(members)}
        for key in SUMMARY:
            values = sorted(m[key] for m in members if m.get(key) is not None)
            if values:
                entry[key] = f'{statistics.median(values):.0f}/{_p90(values):.0f}'
        result.append(entry)
    return result


def _p90(values: list[float]) -> float:
    return values[min(len(values) - 1, round(0.9 * (len(values) - 1)))]


def report(since: str | None = None, app: str | None = None, limit: int = 50) -> str:
    items = rows(since, app, limit)
    if not items:
        return '没有符合条件的已完成任务'
    return (table(items, COLUMNS) + '\n\n按模型 × 冷热汇总（中位数/p90，秒）\n' +
            table(summary(items), ['model', 'reuse', 'jobs'] + SUMMARY))


PROMPT_EXECUTED = re.compile(r'Prompt executed in ([\d.]+) seconds')


def backfill(dry_run: bool = False) -> int:
    """老任务没有 timings：从 Volume 的 result.json 补 elapsedSeconds 与 ComfyUI 执行耗时。"""
    import modal
    from psycopg.types.json import Jsonb

    from . import pipeline

    with db.connect() as connection:
        jobs = connection.execute(
            "SELECT id, app, result_key FROM jobs WHERE state = 'succeeded' AND timings IS NULL"
            ' ORDER BY created_at').fetchall()
    volumes: dict[str, object] = {}
    count = 0
    for job in jobs:
        _app, _function, name, _field = pipeline._target(job['app'])
        volume = volumes.setdefault(name, modal.Volume.from_name(name))
        record = pipeline._read_record(volume, job['result_key'])
        if record is None:
            print(f"跳过 {job['id'][:8]}：Volume 上没有 result.json")
            continue
        tail = record.get('serverLogTail') or []
        executed = PROMPT_EXECUTED.findall('\n'.join(tail) if isinstance(tail, list) else str(tail))
        timings = {'v': 1, 'backfill': True, 'elapsedSeconds': record.get('elapsedSeconds'),
                   'comfyExecuteSeconds': round(sum(map(float, executed)), 2) if executed else None}
        if not dry_run:
            db.update_job(job['id'], timings=Jsonb(timings))
        count += 1
    return count
