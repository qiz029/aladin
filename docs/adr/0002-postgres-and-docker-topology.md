# 0002 Postgres 状态存储与容器化拓扑

日期：2026-09-21
状态：**已接受**（2026-09-21 确认）
确认项：三容器划分（postgres + worker + api）；`unknown` 状态自动重试，`max_attempts` 默认 2

## 背景

现在的实现是单进程：`web.py` 和 `pipeline.py` 跑在同一个 Python 进程里，状态存 SQLite，
产物在 `data/`。这带来三个具体限制：

1. **状态同步靠轮询**：SSE 端点每秒查一次 `job_events`，每个浏览器连接一个轮询循环。
2. **任务生命周期没有持久保证**：进程一死，`running` 的任务只能靠启动时的 `reconcile()`
   一次性接管；没有重试策略、没有尝试次数、没有租约。
3. **web 与执行耦合**：想在另一个进程/容器里看状态或提交任务，就得共享同一个进程。

目标：把任务状态放进 Postgres，web 与执行拆成独立容器，产物仍留在 workstation 磁盘上，
并给出可证明安全的重试与轮询语义。

## 决策

### 1. 拓扑：三个容器，一个共享磁盘

```
                    workstation disk
                    ./data ────────────────┐
                       (bind mount)        │
   ┌───────────────┐   ┌───────────────┐   │
   │   postgres    │   │    worker     │───┤  提交 / 轮询 / 拉产物
   │  16-alpine    │◄──┤  aladin worker│   │  持有 Modal 凭据
   │  (named vol)  │   └───────────────┘   │
   └───────┬───────┘           ▲           │
           │ LISTEN/NOTIFY     │ 同一个镜像 │
           │                   │           │
   ┌───────▼───────┐   ┌───────┴───────┐   │
   │      api      │   │  只读挂载     │   │
   │    FastAPI    │   │  无 Modal 凭据│   │
   └───────────────┘───┴───────────────┴───┘
         页面 / SSE / 文件下载 / 加入图库
```

- **api 与 worker 是同一个镜像、不同命令**：一个镜像少一次构建、少一处版本漂移。
- **只有 worker 有 Modal 凭据**（`~/.modal.toml` 只读挂载）。api 不需要，也就不该有。
- **只有 worker 会提交任务**。api 只往 `jobs` 插一行 `pending` 就返回，避免两个提交入口
  （这是 0001 里就定下的规则，现在有了进程边界更要守住）。
- 两个容器都挂 `./data`：worker 写 `jobs/`，api 写 `gallery/`，各自读对方。

### 2. 状态机（这是重试安全的核心）

```
                 ┌──────────┐
   提交 ────────►│ pending  │ 无 call_id，重试安全
                 └────┬─────┘
                      │ 认领（lease）
                 ┌────▼──────┐
                 │ submitting│ 已认领，spawn 可能已发生 —— 唯一的不确定窗口
                 └────┬──────┘
                      │ 记下 call_id
                 ┌────▼──────┐
                 │ submitted │ ──► running（收到第一个进度事件）
                 └────┬──────┘
                      │
        ┌─────────────┼──────────────┐
        │             │              │
   ┌────▼────┐   ┌────▼─────┐   ┌───▼─────┐
   │succeeded│   │  failed  │   │ unknown │
   └─────────┘   └──────────┘   └────┬────┘
                                     │ 先查 Volume 回执，再决定重试或终止
                                     └──────► 重试回 pending（attempts+1）
```

三条不变量：

1. **有 `call_id` 就绝不重新 spawn**，只能 `FunctionCall.from_id(call_id)` 接管。
   重启、重试、人工干预都遵守这条。
2. **重试前必须先查 Volume 回执**。容器可能已经跑完、`result.json` 已在，
   只是调用记录过期了。查到就直接落地为 `succeeded`，不做 GPU 工作。
3. **`submitting` 是唯一的不确定窗口**（spawn 返回了但 `call_id` 还没写库就崩了）。
   无法消除，但可以界定：靠租约过期把它转成 `unknown`，再由不变量 2 兜底，
   最后用 `max_attempts` 封顶。这是整个设计里唯一"可能白跑一次容器"的地方。

### 3. 重试与轮询

worker 是一个循环，每轮做三件事（各自用 `FOR UPDATE SKIP LOCKED` 认领，互不阻塞）：

| 阶段 | 选中 | 动作 |
| --- | --- | --- |
| 提交 | `state='pending'` | spawn → 写 `call_id` → `submitted`，`next_poll_at=now()+15s` |
| 轮询 | `state IN ('submitted','running')` 且 `next_poll_at<=now()` | `get(timeout=0)`：有结果→拉产物→`succeeded`；`TimeoutError`→仍在跑，推迟下次；过期/不存在→转 `unknown` |
| 收尾 | `state='unknown'` | 查 Volume 回执；有→`succeeded`；无且 `attempts<max`→回 `pending`；否则→`failed` |

- 轮询间隔**退避**：15s → 30s → 60s，封顶 60s。任务通常在 1 分钟内结束，前几次会很快命中。
- `max_attempts` 默认 **2**。重试会烧 GPU，所以默认值保守。
- 租约：`lease_owner` + `lease_expires_at`（90s，每轮续租）。worker 崩溃后过期，
  其他 worker（或重启后的自己）可以接手。**接手 `submitted/running` 只重开日志流和轮询，
  不重新 spawn。**

### 4. 进度流与状态同步

- **进度事件写库即去重**：`job_events` 增加 `dedupe_key`，对 `(job_id, dedupe_key)` 建唯一索引。
  步级进度的 key 是 `step:<image>:<step>`。这样重开日志流导致的重放是无害的——
  这一点很关键，因为流断了就重连，重连是否重放旧行不由我们控制。
- **api 不再轮询**：一个后台线程 `LISTEN aladin_job`，收到通知后按 `job_id` 分发给
 进程内的 SSE 订阅者。SSE 事件带全局单调 `id`，浏览器断线重连用 `Last-Event-ID` 续传。
- 通知只做"唤醒"，**数据永远从表里读**：LISTEN/NOTIFY 不保证送达，但它丢失时
  SSE 仍会因心跳周期（30s）补一次增量查询。通知是加速，不是正确性依赖。

### 5. 表结构迁移

沿用 0001 的"产物层接口稳定、后端可换"思路：`db.py` 变成一个薄仓储层，SQL 从 SQLite 方言
改成 Postgres 方言。表基本保持，改动如下：

| 表 | 变化 |
| --- | --- |
| `jobs` | `status` → `state`（7 态）；新增 `attempts`、`max_attempts`、`lease_owner`、`lease_expires_at`、`next_poll_at`、`submitted_at`；`params_json`/`request_json` → `JSONB`；时间戳 → `TIMESTAMPTZ` |
| `job_events` | `seq` 改为全局 `BIGSERIAL id`（SSE 续传游标）；新增可空 `dedupe_key` + 唯一索引 |
| `artifacts` / `gallery` | 字段不变，类型改为 Postgres |
| `jobs_existing` | **删除**。`result_key`（= `storage_key`，已含 prompt 与全部参数）加上 `UNIQUE` 就等价于原来的唯一约束，一张表少一次 join |
| 新增 | `schema_migrations` |

**迁移方式**：编号 SQL 文件（`migrations/0001_init.sql` …），启动时由一个极小的
`migrate.py` 用 Postgres advisory lock 串行应用。不引 ORM、不引 alembic——
仓库现在没有 ORM 依赖，为 5 张表引一个迁移框架不划算。

**已有数据**：当前 `data/` 是我清理过的空目录，没有需要迁移的数据。

### 6. 产物与凭据

- `./data` 以 bind mount 进两个容器，路径不变（`jobs/<job_id>/`、`gallery/<sha256>.png`）。
  文件始终在 workstation 磁盘上，删掉容器不丢图。
- 容器以宿主 UID/GID 运行（`user: "${UID}:${GID}"`），避免 bind mount 上出现 root 属主文件。
- Modal 凭据走只读挂载 `~/.modal.toml:/home/aladin/.modal.toml:ro`；
  **不写进镜像、不写进 compose 的环境变量**。也支持 `MODAL_TOKEN_ID/SECRET`，但文件挂载更省事。
- postgres 数据用 named volume（`pgdata`），不进 `./data`——它的生命周期和产物不同。

### 7. 容器内额外要做的一件事：worker 回执短路

现在的 `worker.execute()` 无论如何都会跑一遍。要支持安全重试，必须让它在开工前先看
`/results/<key>/result.json`：若存在且 `request` 与本次一致，直接返回，不跑 GPU。
参考实现里有这段（`receipt.exists()`），我们漏了。**这是重试之所以便宜的前提。**

## 后果

- `web.py` 里的 SSE 每秒轮询删除，改成 LISTEN/NOTIFY 推送。
- `pipeline.py` 从"每个任务一个线程 + 一次性 reconcile"改成"无状态 worker 循环 + 租约"。
- 提交入口只剩 worker 一处；api 只写 `pending`。
- 本机开发需要 `docker compose up postgres` 才能跑 api/worker；
  或者用 `docker compose up` 起全套。

## 取舍与风险

1. **复杂度确实上升**：3 个容器 + 迁移 + 状态机 + 租约。对单用户工具这是实打实的代价，
   换来的是"多进程看同一份状态"和"可证明安全的可恢复执行"。如果后面发现只需要前者，
   这套也没白做；但如果连前者都不需要，就该退回 SQLite。**这是本 ADR 最需要你确认的一点。**
2. **macOS 上 bind mount 写大文件偏慢**（走 VM 转发）。图片（MB 级）无感；
   将来做视频（GB 级）时，拉取吞吐的瓶颈会从 2 MiB/s 的 Volume 读到本地磁盘写，
   这个组合需要届时实测。
3. **Modal 调用无法回滚**，所以"有 `call_id` 不重新 spawn"必须是硬规则，
   最好只有一处代码能改 `call_id`。
4. **Postgres 成了单点**：它挂了 api 和 worker 都不可用。用 healthcheck + `restart: unless-stopped`
   兜底，但这是新的失败模式。
5. **Docker Desktop 8GB 内存**：Postgres 空载约 100MB，不构成压力；
   但如果将来 worker 也在容器里跑重活就需要重新算。
6. **不做 SQLite 双后端**。`JSONB`、`SKIP LOCKED`、`LISTEN/NOTIFY` 都是 Postgres 特性，
   维护两套 SQL 方言的代价远大于收益。本机开发也用同一个 Postgres 容器。

## 实施顺序

1. `docker-compose.yml` + Dockerfile + `migrations/0001_init.sql` + `migrate.py`
   → 能 `docker compose up` 起 Postgres 并建表
2. `db.py` 改 Postgres 方言（保留现有函数签名，测试先跑通）
3. `worker` 循环：认领 / 提交 / 轮询 / 回执短路（含 `worker.py` 的 receipt 检查）
4. `api`：SSE 换 LISTEN/NOTIFY，其余路由不动
5. 端到端验证：冷启动、断流重连、杀 worker 后租约过期接管、Volume 有回执时重试不重跑 GPU

每一步都保持 19 个已有测试可跑；第 5 步是新测试。
