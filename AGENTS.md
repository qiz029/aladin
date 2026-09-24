# Aladin

把 Modal 上的生成能力封装成个人软件。第一个产品是 Qwen-Image 生图与图片存储；后续候选是自托管视频生成、LLM API 管理。

本文件是给 agent 的项目约定。它来自 agent-media-lab（默认与本仓库同级的 `../agent-media-lab`，路径可用 `.env` 的 `AGENT_MEDIA_LAB` 指定）的实测经验，那边已经跑通了 Modal GPU 后端、Qwen-Image 和 VLM 三条任务链。

## 架构约定

- **Modal 只做计算。** 容器是临时、按需启动的，不要把它当服务后端或持久层。Modal 支持部署 web endpoint，但容器生命周期由平台调度，对外产品要么保温容器（成本结构不同），要么异步排队。
- **产物落对象存储**（R2/S3）+ 元数据表；Modal Volume 只做模型缓存。agent-media-lab 现在的做法是"从 results Volume 读回、下载到本地 outputs/"，那是制作流水线的做法，不适用于要对外提供访问的平台。
- **镜像按模型/依赖隔离**，不追求统一镜像。agent-media-lab 里 Demucs、Seed-VC、captioner 最终各成一个镜像，因为统一镜像会被依赖冲突打碎。
- **一个能力一个纵向切片**：提交 → 排队 → 生成 → 存储 → 取回。端到端跑通再加下一个。
- **不要做统一 UI / 统一 API 网关**，直到第二个能力（视频）真实落地。抽象只抽"与模型无关、且已经复制 ≥2 次"的东西（任务、存储、计费、审计）；与模型相关或只有 1 个用例的部分保持分叉。
- **LLM API 管理独立立项。** 它与 GPU 生图只共用"用户/额度/审计"这一层，不共用技术栈。

## 命名

不要复用 agent-media-lab 已占用的 Modal 资源：

- App：`agent-media-lab-gpu-v1`、`agent-media-lab-image-v1`、`agent-media-lab-vlm-v2`
- Volume：`agent-media-lab-gpu-models-v1`、`agent-media-lab-gpu-results-v1`

## 成本与凭据

- 不把凭据、token、`.env` 内容写进代码、计划文件或日志。
- 示例金额只是占位，不是报价；按 Modal 实际计费核实后再对外说明。
- 冷启动是产品形态约束，不是实现细节：`min_containers=0` 下每个请求可能等分钟级。改产品形态前先实测一次真实链路的耗时。

## 对外接口

对外只有两个面：给人看的页面（`/`、`/apps/*`、`/jobs`、`/gallery`）和给 agent 用的
JSON API（`/api/v1`，文档在 `/docs`）。两者共用同一套校验（`aladin/params.py`）与
提交路径（`pipeline.enqueue`）——**加功能时两边要一起动，不要在 api 里另写一套逻辑**。

`aladin` skill（`.agents/skills/aladin/`）是对这个 API 的封装，改了接口要同步更新它。

## 参考

- `docs/adr/` 记录决策。
- agent-media-lab 的 `tools/gpu/`（plan JSON → jobs 客户端 → modal app → worker.execute → 产物清单）和 `docs/modal-gpu.md` 是可参考的既有模式，不是要直接依赖的库。
