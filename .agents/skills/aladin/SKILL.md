---
name: aladin
description: Generate images, make img2img variations, apply instruction-based edits, or animate a still image into a short video (LTX-2.5 / LTX-2.3 image-to-video with audio and LoRAs) through the local aladin service running on the workstation, then download the resulting PNGs/webm. Use when asked to generate, draw, edit, or restyle an image, or to animate/turn a picture into a clip, with a local GPU service; when the user mentions aladin, 文生图, 图生图, 指令编辑, 图生视频, Qwen-Image, LTX, or 工作站生图; or when media is needed without an external API key.
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
| `director "描述想法" [--count 2] [--preset manga] --wait` | 自动规划 1–8 张并逐张生图，保留全部参数；manga 为一页多格漫画 |
| `submit "提示词" --size square --steps 25 [--seed 42] --wait` | 文生图（省略 `--seed` 为随机） |
| `edit --image in.png --mode img2img --denoise 0.6 --prompt "" --wait` | 以图为起点重画 |
| `edit --image in.png --mode edit --prompt "把杯子换成蓝色" --wait` | 按指令改图 |
| `status <job_id> [--out DIR]` | 查状态；给了 `--out` 且已完成就顺带下载 |
| `jobs --limit 10 [--state succeeded]` | 任务列表 |
| `delete <job_id>` | 删除已结束的任务及本地产物（收藏副本保留；进行中返回 409） |
| `gallery` / `gallery --add JOB NAME` / `gallery --remove ID` | 图库 |
| `video --image in.jpg --prompt "云慢慢飘过" --model ltx-2.5 --duration normal --wait` | 图生视频（起始图必给；产物是有声 webm；`--lora ID[:强度]` 可叠加） |
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
  两套模型各自一个 app：`ltx-2.5`（默认，画质与提示词理解更好）与 `ltx-2.3`（NSFW LoRA 生态更全）。
  都联合生成视频和音频；冷启动与生成耗时以实际任务为准。
  帧率固定 24fps，时长为 short 2s / normal 5s / long 8s。
  提示词描述**动作、镜头和声音**即可，画面内容由起始图决定；视频没有 `images` 概念，一次一条。

## 直接用 HTTP

脚本只是包装。要自己调（例如在别的语言里）：

```sh
# 文生图：JSON 提交，服务端等到完成再返回
curl -s -X POST "$ALADIN_URL/api/v1/images?wait=true&timeout=300" \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"a brass compass on aged paper","size":"square","steps":20}'

# 图生视频：JSON + base64 起始图，产物是有声 webm（默认 5 秒 / 24fps）
curl -s -X POST "$ALADIN_URL/api/v1/videos/base64?wait=true&timeout=900" \
  -H 'Content-Type: application/json' \
  -d "{\"image_base64\":\"$(base64 -i in.jpg)\",\"prompt\":\"clouds drift slowly\",\"model\":\"ltx-2.5\",\"duration\":\"short\"}"

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

两套都按 Comfy 官方 distilled 模板跑：半分辨率 8 步 → 潜空间 2× 放大 → 3 步精修，CFG 1，
步数 / 采样器 / 负向词固定不开放。24fps，帧数 8n+1；时长档位为 49 / 121 / 193 帧。
- `ltx-2.5`：`Lightricks/LTX-2.5` distilled int8 + Gemma 4 12B 文本编码器。
- `ltx-2.3`：`Lightricks/LTX-2.3-fp8` dev + 0.5 distilled LoRA（官方模板做法）+ Gemma 3。
LoRA 按模型分族（`GET /api/v1/loras` 里 family 为 `ltx25` / `ltx23`），跨模型会被 422 挡住；
触发词由服务端补到提示词末尾。`ltx23-sulphur` 是 Sulphur 2 NSFW 微调的 LoRA 形式（10GB）。
输出 WebM，并包含模型生成的音频。HD 档仍标为未实测。

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
| LTX 视频 | 结尾 | 无 | sensual, suggestive | nsfw, explicit, uncensored |

实时的表以 `GET /api/v1/params` 的 `prompt_defaults` 为准。提示词里已有的词大小写不敏感去重；负向词不变。
任务 `params.prompt_defaults` 保存 original / effective / tags / rating / family，任务页可展开看实际发送的内容；
「再来一张」沿用原任务的配置。尺度参与幂等键：同提示词换尺度是新任务。加上标签后超过 2000 字符会被拒绝，不截断。

## 一句话出图：选模型、LoRA、先看计划

可选 `creative_spec`：`{"must_keep":"蓝色外套", "may_change":"背景", "change_only":"傍晚暖光"}`。
每项字符串最多 1000 字符；空项忽略。要求参与任务幂等键，保存在 `params.creative_spec` 与规划请求里，
planner 对每张图应用并在 rationale 中说明。CLI 对应 `--must-keep` / `--may-change` / `--change-only`。
这是规划约束，不是已经通过的视觉验收。人工评审可增加 `requirements: {"must_keep":"pass"}`，
键对应上述三项，值为 pass / fail / unsure；不能把 agent 判断写成人工评审。

`GET /api/v1/jobs/{id}/artifacts/{name}/reuse` 返回单张图片的 `settings`、`page`、`mode`、`input_url`。
文生图的 settings 可作为 `/images` 请求基础；改图需另外下载 input_url（原始输入图）、编码为 image_base64，
再提交 `/edits/base64`。默认 images=1，seed 使用所选图片的实际值。接口只读取，不触发生成。
网页图片预览与任务卡片的「用相同设置创作」使用同一逻辑；缺失的原输入图或不再可用的模型 / LoRA 会报错。
收藏只有在来源任务仍存在时能恢复完整设置。

`POST /api/v1/director` 除 `brief` / `count` / `rating` 外还接受：
- `model`：`qwen-image-2.1`（默认）/ `pony-realism-2.2` / `anima-base-1.0`。planner 按目标模型的写法写提示词
  （Qwen 自然语言；Pony 以 score_9 开头的标签并写负向词；Anima 以 masterpiece 开头的 Danbooru 标签），
  尺寸、步数、CFG 也落在该模型的范围内。
- `loras`：整批共用的 LoRA（仅 Pony / Anima），同生图接口。planner 会被告知启用了哪些，写提示词时配合；
  触发词自动补。Anima Turbo 会把步数收窄到 8–12、CFG 固定为 1。
- `review: true`：规划完停在 `state = review`（`stage = review`），不花生图的 GPU。
  `POST /api/v1/jobs/{id}/approve` 确认：不带 body 原样确认；带 `{"variants": [...]}` 整体替换
  （每项 prompt / negative / size / steps / cfg / seed，prompt 写原文，标签与触发词会自动补）。review 中的任务可以直接 DELETE。
- CLI：`director "…" --model pony-realism-2.2 --lora style-photo-2 --review`，然后 `approve <job_id>`。

## 一句话出图：漫画预设（`preset: "manga"`）

一页多格的日式漫画：planner **先写故事**（`plan.story` 标题 / 梗概 / 场景、`plan.characters` 固定外貌），
再按版式逐格分镜。`count` 是格数（4–8，默认 6），版式随格数定，从右上开始右→左、上→下阅读；
每格尺寸由格子形状决定，不用传 size。版式坐标见 `GET /api/v1/params` 的 `director.presets.manga.layouts`。
- 每个 `plan.variants[]` 带 `panel`：`index`、`rect`（页面比例 x, y, w, h）、`beat`（setup/build/turn/climax/aftermath）、
  `shot`、`rating`、`characters`（角色下标）、`action`、`caption`、`dialogue: [{speaker, text}]`。
- **尺度按格给**：请求的 `rating` 是上限，每格的 `panel.rating` 可以更低（铺垫格通常是 general / suggestive），
  标签按每格的尺度补。
- 对白与旁白只存在计划里，**不画进图**（拼页和对白框排版尚未实现）。
- 体位 / 视角类 LoRA（会让每格同一个姿势）和整页漫画生成器 LoRA（`hentai-comic-*`，会变成格中格）在此预设下 422；画风、画质和其他概念类可以用。
- approve 时不能增删格：`variants` 必须与原计划等长，按位置覆盖，可改 prompt / negative / steps / cfg / seed /
  rating / action / caption / dialogue，省略的字段保持原样。
- CLI：`director "…" --preset manga --count 6 --model anima-base-1.0 --review`。

## LoRA（仅 Pony / Anima）

`POST /api/v1/images` 可带 `loras: [{"id": "...", "strength": 0.7}]`，最多 6 个；省略 strength 用默认值。
可选项、默认强度、区间、触发词与内置预设见 `GET /api/v1/loras`（按 family：pony / anima 分）。
- 选了 LoRA，服务端自动把它的**触发词**补到提示词末尾；不用自己写。
- 跨底模（例如给 Qwen 或 Anima 选 Pony 的 LoRA）、强度越界、两个漫画生成器同时用 → 422。
- 滑杆类（*-slider）可以取负值；同时用多个体位 / 画风 LoRA 不会被拒，但结果常互相干扰。
- `anima-turbo` 需要 CFG 1、8–12 步、euler 采样器，调用时要自己把这三个参数一起传。
- 任务 `params.loras` 记录版本号、文件与 sha256，可精确复现。
- CLI：`submit "..." --model pony-realism-2.2 --lora pony-realism-enhancer:0.7 --lora real-skin-slider:2 --wait`

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
