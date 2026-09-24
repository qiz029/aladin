"""观察容器内 ComfyUI 的采样进度，并转成容器 stdout 的结构化行。

设计前提（均来自 ComfyUI 源码，非推测）：

1. `PromptServer.send_sync(event, data, sid)` 收到的 `sid` 是 `self.client_id`，
   而 `self.client_id` 在 server.py 里除 `__init__` 外从未被赋值，恒为 `None`。
   因此服务端消息是**广播**给所有 WebSocket 客户端，本观察者无需声明 clientId。
2. 广播意味着并发时会看到别人的任务，所以必须按 `prompt_id` 过滤。
3. `progress` 消息形如 `{"value":12,"max":30,"prompt_id":...,"node":"sampler1"}`，
   每步是否发出由 `ProgressBar` 节流决定（默认 100ms 且 0.5%）。
"""
from __future__ import annotations

import json
import threading
import time

PREFIX = '[aladin-progress]'


def classify(workflow: dict) -> dict:
    """从工作流里找出 sampler 节点及其对应的图片序号。

    `image_request()` 会把 n 个 prompt 展开成 sampler0..sampler{n-1}，
    这里据此建立 node_id -> (index, total) 的映射。
    """
    sampler_nodes = sorted(
        (node_id for node_id, spec in workflow.items()
         if spec.get('class_type') == 'KSampler'),
        key=lambda node_id: int(''.join(c for c in node_id if c.isdigit()) or 0),
    )
    total = len(sampler_nodes)
    return {node_id: (position, total)
            for position, node_id in enumerate(sampler_nodes, start=1)}


def ws_url(server_url: str) -> str:
    """把 HTTP base URL 转成 WebSocket URL。

    websocket-client 的 WebSocketApp 只接受 ws/wss，传 http 会抛
    `ValueError: scheme http is invalid`（容器实测）。
    """
    if server_url.startswith('https://'):
        return 'wss://' + server_url[len('https://'):]
    if server_url.startswith('http://'):
        return 'ws://' + server_url[len('http://'):]
    return server_url


def progress_info(event: dict, prompt_id: str, mapping: dict) -> dict | None:
    """把一条服务端消息归一成进度信息；不属于本任务或无关的事件返回 None。"""
    if not isinstance(event, dict) or event.get('type') != 'progress':
        return None
    data = event.get('data')
    if not isinstance(data, dict) or data.get('prompt_id') != prompt_id:
        return None  # 属于其他任务的消息
    node = data.get('node')
    if node not in mapping:
        return None
    image, total = mapping[node]
    return {'image': image, 'total': total, 'step': data.get('value'),
            'max': data.get('max'), 'node': node}


def format_progress(event: dict, prompt_id: str, mapping: dict) -> str | None:
    """人类可读的进度行（探针直接用）。"""
    info = progress_info(event, prompt_id, mapping)
    if info is None:
        return None
    return (f"{PREFIX} image={info['image']}/{info['total']} "
            f"step={info['step']}/{info['max']} node={info['node']}")


class WatchHandle:
    """正在运行的观察者。`ready` 在 WebSocket 订阅建立后才置位。"""

    def __init__(self):
        self.ready = threading.Event()
        self.done = threading.Event()
        self._emitted = 0
        self.counts: dict[str, int] = {}
        self.seen: list[str] = []

    def _finish(self, emitted: int):
        self._emitted = emitted
        self.done.set()

    def wait(self, timeout: float | None = None) -> int:
        """等 WebSocket 线程结束并返回已发出的进度行数。"""
        self.done.wait(timeout)
        return self._emitted


def watch_async(server_url: str, prompt_id: str, mapping: dict,
                timeout: float = 600.0, on_line=None,
                capture_raw: bool = False, client_id: str | None = None,
                on_event=None) -> WatchHandle:
    """后台订阅 ComfyUI 的 /ws，把属于 prompt_id 的采样进度写成结构化 stdout 行。

    `client_id` 必须与提交 prompt 时 `extra_data["client_id"]` 一致：
    execution.py 里 `self.server.client_id = extra_data["client_id"]`，
    而采样进度只发给 `server.client_id` 对应的那个连接（实测：不声明则收不到任何 progress）。
    返回句柄；调用方用 `handle.ready` 等到订阅建立后再提交 prompt，
    最后用 `handle.wait()` 取回发出的进度行数。
    `handle.counts` 记录收到的事件类型计数，`handle.seen`（capture_raw 时）
    保留原始消息，用于诊断"为什么一条进度都没到"。
    `on_event` 收到每一条解析后的消息（例如 SaveImage 的 `executed`）；
    它抛出的异常被吞掉，不能拖垮进度流。
    """
    try:
        import websocket  # websocket-client
    except ImportError as error:
        raise RuntimeError('需要 websocket-client；Modal 镜像里要加入该依赖') from error

    handle = WatchHandle()
    emitted = [0]

    def on_open(_ws):
        handle.ready.set()

    def on_message(_ws, raw):
        if isinstance(raw, (bytes, bytearray)):
            handle.counts['<binary>'] = handle.counts.get('<binary>', 0) + 1
            return  # 预览图等二进制帧，本观察者不消费
        if capture_raw:
            handle.seen.append(raw if isinstance(raw, str) else str(raw))
        try:
            event = json.loads(raw)
        except (ValueError, TypeError):
            handle.counts['<unparsable>'] = handle.counts.get('<unparsable>', 0) + 1
            return
        kind = event.get('type') if isinstance(event, dict) else '<non-dict>'
        handle.counts[kind] = handle.counts.get(kind, 0) + 1
        if on_event is not None and isinstance(event, dict):
            try:
                on_event(event)
            except Exception:
                pass
        line = format_progress(event, prompt_id, mapping)
        if line is None:
            return
        print(line, flush=True)
        emitted[0] += 1
        if on_line is not None:
            on_line(line)

    url = ws_url(server_url).rstrip('/') + '/ws'
    if client_id:
        url += '?clientId=' + client_id
    ws = websocket.WebSocketApp(url, on_open=on_open, on_message=on_message)

    def run():
        try:
            ws.run_forever(ping_interval=20, ping_timeout=10)
        finally:
            handle._finish(emitted[0])

    threading.Thread(target=run, daemon=True).start()

    def expire():
        if handle.done.wait(timeout):
            return
        ws.close()

    threading.Thread(target=expire, daemon=True).start()
    return handle


def watch(server_url: str, prompt_id: str, mapping: dict, stop: threading.Event,
          timeout: float = 600.0, ready_timeout: float = 30.0) -> int:
    """阻塞版本：等订阅建立、跑满超时或被 stop，然后返回发出的进度行数。"""
    handle = watch_async(server_url, prompt_id, mapping, timeout)
    handle.ready.wait(ready_timeout)
    started = time.time()
    try:
        while not stop.is_set() and time.time() - started < timeout:
            time.sleep(0.2)
    finally:
        handle.done.wait(1.0)
    return handle.wait(timeout=1.0)
