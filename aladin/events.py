"""进程内发布订阅：Postgres NOTIFY 唤醒，SSE 订阅者收到后自己去表里读。

设计要点（见 ADR-0002）：通知只负责**唤醒**，不携带数据。
LISTEN/NOTIFY 不保证送达，丢了也只是让 SSE 晚一个兜底周期（30s）才补上增量。
"""
from __future__ import annotations

import queue
import threading
import time

import psycopg

from . import db, settings

_subscribers: dict[str, set[queue.Queue]] = {}
_lock = threading.Lock()
_started = False
MAX_QUEUE = 32


def subscribe(job_id: str) -> queue.Queue:
    channel: queue.Queue = queue.Queue(maxsize=MAX_QUEUE)
    with _lock:
        _subscribers.setdefault(job_id, set()).add(channel)
    return channel


def unsubscribe(job_id: str, channel: queue.Queue) -> None:
    with _lock:
        channels = _subscribers.get(job_id)
        if channels is None:
            return
        channels.discard(channel)
        if not channels:
            _subscribers.pop(job_id, None)


def _dispatch(job_id: str) -> None:
    with _lock:
        channels = list(_subscribers.get(job_id, ()))
    for channel in channels:
        try:
            channel.put_nowait(job_id)
        except queue.Full:
            pass          # 订阅者来不及消费；它会在兜底周期里自己补


def start_listener() -> None:
    """起一个 LISTEN 线程；重复调用只会起一个。"""
    global _started
    with _lock:
        if _started:
            return
        _started = True
    threading.Thread(target=_listen_loop, daemon=True, name='pg-listen').start()


def _listen_loop() -> None:
    while True:
        try:
            with psycopg.connect(settings.DATABASE_URL, autocommit=True) as connection:
                connection.execute(f'LISTEN {db.NOTIFY_CHANNEL}')
                for notification in connection.notifies():
                    _dispatch(notification.payload)
        except Exception as error:      # 连接断了就重连，不能让通知通道永久失效
            print(f'LISTEN 断开，2s 后重连: {type(error).__name__}: {error}', flush=True)
            time.sleep(2)
