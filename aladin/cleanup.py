"""删除任务与清理生成历史。

本地立刻删：账本行（事件、产物级联）、`data/jobs/<id>/`、不再被任何任务引用的输入图。
图库是独立副本，不受影响。Modal 结果 Volume 上的副本在这里只**登记**到 remote_purge——
api 不持有 Modal 凭据（见 ADR-0002），由 worker 定期执行删除（pipeline._purge_remote）。

只删终态任务：进行中的任务可能还在 Modal 上跑，删了账本行就没人接管它了。
"""
from __future__ import annotations

import hashlib
import os
import shutil
from datetime import datetime, timedelta, timezone

from . import db, metadata, settings


class JobActive(Exception):
    """任务尚未结束，不能删。"""


def delete_job(job_id: str) -> bool:
    """删除一个终态任务及其本地文件。不存在返回 False；进行中抛 JobActive。"""
    with db.connect() as connection:
        row = connection.execute(
            'DELETE FROM jobs WHERE id = %s AND state = ANY(%s)'
            ' RETURNING id, input_path, app, mode, result_key',
            # review（等确认）也能删：此时没有在跑的 Modal 调用
            (job_id, list(db.FINISHED_STATES) + ['review'])).fetchone()
        if row is None:
            exists = connection.execute(
                'SELECT 1 FROM jobs WHERE id = %s', (job_id,)).fetchone()
            if exists:
                raise JobActive('任务仍在进行，结束后再删')
            return False
        for volume in remote_volumes(row):
            connection.execute(
                'INSERT INTO remote_purge (volume, path) VALUES (%s, %s)'
                ' ON CONFLICT (volume, path) DO NOTHING', (volume, row['result_key']))
        orphan_input = None
        if row['input_path']:
            shared = connection.execute(
                'SELECT 1 FROM jobs WHERE input_path = %s LIMIT 1',
                (row['input_path'],)).fetchone()
            orphan_input = None if shared else row['input_path']
    # 行先删、文件后删：文件删到一半失败，最多留下没人引用的文件，不会留下指向空文件的行
    shutil.rmtree(settings.JOBS_DIR / job_id, ignore_errors=True)
    if orphan_input:
        (settings.DATA / orphan_input).unlink(missing_ok=True)
    return True


def remote_volumes(row: dict) -> list[str]:
    """这个任务在 Modal 上留下结果的 Volume（目录名都是 result_key）。"""
    from . import director
    if (row.get('app') or 'image') == 'video':
        return [settings.VOLUME_RESULTS_VIDEO]
    volumes = [settings.VOLUME_RESULTS]          # 三个生图模型共用这个结果 Volume
    if row.get('mode') == 'director':
        volumes.append(director.RESULTS_VOLUME)   # 规划回执
    return volumes


def stale_jobs(days: int) -> list[dict]:
    """超过 `days` 天的终态任务。带人工评审的产物是人工劳动，不自动清。"""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    with db.connect() as connection:
        return list(connection.execute(
            'SELECT id, state, prompt, created_at FROM jobs j'
            ' WHERE state = ANY(%s) AND COALESCE(finished_at, created_at) < %s'
            ' AND NOT EXISTS (SELECT 1 FROM artifacts a'
            '                 WHERE a.job_id = j.id AND a.review IS NOT NULL)'
            ' ORDER BY created_at',
            (list(db.FINISHED_STATES), cutoff)).fetchall())


def purge(days: int, dry_run: bool = False) -> int:
    rows = stale_jobs(days)
    for row in rows:
        label = f"{row['id'][:8]}  {row['state']:<9} {(row['prompt'] or '')[:50]}"
        if dry_run:
            print('将删除 ' + label)
        elif delete_job(row['id']):
            print('已删除 ' + label)
    return len(rows)


def strip_existing_metadata(dry_run: bool = False) -> int:
    """一次性清理：把已有产物和收藏里内嵌的 workflow / 提示词去掉，返回处理的文件数。

    新产物在容器里就不写元数据了（--disable-metadata），这里只处理之前生成的。
    改的是文件字节，所以账本里的 sha256 / bytes 一起更新；先写临时文件、再改账本、最后替换，
    账本更新失败（例如收藏的 sha256 撞车）就不动原文件。
    """
    with db.connect() as connection:
        rows = [('artifacts', row) for row in connection.execute(
            'SELECT job_id, name, rel_path FROM artifacts ORDER BY id').fetchall()]
        rows += [('gallery', row) for row in connection.execute(
            'SELECT id, rel_path FROM gallery ORDER BY id').fetchall()]
    count = 0
    for table, row in rows:
        path = settings.DATA / row['rel_path']
        if not path.is_file() or path.suffix.lower() not in ('.png', '.webm'):
            continue
        data = path.read_bytes()
        clean = metadata.strip(data, path.name)
        if clean == data:
            continue
        count += 1
        removed = len(data) - len(clean)
        label = f"{table:<9} {row['rel_path']}  " + (f'去掉 {removed} 字节' if removed else '原地清零')
        if dry_run:
            print('将清理 ' + label)
            continue
        temporary = path.with_name(path.name + '.strip')
        temporary.write_bytes(clean)
        digest = hashlib.sha256(clean).hexdigest()
        try:
            with db.connect() as connection:
                if table == 'artifacts':
                    connection.execute(
                        'UPDATE artifacts SET sha256 = %s, bytes = %s WHERE job_id = %s AND name = %s',
                        (digest, len(clean), row['job_id'], row['name']))
                else:
                    connection.execute('UPDATE gallery SET sha256 = %s, bytes = %s WHERE id = %s',
                                       (digest, len(clean), row['id']))
        except Exception as error:
            temporary.unlink(missing_ok=True)
            print(f'跳过 {label}: {type(error).__name__}: {error}')
            continue
        os.replace(temporary, path)
        print('已清理 ' + label)
    return count
