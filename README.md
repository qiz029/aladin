# Aladin

把 Modal 上的生成能力封装成个人软件。部署在自己的 workstation 上，形态接近自托管的生成应用：
本地入口 + 本地产物库，Modal 只负责算。

## 一句话出图

`/apps/director` 只填需求，Qwen planner 在 Modal 上输出受 JSON Schema 约束的计划，
再经参数校验自动交给 Qwen-Image Q8 逐张生成。张数默认由需求决定（1–8 张）。
任务页保留原始需求、规划理由、所有 prompt/negative、尺寸、步数、CFG、种子及模型版本；
「用这套参数微调」回填生图表单；打包下载含图片和完整 `generation.json`。

Agent：`POST /api/v1/director`，JSON `{"brief":"海边咖啡馆的两张宣传照片"}`，
可选 `count` 指定 1–8 张。异步返回与 `wait=true` 用法沿用图片接口。
`GET /api/v1/jobs/{id}` 包含 `stage`、`plan`、`generation_request` 和逐产物 `prompt/params`。
规划和生图分阶段持久化；已知 call_id 只接管，规划回执可在进程重启后恢复。

页面上可以**选目标模型**（Qwen / Pony / Anima）和**整批共用的 LoRA**：planner 按目标模型的写法写提示词
（Qwen 自然语言；Pony `score_9…` 标签并写负向词；Anima Danbooru 标签），尺寸与步数/CFG 落在该模型的范围内，
LoRA 的说明会告诉 planner 以便配合，触发词自动补。勾选「先看计划再生成」时，规划完停在「等待确认」：
可以逐张改提示词、尺寸、步数、CFG、种子或删掉几张，确认后才生成（API：`POST /api/v1/jobs/{id}/approve`）。

可选填写「必须保持 / 允许变化 / 本轮只改」，每项最多 1000 字符。API 使用
`creative_spec: {"must_keep":"蓝色外套", "may_change":"背景", "change_only":"傍晚暖光"}`。
要求随任务和规划请求保存，参与幂等键；planner 在每张的提示词与规划说明里落实要求。
这是文本规划约束，不保证生成图的视觉一致性；任务页展示原要求，候选比较页可以分别标记符合 / 不符合 / 不确定。

任务图片、图片预览和有来源任务的收藏提供「用相同设置创作」：恢复该张图的模型、提示词、负向词、
尺寸、步数、CFG、采样器、调度器、种子、尺度和 LoRA，默认生成 1 张；改图恢复原始输入图、模式、
重绘幅度与修复选区。只是回填表单，点击不会提交 GPU 任务。原输入图或模型 / LoRA 不可用时明确报错。
Agent 可用 `GET /api/v1/jobs/{id}/artifacts/{name}/reuse` 读取相同设置；收藏的原任务删除后不能再恢复设置。

「创作方式」选**日式漫画（一页多格）**（API `preset: "manga"`）时，planner 先写故事梗概与人物固定外貌，
再按一页 4–8 格的版式（右上起、右→左阅读）逐格分镜：每格有节拍、镜头、情节、旁白与对白，
尺度逐格递进且不超过所选上限，尺寸由格子形状决定。对白与旁白目前只保存在计划里；拼页和对白框排版尚未实现。

## 状态

已端到端跑通四条链路：文生图（Qwen-Image 2.1 / Anima / Pony Realism）、改图（图生图、指令编辑、
局部修复）、图生视频（LTX-2.5 / LTX-2.3）、一句话出图（Qwen planner → 生图）。
每条都是 提交 → Modal 上生成 → 采样步级实时进度 → 产物回本地 → 收藏。

## 快速开始

本机调试（Postgres 用 compose 起，服务直接跑在宿主上）：

```sh
docker compose up -d postgres                             # 发布在 127.0.0.1:5433
uv --cache-dir .cache/uv sync
uv --cache-dir .cache/uv run python -m aladin            # api + worker 同进程，http://127.0.0.1:8765
```

整套容器化运行：`docker compose up -d --build`（拓扑见下文「架构」与 `docs/deploy.md`）。

依赖 Modal 凭据（走它自己的 `~/.modal.toml`，不需要写进 `.env`）。
首次使用需先部署 Modal app：

```sh
uv --cache-dir .cache/uv run modal deploy aladin_modal_app.py            # 生图 / 改图
uv --cache-dir .cache/uv run modal deploy aladin_extra_image_modal_app.py  # Anima / Pony
ALADIN_VIDEO_MODEL=ltx-2.5 uv --cache-dir .cache/uv run modal deploy aladin_video_modal_app.py  # 图生视频 LTX-2.5
ALADIN_VIDEO_MODEL=ltx-2.3 uv --cache-dir .cache/uv run modal deploy aladin_video_modal_app.py  # 图生视频 LTX-2.3
uv --cache-dir .cache/uv run modal deploy aladin_planner_modal_app.py    # 一句话出图的规划
```

LTX-2.5 的 HF 仓库要先在网页上点「Agree and Access」，再建 Modal secret：
`modal secret create aladin-hf HF_TOKEN=<token>`（token 只放在 Modal 里）。
视频权重先用 CPU 预下载，免得 GPU 冷启动时为下载付费：
`ALADIN_VIDEO_MODEL=ltx-2.5 uv --cache-dir .cache/uv run modal run aladin_video_modal_app.py::prepare_models`（2.3 同理）。

## 使用

1. `/` 选应用：「生成图片」文生图，「改图」上传一张图后做变体或下指令编辑。
2. `/apps/image` 填提示词、张数、尺寸，开始生成。
   主界面只有这三项，其余参数收在「高级参数」里。图库与任务历史在顶部导航各自的页面。
3. `/apps/edit` 上传输入图（PNG/JPEG，≤8 MiB）后选模式：
   - **图生图**：同一个模型以这张图为起点重画，提示词可留空，「重绘幅度」控制改动大小。
   - **指令编辑**：Qwen-Image-Edit-2509 按提示词改图，提示词必填。
4. 生成过程实时显示「第 i/n 张 · 采样 s/m 步」，进度来自容器内 ComfyUI 的 WebSocket。
5. 完成后可下载单张或打包下载；「加入图库」会把图片连同 prompt 与 seed 一起保存。

## 给 agent 用的 API

页面和 API 是同一套服务：共用校验（`aladin/params.py`）与提交路径（`pipeline.enqueue`），
所以页面上能做的，API 都能做，没有第二份业务逻辑。

交互式文档：`/docs`（Swagger UI）与 `/openapi.json`。能力自描述：`GET /api/v1`。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/v1` | 能力清单、可用端点、注意事项 |
| GET | `/api/v1/params` | 参数边界、尺寸预设、默认值——照它构造请求即可 |
| POST | `/api/v1/director` | 一句话出图，**JSON**（`brief`，可选 `count`） |
| POST | `/api/v1/images` | 文生图，**JSON** |
| POST | `/api/v1/edits` | 改图，**multipart**（字段名 `file`） |
| POST | `/api/v1/edits/base64` | 改图，**JSON**（`image_base64`） |
| POST | `/api/v1/videos` | 图生视频，**multipart**（起始图字段名 `file`） |
| POST | `/api/v1/videos/base64` | 图生视频，**JSON**（`image_base64`） |
| GET | `/api/v1/jobs` | 任务列表，可按 `state` / `mode` 过滤 |
| GET | `/api/v1/jobs/{id}` | 任务详情：状态、进度、产物、链接 |
| DELETE | `/api/v1/jobs/{id}` | 删除已结束的任务与本地产物（收藏副本保留；进行中返回 409） |
| GET | `/api/v1/jobs/{id}/events` | 事件流（进度历史的原始记录） |
| GET | `/api/v1/jobs/{id}/artifacts/{name}` | 产物字节 |
| GET | `/api/v1/jobs/{id}/input` | 输入图（改图任务） |
| GET/POST/PATCH/DELETE | `/api/v1/gallery` … | 收藏的检索（q/tag/model/kind/min_stars/rating/sort）、加入、标签与星级、移除 |
| GET | `/api/v1/billing` | Modal 账单快照：本期开销、credits 抵扣、余额估算（worker 每半小时刷新） |

三个为 agent 做的取舍：

1. **提交是异步的，但可以同步等。** 默认立即返回 `202` + 任务对象；加 `?wait=true&timeout=300`
   则阻塞到终态（或超时）再返回。实测一次文生图 90 秒、一次 base64 改图 68 秒。
2. **撞车不是错误。** 省略 `seed` 时每次随机，不会撞车；显式给了相同种子与参数时，重复提交返回 `200`（新建的是 `202`），body 里是**已存在任务**的
   完整对象和 `created: false`。agent 拿到 `id` 直接继续用，不必解析错误信息。
3. **参数错误是一份清单。** 校验失败返回 `422`，`detail` 是字符串数组，每条一个原因。

```sh
# 一次调用拿到成品
curl -s -X POST "$BASE/api/v1/images?wait=true" -H 'Content-Type: application/json' \
  -d '{"prompt":"a brass compass on aged paper","size":"square","steps":20}' | jq .artifacts

# 改图（JSON + base64，适合手上已有字节的场景）
curl -s -X POST "$BASE/api/v1/edits/base64?wait=true" -H 'Content-Type: application/json' \
  -d "{\"image_base64\":\"$(base64 -i in.png)\",\"mode\":\"edit\",\"prompt\":\"make it night\"}"
```

**API 不鉴权**，和页面一样，只应部署在 tailnet 内。

仓库里带了一个 skill，让 agent 不用手拼 curl：

```
.agents/skills/aladin/SKILL.md          调用约定、命令表、注意事项
.agents/skills/aladin/scripts/aladin.py 薄 CLI（只用标准库）
```

在本仓库里工作的 agent 会自动发现它。想在**其他项目**里也能用，把 skill 目录
软链到上层目录即可（祖先目录的 `.agents/skills` 同样会被扫描）：

```sh
ln -s "$PWD/.agents/skills/aladin" ~/Workspace/.agents/skills/aladin
```

```sh
python3 .agents/skills/aladin/scripts/aladin.py submit "a brass compass" --wait
```



## 架构

```
workstation（docker compose）                                 Modal
─────────────────────────────                                 ─────
api（页面 / JSON API / SSE）──► Postgres（账本）◄── worker ── spawn / 轮询 ──► 各 app 的容器
      │   只插 pending 行        LISTEN/NOTIFY ▲       │    （ComfyUI + worker.py，进度走 stdout）
      └────────── ./data（产物、收藏、输入图）◄─────────┘ ◄── 结果 Volume（完成后拉回本地）
```

- **本地是权威**：账本、幂等键、产物都在本机；Modal Volume 只做模型缓存 + 传输中转。
- **只有 worker 持有 Modal 凭据、只有 worker 提交**。api 只往 `jobs` 插一行 `pending`（ADR-0002）。
- **进度走日志流**，产物走 Volume 拉取，两条独立通道，不需要任何入站端口。
- worker 写事件后 `NOTIFY`，api 的 SSE 被唤醒后自己读表；通知丢了有 30s 兜底。

目录：

| 路径 | 作用 |
| --- | --- |
| `aladin/` | 本地服务：`web.py`（页面）、`api.py`（JSON API）、`pipeline.py`（编排）、`db.py`（账本）、`params.py`（共用校验）、`cleanup.py`（删除与清理） |
| `aladin/worker.py` | 容器内执行体。**由 `tools/build_worker.py` 生成，不要手改** |
| `aladin/video_worker.py` / `extra_image_worker.py` | 视频、Anima/Pony 的容器内执行体 |
| `aladin_*modal_app.py` | 各 Modal app 定义与镜像（按模型隔离） |
| `request.py` / `video_request.py` | 请求构造、幂等键与 `workerRevision`（本地与容器共用） |
| `migrations/` | Postgres schema，启动时自动应用 |
| `probe/` | 验证用的探针与单测，见 `docs/probes/` |
| `data/` | 本地产物（不入库） |

## 数据布局

账本在 Postgres（compose 的 named volume `pgdata`），文件全部在本地 `data/`
（`ALADIN_DATA` 可覆盖），**不入库**：

```
data/
├── jobs/<job_id>/image-01.png    生成历史：可清理
├── uploads/<sha256>.png          改图 / 视频的输入图，按内容寻址
└── gallery/<sha256前16>.png      收藏：独立副本，永久保留
```

| 表 | 内容 |
| --- | --- |
| `jobs` | 一次生成任务：prompt、全部参数、请求体、`call_id`、状态、租约、重试次数 |
| `job_events` | 该任务的事件流（提交/入队/采样步/完成/失败），进度就存在这里 |
| `artifacts` | 产物清单：文件名、sha256、字节数、seed、prompt、逐图参数、人工评审 |
| `gallery` | 收藏条目：副本路径、prompt、seed、sha256、标签、星级、来源模型/模式/尺度 |
| `remote_purge` | 已删除任务在 Modal 结果 Volume 上待删的目录 |
| `billing_snapshot` | Modal 账单快照（worker 定期刷新） |

**收藏存的是复制出来的独立文件**，不是指向 `data/jobs/` 的引用。所以删除任务
不会弄丢收藏里的图；从收藏移除只删除收藏那份副本。

## 任务状态

```
pending → submitting → submitted → running → succeeded
                                          └→ failed
          （任一提交/轮询阶段）───────────→ unknown → 先查 Volume 回执，再决定重试或失败
```

- api 只写 `pending`；worker 认领后 spawn，记下 Modal `call_id`。**有 `call_id` 就只接管、绝不重新 spawn。**
- 容器内的采样进度以事件形式写进 `job_events`，页面通过 SSE（`/jobs/{id}/stream`）订阅，
  也可用 `/jobs/{id}/state?after=N` 轮询增量。
- 本机到 Modal 的连接抖动只会推迟下一次轮询，不会把任务判为失败；容器内抛出的异常才是 `failed`。
- **逐张回传**（Qwen 生图/改图）：批量里每张图的 SaveImage 一完成，容器就写 Volume、提交，并发一条
  `artifact` 进度；worker 校验 sha256 后先把这一张拉回本地，任务页随即显示。整批结束后仍以
  `result.json` 为准完整拉取一遍，逐张回传失败不影响任务。ComfyUI 不按序号顺序采样（实测 2 → 3 → 1），
  所以进度按「已采完几张」计算。Anima/Pony 与视频仍是整批结束后回传。
- worker 崩溃或重启后，租约过期的任务会被自动接管；`unknown` 最多重试 `max_attempts`（默认 2）次。
- 完整状态机与不变量见 `docs/adr/0002-postgres-and-docker-topology.md`。

## 生成参数

| 参数 | 范围 | 默认 |
| --- | --- | --- |
| 提示词 | 1–2000 字符 | — |
| 张数 | 1–8（同一 prompt，seed 递增） | 1 |
| 尺寸 | 方形 1024×1024 / 竖版 768×1152 / 横版 1152×768 | 方形 |
| 步数 | 1–40 | 25 |
| CFG | 0–10 | **1.0** |
| 种子 | 0 – 2⁶³-1 | **随机**（提交时取定，记进任务） |
| 采样器 | `euler` `euler_ancestral` `dpmpp_2m` `dpmpp_2m_sde` | `euler` |
| 调度器 | `simple` `normal` `beta` | `simple` |

CFG 默认 1.0 是刻意的：Qwen-Image 是低 CFG 模型，套用 SD 习惯的 7–8 会出问题。

`/apps/edit` 另有两个参数：

| 参数 | 范围 | 默认 |
| --- | --- | --- |
| 模式 | `img2img` 图生图 / `edit` 指令编辑 | `img2img` |
| 重绘幅度 | 0.05–1（仅图生图，越大改动越多） | 0.6 |

改图**没有尺寸选项**：输出尺寸跟输入图走。容器用 `FluxKontextImageScale` 把输入吸附到
一个受支持的档位（实测 768×1152 的输入输出 832×1248，宽高比保持一致）。

## 测试

测试连 compose 发布在 `127.0.0.1:5433` 的 Postgres，自动建独立的 `aladin_test` 库：

```sh
docker compose up -d postgres
uv --cache-dir .cache/uv sync --group dev
uv --cache-dir .cache/uv run python -m unittest discover -s aladin -p 'test_*.py' -t .
uv --cache-dir .cache/uv run python -m unittest probe.test_watchdog
```

## 删除与清理

- 任务页「删除任务」或 `DELETE /api/v1/jobs/{id}`：删账本行、`data/jobs/<id>/`，以及不再被
  其他任务引用的输入图。只能删已结束的任务；收藏副本保留。
- 批量清理：`python -m aladin cleanup --days 30 [--dry-run]`（容器里：
  `docker compose exec worker python -m aladin cleanup --days 30`）。带人工评审的任务不会被自动清理。
- **Modal 结果 Volume 里的副本不会被删**（api 不持有 Modal 凭据）。同参数再提交会直接命中那里的回执。

## 生成耗时

每个成功任务的 `jobs.timings` 记着容器内的分段（权重检查、ComfyUI 启动、按节点归类的加载/采样/解码、
写产物、是否复用热容器）和宿主侧的发现完成与下载耗时；提交、容器开始/结束等时刻在 `job_events` 里。

- 报表：`docker compose exec worker python -m aladin timings [--since 2026-09-25] [--app image] [--limit 50]`，
  逐任务列分段，末尾按 模型 × 冷热 给中位数/p90。各列含义见 `aladin/timings.py` 顶部。
- 老任务只有 Volume 回执里的总耗时：`python -m aladin timings --backfill [--dry-run]` 一次性补上（需 Modal 凭据，在 worker 容器里跑）。

## 已知限制

- 同参数（含显式种子）重复提交会被唯一约束拦住，这是有意的幂等保护；不填种子则每次随机。
- 生成历史不会自动清理，需要定期跑 `cleanup`。
- 服务不鉴权：API 只应部署在 tailnet 内，网页由 Cloudflare Access 保护。

## 部署到服务器

完整运维手册（更新、回滚、排障）：**`docs/deploy.md`**。下面是要点。

这套东西可以直接跑在另一台 Linux 机器上（`.env` 里的 `ALADIN_HOST`），项目放在
`~/docker/<project>/`。机器相关的差异全部走 `.env`（已 gitignore）：

| 变量 | 作用 | 默认 |
| --- | --- | --- |
| `BIND_ADDR` | api 发布在哪个地址。默认只回环；设为该机 IP 或 Tailscale IP 才对外可达 | `127.0.0.1` |
| `POSTGRES_HOST_PORT` | Postgres 映射到宿主哪个端口（仅供宿主上测试/排查） | `5433` |
| `ALADIN_UID` / `ALADIN_GID` | 容器以谁的身份写产物 | `0`（root） |

两个必须注意的地方：

1. **Linux 上必须把 `ALADIN_UID`/`ALADIN_GID` 设成宿主用户**（例如 1000），否则产物属主是
   root，宿主用户连删都删不掉。macOS 的 Docker Desktop 会把属主映射成宿主用户，
   所以默认 root 在 Mac 上没问题。
2. **worker 需要 Modal 凭据**，只读挂在 `/etc/aladin/modal.toml`。特意不用 `/root/` 下的
   路径，是为了让以非 root 身份运行的容器也能读到。目标机上没有的话：
   `scp ~/.modal.toml <host>:~/.modal.toml` 然后 `chmod 600`。

改完 `.env` 后 `docker compose up -d --force-recreate`；改了 `aladin/worker.py` 或
`aladin_modal_app.py` 之后，记得在任意一台有 `modal` CLI 的机器上重新 `modal deploy`。

## 人物质量工具

人物测试场景、候选并排比较、人工评审与矩形区域修复见 [使用说明](docs/people-quality.md)。
本版提供评测与修复流程；模型质量提升仍需真实生成样本和人工审图验证。

## 尺度与自动标签

生成表单和 API 都有「尺度」：日常 / 暗示 / 露骨（`rating`: general / suggestive / explicit），
默认值由 `ALADIN_DEFAULT_RATING` 决定（默认 explicit）。同一个尺度在不同模型下换成它自己认的词：

- **Anima**：Danbooru 系分级词 safe / sensitive / explicit，按模型卡约定放在提示词**开头**。
- **Pony Realism**：Pony V6 的 rating_safe / rating_questionable / rating_explicit，放开头。
- **Qwen-Image 与视频**：文本编码器是 LLM，分级标签作用有限，只在结尾补少量直白词；日常尺度不补。

词表在 `aladin/prompt_defaults.py` 的 `FAMILIES`，是起点不是定论，按实际出图效果调整；
改了只影响新任务（每个任务把实际标签冻结在 `params.prompt_defaults`）。尺度参与幂等键。
一句话出图在提交时按 Qwen 词表冻结标签，规划完成后补到每张图的提示词上。

## 隐私

- **产物不带创作信息**：ComfyUI 默认把整个 workflow（含提示词原文）写进 PNG 文本块和 WebM 的 Tags。
  容器现在以 `--disable-metadata` 启动；worker 拉回本地时再无损剥一次（`aladin/metadata.py`）。
  之前生成的文件用 `python -m aladin strip-metadata [--dry-run]` 一次性清理（会同步更新账本的 sha256）。
- **缩略图默认模糊**：顶栏「隐私模式」开关，记在本浏览器；悬停、键盘聚焦或点开大图时显示。
- **不进共享缓存**：除 `/static` 外所有响应带 `Cache-Control: private, no-cache`，CDN 不会在边缘节点存图。
- **删除即清理远端**：删除任务时登记到 `remote_purge`，worker 每分钟删掉 Modal 结果 Volume 上对应目录。

## LoRA

生图的 Pony / Anima 与视频的 LTX-2.3 / LTX-2.5 支持叠加 LoRA（页面选中这些模型时出现 LoRA 面板；
API 是 `loras` 字段）。LoRA 按底模分族，跨族会被挡住。

- **目录**：`aladin/loras.py`。每项钉死 Civitai 的版本 ID（或 HF 仓库提交）与 sha256，带默认强度、区间、触发词、互斥组、建议采样参数，
  以及内置预设。页面上还可以把当前组合存成自己的预设（存在浏览器里）。
- **同步**：`python -m aladin loras-sync [--only ID ...] [--dry-run]` 在本机下载、校验 sha256、上传到
  对应底模的模型 Volume（`loras/civitai-<版本>.safetensors`）。Civitai API key 放在 `.env` 的 `CIVITAI_API_KEY`，
  只在本机使用，不进 Modal。HF 来源的（Sulphur 2、官方 LTX LoRA）由视频 app 的 `fetch_lora`
  在容器里直接下载（`loras/hf-<sha 前 16 位>.safetensors`），大文件不经本机。
- **容器**：只收「文件 + sha256 + 强度」，在加载器之后串接 `LoraLoader`（视频是 `LoraLoaderModelOnly`，两段采样共用），首次使用时核对 sha256 并记标记。
  增删 LoRA、调默认值只改目录、再同步一次，**不用重新部署 Modal**。
- **触发词**自动补到提示词末尾，冻结进 `params.prompt_defaults.triggers`；LoRA 选择与强度参与幂等键。
- Qwen-Image 2.1 是新架构，旧 Qwen LoRA 不兼容（加载后静默无效），所以暂不支持。

## 收藏整理

收藏页可按提示词搜索，按类型、模型、尺度、星级、标签筛选，按时间或星级排序；
每张卡片上直接打星（再点一次清除）和增删标签。收藏时会记下来源模型 / 模式 / 尺度，删掉原任务也不丢。
API：`GET /api/v1/gallery`（同样的筛选参数）、`GET /api/v1/gallery/facets`、`PATCH /api/v1/gallery/{id}`。
