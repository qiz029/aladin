"""测试支撑：把 DATABASE_URL 指向独立测试库。

DB 不再是随 ALADIN_DATA 走的临时文件，所以测试必须自己隔离：
连到 Postgres 建一个专用库（不存在才建），并在每个用例前清空表。

刻意不 import aladin.settings —— 调用方必须在 import 任何 aladin 业务模块**之前**
调用 `configure()`，否则 settings 已经把 DATABASE_URL 读成生产库了。
"""
from __future__ import annotations

import os

import psycopg

ADMIN_URL = os.environ.get('ALADIN_TEST_ADMIN_URL',
                           'postgresql://aladin:aladin-local@127.0.0.1:5433/postgres')


def configure(name: str = 'aladin_test') -> str:
    """建库（如需要）并把 DATABASE_URL 指过去，返回该 URL。"""
    with psycopg.connect(ADMIN_URL, autocommit=True) as admin:
        exists = admin.execute(
            'SELECT 1 FROM pg_database WHERE datname = %s', (name,)).fetchone()
        if not exists:
            admin.execute(f'CREATE DATABASE {name}')
    url = ADMIN_URL.rsplit('/', 1)[0] + '/' + name
    os.environ['DATABASE_URL'] = url
    return url


def reset() -> None:
    """清空业务表，保留 schema。"""
    with psycopg.connect(os.environ['DATABASE_URL'], autocommit=True) as connection:
        connection.execute('TRUNCATE jobs, gallery, billing_snapshot, remote_purge RESTART IDENTITY CASCADE')
