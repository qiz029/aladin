"""编号 SQL 迁移。用 Postgres advisory lock 串行化，api 与 worker 都可安全调用。"""
from __future__ import annotations

import sys
from pathlib import Path

import psycopg

MIGRATIONS = Path(__file__).resolve().parent.parent / 'migrations'
# 任意固定值：只要 api 与 worker 用同一个
LOCK_KEY = 0x616C6164  # 'alad'


def applied_versions(connection: psycopg.Connection) -> set[str]:
    connection.execute(
        'CREATE TABLE IF NOT EXISTS schema_migrations ('
        ' version TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())')
    rows = connection.execute('SELECT version FROM schema_migrations').fetchall()
    return {row[0] for row in rows}


def apply(dsn: str, verbose: bool = True) -> list[str]:
    """应用尚未执行的迁移，返回本次应用的版本列表。"""
    files = sorted(MIGRATIONS.glob('*.sql'))
    if not files:
        raise RuntimeError(f'迁移目录里没有 SQL：{MIGRATIONS}')

    done: list[str] = []
    with psycopg.connect(dsn, autocommit=True) as connection:
        # advisory lock 在会话级；连接关闭即释放
        connection.execute('SELECT pg_advisory_lock(%s)', (LOCK_KEY,))
        try:
            versions = applied_versions(connection)
            for path in files:
                version = path.stem
                if version in versions:
                    continue
                # 每个迁移单独一个事务：失败不会留下半截 schema
                with connection.transaction():
                    connection.execute(path.read_text())
                    connection.execute(
                        'INSERT INTO schema_migrations (version) VALUES (%s)', (version,))
                done.append(version)
                if verbose:
                    print(f'applied {version}', flush=True)
        finally:
            connection.execute('SELECT pg_advisory_unlock(%s)', (LOCK_KEY,))
    return done


if __name__ == '__main__':
    from .settings import DATABASE_URL

    print('applied:', apply(DATABASE_URL) or '(无新迁移)')
    sys.exit(0)
