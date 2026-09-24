# 部署与更新（工作站）

这是运维手册：代码改完之后怎么推上线。架构取舍见 `docs/adr/0002`。

## 现状

| 项 | 值 |
| --- | --- |
| 机器 | `.env` 的 `ALADIN_HOST`（Ubuntu，x86_64），SSH 免密已配。下文 `<workstation>` 都指它 |
| 代码目录 | `~/docker/aladin/`（compose 项目目录） |
| 入口 | `http://<工作站 Tailscale IP>:8765`（即工作站 `.env` 的 `BIND_ADDR`） |
| 产物 | `~/docker/aladin/data/`（`jobs/` 与 `gallery/`） |
| 状态库 | Postgres，named volume `aladin_pgdata`（**不在** `data/` 里） |
| 凭据 | 远端 `~/.modal.toml`，只读挂进 worker 的 `/etc/aladin/modal.toml` |

本机 `~/Workspace/other/aladin` 只是**开发副本**，改它不影响线上，必须同步过去。

## 一条命令更新

```sh
tools/deploy.sh
```

部署目标从仓库根目录的 `.env` 读取（不入库，模板见 `.env.example`）：`ALADIN_HOST` 是 SSH 主机名，
`ALADIN_REMOTE_DIR` 是远端 compose 目录（默认 `~/docker/aladin`）。环境变量优先于 `.env`。

脚本做四件事：校验目标目录、rsync、按需重建、打印状态。三个细节：

- **目标校验**：先确认远端那个目录确实是本项目的 compose 目录，否则中止——rsync 带 `--delete`，指错目录会删掉别人的文件。
- **按需重建**：即使同步没有变化，也重建 api/worker；Docker 会复用构建缓存，避免上次中断留下旧容器。
- **worker 提醒**：这次同步如果动到了 `aladin/worker.py` 或 `aladin_modal_app.py`，
  会显式提醒你补 `modal deploy`；脚本不替你跑，因为那需要 Modal 凭据。

脚本**不接受参数**（它始终重建镜像），传了会直接报错退出。

## 手动流程（脚本做的事）

```sh
# 1) 同步。--delete 让远端和本地一致。
#    .env 及其备份（.env.*）是各机器自己的配置：被排除的文件 rsync 既不覆盖也不删除。
rsync -az --delete \
  --exclude .venv --exclude .cache --exclude .docker --exclude data \
  --exclude __pycache__ --exclude *.pyc \
  --exclude probe/artifacts --exclude probe/report-*.json \
  --include .env.example --exclude ".env*" \
  ./ <workstation>:~/docker/aladin/

# 2) 重建镜像并重启
ssh <workstation> "cd ~/docker/aladin && docker compose up -d --build --force-recreate"

# 3) 确认
curl -s -o /dev/null -w "%{http_code}\n" http://<workstation-ip>:8765/
```

两个参数都不能省：

- `--build`：应用代码是 `COPY` 进镜像的。只 `--force-recreate` 会用**旧镜像**重开容器，
  改了 `aladin/` 下任何文件都不会生效。
- `--force-recreate`：镜像标签固定是 `aladin-api:latest` / `aladin-worker:latest`，
  compose 有时会认为镜像没变而不重开容器。

脚本默认**只重建 `api` 和 `worker`**：postgres 跑的是同一个镜像里的数据库服务，
重建它不会带来任何代码更新，却会让连接断掉二十来秒。只有 `docker-compose.yml`
或 `.env.example` 本身变了，才会连 postgres 一起重建。

依赖层有 Docker 缓存，重建通常只要几秒。
## 三个容易踩错的点

### 1. 改了 `aladin/worker.py` 或 `aladin_modal_app.py`，必须先 `modal deploy`

```sh
# 在任意一台有 modal CLI 的机器上（本机即可）
uv --cache-dir .cache/uv run modal deploy aladin_modal_app.py
```

**顺序很重要。** Modal 侧和本地都按 `worker.py` 的 sha256 校验版本，不一致时容器会直接拒绝任务，
报 `Worker revision mismatch`（错误信息里带两侧哈希，一眼能看出哪边旧）。忘记这一步的表现是
**所有任务瞬间失败**，但**不花 GPU 钱**——校验发生在最前面。所以它是安全失败，不是隐性错误。

`worker.py` 是生成文件，真正要改的是 `tools/build_worker.py`：

```sh
uv --cache-dir .cache/uv run python tools/build_worker.py   # 重新生成 worker.py
uv --cache-dir .cache/uv run modal deploy aladin_modal_app.py
tools/deploy.sh
```

### 2. 只 deploy 了 Modal、没同步本地 —— 同样 revision mismatch

两边都要更新。`tools/deploy.sh` 之所以不跑 `modal deploy`，是因为它需要 Modal 凭据，
而且大多数改动（web、UI、pipeline）不涉及容器侧；改了 worker 就手动补一次。

### 3. 改了 `aladin/` 下任何文件 —— 都要重建镜像（脚本已处理）

因为代码是 `COPY` 进镜像的，容器只是镜像的一次运行。`tools/deploy.sh` 现在**始终**带 `--build`，
所以正常不用管；手动执行 compose 命令时别忘了它。

加新依赖走同一条路：本地先 `uv sync`（更新 `uv.lock`），再 `tools/deploy.sh`；
依赖那一层会因为 `pyproject.toml`/`uv.lock` 变化而自动重建。
## 数据库迁移

`migrations/*.sql` 按文件名排序，api 和 worker 启动时都会各跑一次，
用 Postgres advisory lock 串行化，重复执行是安全的。

加迁移就是新增一个 `migrations/0002_xxx.sql`，然后走正常更新流程，不需要手动执行任何命令。

**注意**：迁移只前进不回退。改 schema 前想清楚，尤其是删列。

## 回滚

代码回滚 = 把本地 checkout 切到旧版本，再 `tools/deploy.sh`。

数据库**不会**跟着回滚：如果新版本只是加了列/表，回滚到旧代码仍能跑（旧代码忽略新列）；
但如果新版本**删了**列，回滚就会失败。所以迁移里尽量避免删列。

## 排障

```sh
# 实时日志
ssh <workstation> "cd ~/docker/aladin && docker compose logs -f worker"
ssh <workstation> "cd ~/docker/aladin && docker compose logs --tail 50 api"

# 任务状态
ssh <workstation> 'docker exec aladin-postgres-1 psql -U aladin -d aladin -c "SELECT state, attempts, left(call_id,14), left(last_error,60) FROM jobs ORDER BY created_at DESC LIMIT 10"'

# 事件流（进度、重试、失败原因都在这里）
ssh <workstation> 'docker exec aladin-postgres-1 psql -U aladin -d aladin -c "SELECT kind, left(message,70) FROM job_events WHERE job_id=(SELECT id FROM jobs ORDER BY created_at DESC LIMIT 1) ORDER BY id"'

# 容器内的 worker 与本地是否同版本
uv --cache-dir .cache/uv run python -c "import modal; print(modal.Function.from_name('aladin-image-v1', 'worker_probe').remote())"
```

状态含义见 README 的「任务状态」一节。任务卡在 `unknown` 不用手改库：
worker 每 30 秒检查一次 Volume 回执——有回执就直接恢复，没有且还有尝试次数就重投。

## 数据

```sh
# 产物就是宿主机上的普通文件，直接看/拷/删
ssh <workstation> "ls -la ~/docker/aladin/data/gallery | head"

# 清空生成历史但保留图库
ssh <workstation> "rm -rf ~/docker/aladin/data/jobs/*"

# 彻底重来（删掉所有任务状态与图库副本，不可恢复）
ssh <workstation> "cd ~/docker/aladin && docker compose down -v && docker compose up -d"
```

`docker compose down` 不加 `-v` 不会删 Postgres 数据；加了 `-v` 会删 named volume。

**产物属主**：Linux 上容器以 `.env` 里的 `ALADIN_UID`/`ALADIN_GID` 写文件（当前是 1000），
所以宿主用户能直接管理。哪天发现产物变成 root 属主，说明这两个值从 `.env` 里丢了。

## Planner / 一句话出图

新增链路涉及三个部署面：`aladin_planner_modal_app.py`（规划）、
`aladin_modal_app.py`（逐图参数协议）、workstation api/worker（页面与任务编排）。
先通过 CPU `planner_probe` 验证镜像构建，再做真实规划；之后依次：

```sh
.venv/bin/modal deploy aladin_planner_modal_app.py
.venv/bin/modal deploy aladin_modal_app.py
tools/deploy.sh
```

Planner 的版本指纹覆盖 app、planner_schema 和 settings；改动其中任一文件必须同步部署。
`min_containers=0`，没有保温 GPU。生成结果回 workstation，规划回执放在独立
`aladin-planner-results-v1` Volume 中作恢复中转，模型 Volume 只存缓存。
Docker 必须包含 `request.py`、`video_request.py` 与 planner app 源文件；部署脚本会检查导入。

## Anima / Pony 文生图

模型目录与版本在 `aladin/image_models.py`。两个 app 各有独立模型 Volume，
共用 `aladin-image-results-v1` 结果回执协议；GPU 均按需启动（min=0）。
先部署 Modal，再部署 workstation：

```sh
ALADIN_EXTRA_IMAGE_MODEL=anima-base-1.0 .venv/bin/modal deploy aladin_extra_image_modal_app.py
ALADIN_EXTRA_IMAGE_MODEL=pony-realism-2.2 .venv/bin/modal deploy aladin_extra_image_modal_app.py
bash tools/deploy.sh
```

可先调用各 app 的 `prepare_models`，在 CPU 容器中下载并校验固定版本的 SHA256。
`extra_image_worker.py`、`extra_image_request.py`、`image_models.py`、通用传输 `worker.py`
任一变化，均需重新部署这两个 app；请求指纹会拒绝本地和 Modal 的版本漂移。
Qwen 仍由原有 app 执行。新增选项仅用于文生图；图生图、指令编辑与 director 保持原模型。
