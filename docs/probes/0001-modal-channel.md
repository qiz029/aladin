# 探针 0001：Modal 通道与产物回传

日期：2026-09-20
运行环境：workstation macOS，aladin 自己的 `.venv`（`modal==1.5.5`）
验证状态：三条通道均已实测打通

## 目的

用真实调用替代此前的推理，验证 aladin 依赖的三条通道并取得第一批数字：

1. 提交：`Function.from_name(...).spawn(request)`
2. 实时进度：`FunctionCall.logs.stream()`
3. 产物回传：从 Modal Volume 拉回本地

## 复现命令

```sh
uv --cache-dir .cache/uv sync
.venv/bin/python probe/probe.py --mode smoke --gpu L4 --id probe-smoke-5 --volume
.venv/bin/python probe/probe.py --mode image --gpu L40S \
  --prompt "A red ceramic teapot on a wooden table beside a window, soft daylight, photographic" \
  --steps 25 --cfg 1.0 --size 1024 --seed 0 --sampler euler --scheduler simple \
  --model-revision 440590060921f09fabfe963e31392a6b91f46ca7 --volume
```

## 实测数字

| 指标 | 值 | 说明 |
| --- | --- | --- |
| `spawn` 往返 | 0.4–0.5s | 返回 `call_id`，同步、几乎无延迟 |
| spawn → 第一条日志 | 2.7–3.5s | 图片任务；容器已就绪（权重命中缓存） |
| 端到端（单张 1024×1024，25 步） | **49.9s** | 含采样、VAE 解码、写入 Volume、读回清单 |
| 容器内 `elapsedSeconds` | 40.3s | 与端到端的差值即队列与网络开销 |
| 采样速率 | 1.76 it/s | 取自容器 `serverLogTail` 的 tqdm 输出 |
| 产物 | 1,505,721 B PNG，RGBA 1024×1024 | sha256 与清单一致，已在本地校验通过 |
| Volume 读吞吐 | 1.44 MiB 用时 0.7–2.5s，约 **1.4–2.2 MiB/s** | 三次读数，稳定在 2 MiB/s 量级 |
| 该次费用量级 | **约 $0.033** | L40S ≈ $0.000665/s × 50s，按现价粗算 |

参考实现里 `qwen_image.py` 的 `ESTIMATE_USD = 0.35` 比实测高约一个数量级；那是**预留上限**，不是成本依据。

## 影响设计的三条事实

### 1. `FunctionCall.logs` 有硬性版本下限：Modal ≥ 1.5.5

workstation 的 system python 是 `modal 1.3.3`，该版本**没有** `FunctionCall.logs`（已实测）。实时进度通道要求 `>=1.5.5`，因此 aladin 必须钉死版本并在 `pyproject.toml` 写明原因，不能依赖系统 Python。

### 2. 函数返回值**不可依赖**——产物一律以 Volume 为准

参考实现两条链路的返回值都**间歇性**无法在客户端反序列化，报
`DeserializationError: ... the 'torch' module is not available in the local environment`。
实测 6 次 smoke 中 2 次成功、4 次失败；图片任务首次亦失败。而 **Volume 里的 `result.json` 与 PNG 始终完整**。

结论不是"容器有 bug"，而是**架构约束**：返回值里可能含 torch 类型，本地要装 torch 才能解。因此

- 容器应只返回纯 JSON 原语，产物走 Volume；
- 消费端不应以函数返回值为权威，应读 Volume（本条同时解释了 `gpu-smoke` 为何**不能**用作廉价预检）；
- 若本地"装个 torch"就能绕过，那会直接毁掉"本地依赖面尽量小"这个前提。

### 3. Volume 拉取吞吐约 2 MiB/s——拉取模型的量化约束

1.44 MiB 的图片 <1s，完全无感。按同一速率外推：

- 500 MB 视频 ≈ 4 分钟
- 2 GB 视频 ≈ 17 分钟

异步后台传输可以接受，但**这是图片与视频分界的量化依据**。图片阶段无需任何优化。

## 请求协议（构造合法请求必须满足）

`worker.execute()` / `image_worker.validate()` 会逐项校验，任一项不符即拒绝：

- `workerRevision` 必须等于容器内 `worker.py` 自身的 sha256
- `resources` 必须与参考实现的 `profile(gpu)` **逐字段相等**（含注入的 `timeout=600`）
- `key` 必须是 64 位 hex；`inputSha256` 必须与传入字节一致
- `modelRevision` / `comfyRevision` / `ggufRevision` 必须是钉死的 40 位 hex
- `modal_app.py` 的 `invoke(request, source)` 需要**两个位置参数**；`modal_image_app.py` 的 `generate(request)` 只要一个

`probe/modal_reference.py` 从参考实现**解析**这些值（`ast.literal_eval`，非手抄），且已实测 8 档 GPU 的 `resources` 与参考 `profile()` 完全相等。

## 本次未能测量的项

- **权重冷启动（需下载 14.63 GB）**：本次全部命中 Volume 缓存，未触发下载。首次部署时 `ensure_weights()` 要从 HuggingFace 拉取，耗时未知。这是目前最大的未测量项。
- **热容器复用**：未测连续调用是否复用容器、复用后耗时下降多少。
- **多图并发**：仅单请求单图。
