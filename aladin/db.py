"""账本：Postgres 是本地权威源。

状态词表（见 ADR-0002）：
    pending → submitting → submitted → running → succeeded
                                                 └→ failed
                       └──────────────────────────→ unknown（需先查回执再决定重试）

两张表刻意分开，对应产品上的两个概念：
- `jobs` / `job_events` / `artifacts`：**生成历史**，可清理的中间产物。
- `gallery`：**图库**，用户手动挑进来的独立副本，永久保留。
图片文件本身不入库，只存相对路径与 sha256。
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

from . import settings

# 允许通过 update_job 更新的列；防止把列名拼进 SQL 时出现意外字段
UPDATABLE = frozenset({
    'state', 'call_id', 'attempts', 'max_attempts', 'lease_owner',
    'lease_expires_at', 'next_poll_at', 'last_error', 'submitted_at',
    'finished_at', 'image_count',
})

FINISHED_STATES = ('succeeded', 'failed')
ACTIVE_STATES = ('pending', 'submitting', 'submitted', 'running', 'unknown')


def connect() -> psycopg.Connection:
    """每次调用一个连接。本机规模下开销可忽略，省掉连接池与它的失效处理。"""
    return psycopg.connect(settings.DATABASE_URL, row_factory=dict_row)


def init() -> None:
    from . import migrate

    migrate.apply(settings.DATABASE_URL, verbose=False)


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


# --- jobs -----------------------------------------------------------------

def create_job(prompt: str, params: dict, request: dict, result_key: str,
               shape: str | None = None, call_id: str | None = None,
               mode: str = 'txt2img', input_sha256: str | None = None,
               input_path: str | None = None, app: str = 'image') -> str:
    """登记任务。`result_key` 上有唯一约束，同参数重复登记会抛 UniqueViolation。

    这是幂等闸门：result_key 由 prompt + 全部生成参数 + 输入图哈希算出，重跑没有意义。
    例外是**已失败**的同参数任务：它没有产物，挡住重提只会让人卡住（例如部署不同步导致的
    revision mismatch 修好之后）。这种情况就地把那一行重置为 pending 再跑一次，沿用原任务号。
    输入图只存路径与哈希：字节不进 JSONB（否则每行十几 MB），
    由 worker 在提交时读出来随请求发给容器。
    """
    job_id = uuid.uuid4().hex
    with connect() as connection:
        row = connection.execute(
            'INSERT INTO jobs (id, app, state, prompt, params, request, result_key,'
            ' call_id, image_count, attempts, mode, input_sha256, input_path)'
            ' VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)'
            ' ON CONFLICT (result_key) DO UPDATE SET'
            "  state = 'pending', prompt = EXCLUDED.prompt, params = EXCLUDED.params,"
            '  request = EXCLUDED.request, image_count = EXCLUDED.image_count,'
            '  input_path = EXCLUDED.input_path, call_id = NULL, attempts = 0,'
            '  last_error = NULL, finished_at = NULL, submitted_at = NULL, next_poll_at = NULL,'
            '  lease_owner = NULL, lease_expires_at = NULL'
            "  WHERE jobs.state = 'failed'"
            ' RETURNING id',
            (job_id, app, 'pending', prompt, dumps(params), dumps(request),
             result_key, call_id, params.get('images', 1), 1 if call_id else 0,
             mode, input_sha256, input_path)).fetchone()
        if row is None:
            # 撞上的是未失败的任务：保持原来的幂等语义，由调用方反查已存在任务
            raise psycopg.errors.UniqueViolation('相同参数的任务已存在')
    if row['id'] != job_id:
        add_event(row['id'], 'retry', '同参数重新提交：上次失败，重置后再跑一次')
        notify(row['id'])
    return row['id']


def update_job(job_id: str, **fields: Any) -> None:
    if not fields:
        return
    unknown = set(fields) - UPDATABLE
    if unknown:
        raise ValueError('不可更新的列: ' + ', '.join(sorted(unknown)))
    columns = ', '.join(f'{name} = %s' for name in fields)
    with connect() as connection:
        connection.execute(f'UPDATE jobs SET {columns} WHERE id = %s',
                           (*fields.values(), job_id))


def finish_job(job_id: str, state: str, error: str | None = None) -> None:
    update_job(job_id, state=state, last_error=error, finished_at=_now(),
               lease_owner=None, lease_expires_at=None, next_poll_at=None)


def _now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc)


def job(job_id: str) -> dict | None:
    with connect() as connection:
        return connection.execute('SELECT * FROM jobs WHERE id = %s', (job_id,)).fetchone()


def job_by_result_key(result_key: str) -> dict | None:
    """按幂等键反查任务。提交撞车时用它把已存在任务的 id 还给调用方。"""
    with connect() as connection:
        return connection.execute(
            'SELECT * FROM jobs WHERE result_key = %s', (result_key,)).fetchone()


def recent_jobs(limit: int = 20, state: str | None = None,
                states: tuple[str, ...] | None = None, mode: str | None = None) -> list[dict]:
    """过滤必须在 LIMIT 之前做：先取 N 条再在 Python 里筛，结果会莫名变少。"""
    conditions: list[str] = []
    args: list[Any] = []
    if state:
        conditions.append('state = %s')
        args.append(state)
    if states:
        conditions.append('state = ANY(%s)')
        args.append(list(states))
    if mode:
        # 老任务的 mode 可能为空，按文生图算
        conditions.append("COALESCE(mode, 'txt2img') = %s")
        args.append(mode)
    query = 'SELECT * FROM jobs'
    if conditions:
        query += ' WHERE ' + ' AND '.join(conditions)
    query += ' ORDER BY created_at DESC LIMIT %s'
    args.append(limit)
    with connect() as connection:
        return list(connection.execute(query, args).fetchall())


def job_extras(job_ids: list[str], progress_ids: list[str] | None = None) -> dict:
    """列表页一次取齐：产物、进度事件（仅 progress_ids）、哪些 sha256 已在图库。

    逐个任务查的话，60 条任务要开几百个连接，还要把每个采样步的事件全读一遍。
    """
    extras: dict = {'artifacts': {job_id: [] for job_id in job_ids},
                    'progress': {}, 'gallery': set()}
    if not job_ids:
        return extras
    with connect() as connection:
        for row in connection.execute(
                'SELECT * FROM artifacts WHERE job_id = ANY(%s) ORDER BY job_id, name',
                (job_ids,)).fetchall():
            extras['artifacts'][row['job_id']].append(row)
        # 进度只对进行中的任务有意义；已结束的任务不读它们成百上千条采样事件
        if progress_ids:
            for row in connection.execute(
                    "SELECT job_id, kind, message FROM job_events"
                    " WHERE job_id = ANY(%s) AND kind IN ('queued', 'retry', 'progress')"
                    " ORDER BY id", (list(progress_ids),)).fetchall():
                extras['progress'].setdefault(row['job_id'], []).append(row)
        shas = [a['sha256'] for items in extras['artifacts'].values() for a in items]
        if shas:
            extras['gallery'] = {row['sha256'] for row in connection.execute(
                'SELECT sha256 FROM gallery WHERE sha256 = ANY(%s)', (shas,)).fetchall()}
    return extras


def unfinished_jobs() -> list[dict]:
    """worker 启动时用来看有哪些任务需要接管。"""
    with connect() as connection:
        return list(connection.execute(
            'SELECT * FROM jobs WHERE state = ANY(%s) ORDER BY created_at',
            (list(ACTIVE_STATES),)).fetchall())


# --- events ---------------------------------------------------------------

def add_event(job_id: str, kind: str, message: str,
              dedupe_key: str | None = None) -> bool:
    """追加一条事件；返回是否真的写入。

    `dedupe_key` 让重放无害：日志流断了要重连，而重连是否重放旧行不由我们控制，
    所以靠唯一索引把重复吞掉，而不是假设它不会发生。
    """
    with connect() as connection:
        cursor = connection.execute(
            'INSERT INTO job_events (job_id, kind, message, dedupe_key)'
            ' VALUES (%s,%s,%s,%s)'
            ' ON CONFLICT (job_id, dedupe_key) WHERE dedupe_key IS NOT NULL'
            ' DO NOTHING',
            (job_id, kind, message, dedupe_key))
        return cursor.rowcount > 0


def events(job_id: str, after: int = 0) -> list[dict]:
    """按全局递增 id 取增量；`after` 就是 SSE 的 Last-Event-ID。"""
    with connect() as connection:
        return list(connection.execute(
            'SELECT * FROM job_events WHERE job_id = %s AND id > %s ORDER BY id',
            (job_id, after)).fetchall())


# --- artifacts ------------------------------------------------------------

def add_artifact(job_id: str, name: str, rel_path: str, sha256: str, size: int,
                 seed: int, prompt: str, params: dict | None = None) -> None:
    """登记一个产物。`params` 是这张图**自己**的参数（批量任务里每张都可能不同）。"""
    with connect() as connection:
        connection.execute(
            'INSERT INTO artifacts (job_id, name, rel_path, sha256, bytes, seed, prompt,'
            ' params) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)'
            ' ON CONFLICT (job_id, name) DO UPDATE SET rel_path = EXCLUDED.rel_path,'
            ' sha256 = EXCLUDED.sha256, bytes = EXCLUDED.bytes,'
            ' params = EXCLUDED.params',
            (job_id, name, rel_path, sha256, size, seed, prompt,
             dumps(params) if params is not None else None))


def recent_artifacts(limit: int = 12, app: str | None = None) -> list[dict]:
    """跨任务取最近的产物，首页缩略图用。给了 app 就只取那个切片的产物。"""
    query = 'SELECT a.* FROM artifacts a'
    args: list[Any] = []
    if app:
        query += ' JOIN jobs j ON j.id = a.job_id WHERE j.app = %s'
        args.append(app)
    query += ' ORDER BY a.at DESC, a.id DESC LIMIT %s'
    args.append(limit)
    with connect() as connection:
        return list(connection.execute(query, args).fetchall())


def artifacts(job_id: str) -> list[dict]:
    with connect() as connection:
        return list(connection.execute(
            'SELECT * FROM artifacts WHERE job_id = %s ORDER BY name', (job_id,)).fetchall())


def artifact(job_id: str, name: str) -> dict | None:
    with connect() as connection:
        return connection.execute(
            'SELECT * FROM artifacts WHERE job_id = %s AND name = %s',
            (job_id, name)).fetchone()


def review_artifact(job_id: str, name: str, review: dict) -> bool:
    from psycopg.types.json import Jsonb
    with connect() as connection:
        return connection.execute(
            'UPDATE artifacts SET review = %s WHERE job_id = %s AND name = %s',
            (Jsonb(review), job_id, name)).rowcount > 0


# --- gallery --------------------------------------------------------------

def add_to_gallery(rel_path: str, prompt: str, seed: int, sha256: str, size: int,
                   source_job: str | None, model: str | None = None, mode: str | None = None,
                   rating: str | None = None) -> bool:
    """把一张图放进图库；prompt、seed 与来源（模型/模式/尺度）一起带过去。已存在则返回 False。"""
    with connect() as connection:
        cursor = connection.execute(
            'INSERT INTO gallery (rel_path, prompt, seed, sha256, bytes, source_job,'
            ' model, mode, rating) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)'
            ' ON CONFLICT (sha256) DO NOTHING',
            (rel_path, prompt, seed, sha256, size, source_job, model, mode, rating))
        return cursor.rowcount > 0


GALLERY_SORTS = {'newest': 'at DESC', 'oldest': 'at ASC', 'stars': 'stars DESC, at DESC'}


def gallery_search(q: str = '', tag: str = '', model: str = '', kind: str = '',
                   min_stars: int = 0, rating: str = '', sort: str = 'newest') -> list[dict]:
    """收藏检索。所有条件都在 SQL 里做；提示词搜索不区分大小写。"""
    conditions, args = [], []
    if q:
        conditions.append("prompt ILIKE %s ESCAPE '\\'")
        args.append('%' + q.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%')
    if tag:
        conditions.append('%s = ANY(tags)')
        args.append(tag)
    if model:
        conditions.append('model = %s')
        args.append(model)
    if rating:
        conditions.append('rating = %s')
        args.append(rating)
    if kind == 'video':
        conditions.append("rel_path ILIKE '%%.webm'")
    elif kind == 'image':
        conditions.append("rel_path NOT ILIKE '%%.webm'")
    if min_stars:
        conditions.append('stars >= %s')
        args.append(min_stars)
    query = 'SELECT * FROM gallery'
    if conditions:
        query += ' WHERE ' + ' AND '.join(conditions)
    query += ' ORDER BY ' + GALLERY_SORTS.get(sort, GALLERY_SORTS['newest'])
    with connect() as connection:
        return list(connection.execute(query, args).fetchall())


def gallery_facets() -> dict:
    """筛选项：用过的标签、模型、尺度及各自数量。"""
    with connect() as connection:
        tags = connection.execute(
            'SELECT tag, count(*) AS n FROM gallery, unnest(tags) AS tag'
            ' GROUP BY tag ORDER BY n DESC, tag').fetchall()
        models = connection.execute(
            'SELECT model, count(*) AS n FROM gallery WHERE model IS NOT NULL'
            ' GROUP BY model ORDER BY n DESC').fetchall()
        ratings = connection.execute(
            'SELECT rating, count(*) AS n FROM gallery WHERE rating IS NOT NULL'
            ' GROUP BY rating ORDER BY n DESC').fetchall()
    return {'tags': [(r['tag'], r['n']) for r in tags],
            'models': [(r['model'], r['n']) for r in models],
            'ratings': [(r['rating'], r['n']) for r in ratings]}


def update_gallery_item(item_id: int, tags: list[str] | None = None,
                        stars: int | None = None) -> dict | None:
    fields, args = [], []
    if tags is not None:
        fields.append('tags = %s')
        args.append(tags)
    if stars is not None:
        fields.append('stars = %s')
        args.append(stars)
    with connect() as connection:
        if fields:
            return connection.execute(
                f"UPDATE gallery SET {', '.join(fields)} WHERE id = %s RETURNING *",
                (*args, item_id)).fetchone()
        return connection.execute('SELECT * FROM gallery WHERE id = %s', (item_id,)).fetchone()


def gallery_items() -> list[dict]:
    with connect() as connection:
        return list(connection.execute('SELECT * FROM gallery ORDER BY at DESC').fetchall())


def gallery_item(item_id: int) -> dict | None:
    with connect() as connection:
        return connection.execute('SELECT * FROM gallery WHERE id = %s', (item_id,)).fetchone()


def in_gallery(sha256: str) -> bool:
    with connect() as connection:
        return connection.execute(
            'SELECT 1 FROM gallery WHERE sha256 = %s', (sha256,)).fetchone() is not None


def gallery_has_path(rel_path: str) -> bool:
    with connect() as connection:
        return connection.execute(
            'SELECT 1 FROM gallery WHERE rel_path = %s', (rel_path,)).fetchone() is not None


def remove_from_gallery(item_id: int) -> dict | None:
    with connect() as connection:
        row = connection.execute('SELECT * FROM gallery WHERE id = %s', (item_id,)).fetchone()
        if row is not None:
            connection.execute('DELETE FROM gallery WHERE id = %s', (item_id,))
        return row


# --- billing ---------------------------------------------------------------

def save_billing(payload: dict) -> None:
    """写入最新账单快照（单行表，新覆盖旧）。"""
    with connect() as connection:
        connection.execute(
            'INSERT INTO billing_snapshot'
            ' (id, cycle_start, cycle_end, metered_cost, billed_cost,'
            '  credits_applied, credit_grant, adjustments, breakdown)'
            ' VALUES (1, %s,%s,%s,%s,%s,%s,%s,%s)'
            ' ON CONFLICT (id) DO UPDATE SET'
            '  cycle_start = EXCLUDED.cycle_start, cycle_end = EXCLUDED.cycle_end,'
            '  metered_cost = EXCLUDED.metered_cost,'
            '  billed_cost = EXCLUDED.billed_cost,'
            '  credits_applied = EXCLUDED.credits_applied,'
            '  credit_grant = EXCLUDED.credit_grant,'
            '  adjustments = EXCLUDED.adjustments,'
            '  breakdown = EXCLUDED.breakdown,'
            '  fetched_at = now()',
            (payload['cycle_start'], payload['cycle_end'], payload['metered_cost'],
             payload['billed_cost'], payload['credits_applied'],
             payload.get('credit_grant'), dumps(payload['adjustments']),
             dumps(payload['breakdown'])))


def billing_snapshot() -> dict | None:
    with connect() as connection:
        return connection.execute(
            'SELECT * FROM billing_snapshot WHERE id = 1').fetchone()


# --- 通知 ------------------------------------------------------------------

NOTIFY_CHANNEL = 'aladin_job'


def notify(job_id: str) -> None:
    """告诉 api 有变化。

    通知只负责**唤醒**，数据永远从表里读：LISTEN/NOTIFY 不保证送达，
    丢了也只是让 SSE 晚一个心跳周期（30s）才补上增量。
    """
    with connect() as connection:
        connection.execute('SELECT pg_notify(%s, %s)', (NOTIFY_CHANNEL, job_id))


# --- 认领 ------------------------------------------------------------------

def claim(where: str, args: tuple, owner: str, new_state: str | None = None,
          lease_seconds: int = 90) -> dict | None:
    """认领一行并加租约。

    两件事必须一起做，否则认领没有意义：
    1. `FOR UPDATE SKIP LOCKED` —— 只在**并发事务内**互斥，提交后就放开。
    2. 同一事务里改状态 / 记租约 —— 否则刚提交就会被别人再选中。
    另外排除仍在他人有效期内的租约，让崩溃的 worker 留下的行在过期后可被接管。
    """
    sql = (f'SELECT * FROM jobs WHERE ({where})'
           ' AND (lease_owner IS NULL OR lease_expires_at < now() OR lease_owner = %s)'
           ' ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1')
    assignments = ['lease_owner = %s',
                   'lease_expires_at = now() + make_interval(secs => %s)']
    values: list[Any] = [owner, lease_seconds]
    if new_state is not None:
        assignments.append('state = %s')
        values.append(new_state)
    with connect() as connection:
        with connection.transaction():
            row = connection.execute(sql, (*args, owner)).fetchone()
            if row is None:
                return None
            connection.execute(
                f"UPDATE jobs SET {', '.join(assignments)} WHERE id = %s",
                (*values, row['id']))
            row['lease_owner'] = owner
            if new_state is not None:
                row['state'] = new_state
            return row


def data_path(rel_path: str) -> Path:
    """产物路径以 DATA 为基准，这样 ALADIN_DATA 指到仓库外也能工作。"""
    return settings.DATA / rel_path


def accept_plan(row: dict, request: dict, params: dict) -> bool:
    """计划与下一阶段请求一次提交；重复轮询不会重复安排图片任务。"""
    with connect() as connection:
        cursor = connection.execute(
            "UPDATE jobs SET request=%s, params=%s, image_count=%s, state='pending',"
            " call_id=NULL, attempts=0, next_poll_at=NULL, lease_owner=NULL,"
            " lease_expires_at=NULL, last_error=NULL"
            " WHERE id=%s AND request=%s::jsonb AND state IN ('submitted','running','unknown')",
            (dumps(request), dumps(params), params['images'], row['id'], dumps(row['request'])))
        return cursor.rowcount > 0
