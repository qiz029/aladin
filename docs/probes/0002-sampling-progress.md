# 探针 0002：容器内采样步级进度

日期：2026-09-20
验证状态：**通过**（真实 L4 容器，端到端）
复现：`.venv/bin/modal deploy probe/modal_app.py` 后调用 `sampling_progress_probe`

## 结论

容器内可以实时拿到采样步级进度，并已通过 Modal 日志流回到本地：

```
[aladin-progress] image=1/1 step=1/25 node=sampler0
... step=3/25, 5/25, 7/25 ... 25/25
```

实测 `ws_event_counts = {"progress": 13, ...}`，与发出的 13 条结构化行一致；图片正常生成。

## 三个必须做对的地方（都是实测踩出来的）

### 1. 必须声明并复用同一个 `clientId`——这是最容易错的一步

采样进度**不是广播**。`comfy_execution/execution.py` 里：

```python
if "client_id" in extra_data:
    self.server.client_id = extra_data["client_id"]
else:
    self.server.client_id = None
```

而 `hijack_progress` 用 `server.send_sync("progress", ..., server.client_id)` 发送。
所以进度**只发给与提交时 `extra_data["client_id"]` 相同的那个 WebSocket 连接**。

实测证据：观察者不声明 clientId 时，收到了 4 条 `status`（广播，`sid=None`）但
**一条 `progress` 都没有**；声明并复用同一 clientId 后立刻正常。
`status` 是广播、`progress` 是定向——这个差别就是判断依据。

### 2. `prompt_id` 必须是规范 UUID

自定义字符串会被 400 拒绝：
`prompt_id must be a UUID string in canonical lowercase hyphenated form`。
所以 aladin 的幂等键不能直接当 ComfyUI 的 prompt_id 用，需要单独生成 UUID 并建立映射。

### 3. 模型路径要显式告知 ComfyUI

要传 `--extra-model-paths-config` 指向 Volume 里的模型目录，否则
`CheckpointLoaderSimple` 报 `ckpt_name ... not in []`。

## 节流：不是每步都发

25 步实测只发出 **13 条** `progress`（1、3、5…25），即**每 2 步一条**。
`ProgressBar.update_absolute` 的触发条件是「距上次 ≥100ms **且** 累计变化 ≥0.5%」，
两个条件同时满足才发。所以：

- 对 UI 而言这是**够用的粒度**（进度条平滑推进），
- 但若要求严格"每一步一行"，需要在容器内 patch 那两个阈值（`comfy/utils.py` 的常量）。

建议：**不 patch**。13 条对个人工具完全够用，patch 会增加与 ComfyUI 版本的耦合。

## 其他实测事实

- ComfyUI 在 L4 容器内启动约 **15–21s**（含 torch 导入）。
- 该小模型（SD1.5 fp16）512×512、25 步采样约 **10–12s**，采样速率 ~12 it/s。
- 镜像层复用有效：基础镜像重建仅 2–3 秒，前提是 apt/pip/git 步骤与
  `agent-media-lab-image-v1` **逐字一致**（多一层 `pip install` 会后续层全失效）。
- 容器返回值仍可能无法在本地反序列化（torch 类型）；产物与证据应写 Volume。

## 后续验证（同日补测）

容器 stdout → Modal 日志流 → 本地 daemon → 账本 的完整链路已跑通：
`pipeline.parse_progress()` 把 `[aladin-progress] {json}` 逐条落成账本事件，
端到端可见 `{"image":2,"total":2,"step":9,"max":20,"node":"sampler1"}`。
在此之前探针只验证到"容器 stdout 出现了这些行"。

## 对 aladin 的净收益

`probe/watchdog.py` 就是那段容器内观察者，已被本次验证证明可用：
连接 `/ws?clientId=...` → 按 `prompt_id` 过滤 `progress` → 输出结构化 stdout 行。
容器镜像只需多一个 `websocket-client` 依赖。本地纯函数部分有 7 个单元测试覆盖。
