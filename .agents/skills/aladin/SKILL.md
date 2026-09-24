---
name: aladin
description: Generate images, make img2img variations, apply instruction-based edits, or animate a still image into a short video (10Eros-Max / MiniMax-H3 image-to-video) through the local aladin service running on the workstation, then download the resulting PNGs/webm. Use when asked to generate, draw, edit, or restyle an image, or to animate/turn a picture into a clip, with a local GPU service; when the user mentions aladin, 文生图, 图生图, 指令编辑, 图生视频, Qwen-Image, 10Eros-Max, or 工作站生图; or when media is needed without an external API key.
---

# aladin

本机的生图服务：文生图、图生图、指令编辑。模型跑在 Modal 的按需 GPU 容器上，
产物落在这台工作站的磁盘上。**API 源站仅绑定 tailnet；公网网页由 Cloudflare Access 的 Google 白名单保护。**

- 服务地址：仓库根目录 `.env` 的 `ALADIN_URL`（例如 `http://<工作站>.<tailnet>.ts.net:8765`），CLI 会自动读取
- 覆盖方式：环境变量 `ALADIN_URL`，或每条命令的 `--url`
- 参数边界与可选值：`aladin.py params`（别猜，直接查）

## 快速开始

```sh
# 脚本在本 skill 的 scripts/ 下，只用标准库，不需要装依赖
python3 .agents/skills/aladin/scripts/aladin.py submit "a brass compass on aged paper" --wait
```

`--wait` 会阻塞到出图（实测一次文生图约 70–90 秒），产物下载到 `aladin-out/<job_id>/`，
stdout 上每行一个结果，直接读就行：

```
job 4d65cf27121640ca82d059d20c65c52b
state succeeded
file aladin-out/4d65cf27121640ca82d059d20c65c52b/image-01.png
```

## 命令

| 命令 | 用途 |
| --- | --- |
| `director "描述想法" [--count 2] --wait` | 自动规划 1–8 张并逐张生图，保留全部参数 |
| `submit "提示词" --size square --steps 25 [--seed 42] --wait` | 文生图（省略 `--seed` 为随机） |
| `edit --image in.png --mode img2img --denoise 0.6 --prompt "" --wait` | 以图为起点重画 |
| `edit --image in.png --mode edit --prompt "把杯子换成蓝色" --wait` | 按指令改图 |
| `status <job_id> [--out DIR]` | 查状态；给了 `--out` 且已完成就顺带下载 |
| `jobs --limit 10 [--state succeeded]` | 任务列表 |
| `delete <job_id>` | 删除已结束的任务及本地产物（收藏副本保留；进行中返回 409） |
| `gallery` / `gallery --add JOB NAME` / `gallery --remove ID` | 图库 |
| `video --image in.jpg --prompt "云慢慢飘过" --duration normal --wait` | 图生视频（起始图必给；产物是 webm） |
| `params` | 参数边界、尺寸预设、默认值（含视频档位） |
| `billing` | Modal 账单快照：本期开销、credits 抵扣、余额估算 |

尺寸只能传预设键 `square` / `portrait` / `landscape`；具体宽高按模型查询 `params.image_models`。
**改图的尺寸不可选**，跟随输入图自动吸附到受支持档位。

## 需要知道的几个行为

- **提交是异步的**：不加 `--wait` 立刻返回 job id，之后用 `status` 查。
- **种子默认随机**：省略 `seed` 时服务端在提交时随机取一个，写进任务的 `params.seed`
  （以及逐图 `artifacts[].seed`）。要复现某张图，把那个值显式传回去。
- **同参数重复提交不是错误**：显式给了相同的种子和参数时，服务返回**已存在**的那个任务
  （`created: false`），脚本会往 stderr 打一行 `note`。
- **参数错误是清单**：HTTP 422，`detail` 是数组，脚本会拼成一行打印并退出 1。
  不花 GPU 钱。
- **CFG 按模型选择**：Qwen 默认 1.0；Anima Base 1.0 默认 4.5；Pony Realism 2.2 默认 6.5。省略参数即使用该模型默认值。
- **图库与任务历史有页面**，给人看的：`/jobs`、`/gallery`（图片和视频都能长期收藏）。
- **图生视频是另一个切片**：Modal app 不同、产物 Volume 不同，但任务/接口形状一致。
  10Eros-Max beta5 Turbo 联合生成视频和音频；冷启动与生成耗时以实际任务为准。
  帧率固定 24fps，时长为 short 2.3s / normal 5.2s / long 8s。
  提示词描述**运动**即可，画面内容由起始图决定；视频没有 `images` 概念，一次一条。

## 直接用 HTTP

脚本只是包装。要自己调（例如在别的语言里）：

```sh
# 文生图：JSON 提交，服务端等到完成再返回
curl -s -X POST "$ALADIN_URL/api/v1/images?wait=true&timeout=300" \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"a brass compass on aged paper","size":"square","steps":20}'

# 图生视频：JSON + base64 起始图，产物是 webm（默认约 5.2 秒 / 24fps）
curl -s -X POST "$ALADIN_URL/api/v1/videos/base64?wait=true&timeout=900" \
  -H 'Content-Type: application/json' \
  -d "{\"image_base64\":\"$(base64 -i in.jpg)\",\"prompt\":\"clouds drift slowly\",\"duration\":\"short\"}"

# 改图：JSON + base64（图片最多 8 MiB）
curl -s -X POST "$ALADIN_URL/api/v1/edits/base64?wait=true" \
  -H 'Content-Type: application/json' \
  -d "{\"image_base64\":\"$(base64 -i in.png)\",\"mode\":\"edit\",\"prompt\":\"make it night\"}"
```

其他端点：`GET /api/v1` 是能力自描述，`GET /api/v1/params` 是参数边界，
详情页 `/docs`（Swagger UI）与 `/openapi.json`。任务详情里 `links` 字段直接给出
可访问的 URL（产物、输入图、SSE 事件流）。

## 别做的事

- 不要把服务暴露到 tailnet 之外：它没有鉴权，提交任务会花真金白银。
- 不要为同一张图反复提交完全相同的参数（含显式种子）——会被幂等闸门挡住，拿不到新结果。
- 不要在提示词里塞超长文本：上限 2000 字符。

## 一句话出图

`director` 调用 `POST /api/v1/director`；只给 `brief` 时由模型决定张数（1–8），
`--count` 可固定数量。先在 Modal 规划，再自动生图；任务里的 `stage` 区分两阶段。
返回的 `plan` 保留原始输出与有效计划，`artifacts[].prompt/params` 保留逐图参数。
CLI 下载产物时同时保存 `generation.json`，不要只交付 PNG 而丢失复现信息。
页面入口 `/apps/director`，任务页可展开完整计划、逐图微调或打包下载。

## 视频模型参数

视频后端为 `TenStrip/10Eros-Max` beta5 Turbo INT8，作者标注 NSFW capable。
24fps，帧数按 17n+5；时长档位为 56 / 124 / 192 帧。默认 6 步、CFG 1、Shift 12、res_multistep/simple。
输出保留 WebM，并包含模型生成的音频。提示词可描述动作、环境音与音乐。
`lora_strength` 为旧 API 兼容字段，只允许 1.0；Turbo 已融合，不能再叠加旧 Wan 蒸馏 LoRA。
CFG=1 时只使用正向条件；负向提示词在 CFG 不等于 1 时才参与计算。HD 档仍标为未实测。

## 文生图模型选择

`submit --model anima-base-1.0 "a watercolor teapot" --wait`，或 `--model pony-realism-2.2`。
默认 `qwen-image-2.1`。`--steps`、`--cfg`、`--sampler`、`--scheduler`、`--negative` 可覆盖模型默认；显式空 negative 会清空负向词。
API 在 `POST /api/v1/images` 增加 `model`，`GET /api/v1/params` 的 `image_models` 提供每个模型的预设和支持值。
Anima Base 1.0：35 步、CFG 4.5、er_sde/simple。Pony Realism 2.2 Main + VAE：30 步、CFG 6.5、dpmpp_2m_sde/karras、固定 Clip Skip 2。
任务与逐图参数保留模型。正向提示词会按下述默认标签规则补充；模型提示词建议在网页模型说明中显示。
新增选择仅用于文生图；一句话出图、图生图和指令编辑维持原有模型。

## 人物比较与局部修复

- `GET /api/v1/benchmarks/people` 返回版本化人物测试场景。页面可选择场景并生成 4 张固定种子候选；尚未证明哪套模型/参数最优。
- `/jobs/{id}/compare` 并排查看候选；`?other={job_id}` 可加入另一任务。
- `PUT /api/v1/jobs/{id}/artifacts/{name}/review` 保存人工评审：`anatomy` 为 pass/fail/unsure/not_applicable，`matches_request` 为 pass/fail/unsure，可选 preferred 布尔与 notes。GET 任务的 artifacts[].review 与 `/jobs/{id}/review.json` 可取回。不要把 agent 判断写成 human 评审。
- 局部修复使用现有 `mode=edit` 加 `region: [left, top, right, bottom]`，四边坐标为经 EXIF 方向校正后的原图的 0–1 比例。multipart 的 region 是 JSON 字符串。
- CLI：`edit --image in.png --mode edit --region 0.2 0.3 0.5 0.7 --prompt "修正右手的手指与手腕连接" --wait`。
- 当前实现裁取带上下文的区域，交给 Qwen-Image-Edit，再合成回原图；仅矩形选区内改动、保留原尺寸。这不是新训练的蒙版修复模型，衔接与肢体修复质量待实际审图验证。
- 文生图与改图的 CFG=1 时负向提示词不参与采样；不要仅为使负向词生效擅自提高 CFG。

## 尺度（rating）与自动标签

所有生成接口（`images` / `edits` / `videos` / `director`，JSON 与表单）都接受 `rating`：
`general`（日常）/ `suggestive`（暗示）/ `explicit`（露骨），省略用服务端默认（`ALADIN_DEFAULT_RATING`，默认 explicit）。
CLI 对应 `--rating`。服务端按模型把尺度换成该模型认的词，**不要自己在提示词里重复拼**：

| 模型族 | 位置 | general | suggestive | explicit |
| --- | --- | --- | --- | --- |
| Qwen-Image（文生图、改图、一句话出图） | 结尾 | 无 | sensual, suggestive | nsfw, explicit, uncensored |
| Anima Base | 开头 | safe | sensitive | explicit |
| Pony Realism | 开头 | rating_safe | rating_questionable | rating_explicit |
| 10Eros-Max 视频 | 结尾 | 无 | sensual, suggestive | nsfw, explicit, uncensored |

实时的表以 `GET /api/v1/params` 的 `prompt_defaults` 为准。提示词里已有的词大小写不敏感去重；负向词不变。
任务 `params.prompt_defaults` 保存 original / effective / tags / rating / family，任务页可展开看实际发送的内容；
「再来一张」沿用原任务的配置。尺度参与幂等键：同提示词换尺度是新任务。加上标签后超过 2000 字符会被拒绝，不截断。

## 收藏整理

- `GET /api/v1/gallery?q=&tag=&model=&kind=&min_stars=&rating=&sort=`：检索（q 搜提示词，不分大小写）。
  每项带 `tags`、`stars`、`model`、`mode`、`rating`。筛选项见 `GET /api/v1/gallery/facets`。
- `PATCH /api/v1/gallery/{id}`，JSON `{"tags": [...], "stars": 0-5}`：tags 整体替换（最多 20 个、每个 32 字符），stars 0 为未评分。
- CLI：`gallery --q 海边 --min-stars 3`、`gallery --tags 12 "夜景,精选"`、`gallery --stars 12 5`。
- `POST /api/v1/gallery` 收的是**表单字段**（job_id、name），不是 JSON。

## 隐私

- 产物文件不再内嵌 workflow / 提示词（容器 `--disable-metadata`，拉回本地时再剥一次）。复现信息在任务 JSON 与打包下载的 `generation.json` 里。
- 页面默认模糊缩略图（右上角「隐私模式」可关）。所有非静态响应带 `Cache-Control: private, no-cache`。
- `DELETE /api/v1/jobs/{id}` 之后，worker 会在一分钟内删掉 Modal 结果 Volume 上的副本。
