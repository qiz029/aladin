# 人物质量：第一版

本版提供测试场景、人工比较及框选修复。没有完成模型质量排名，不能据此承诺更高的肢体合格率。

## 用法

1. `/apps/image` 选择人物测试场景，填入固定提示词、竖版、4 张候选和 seed 4100–4103。模型参数仍使用所选模型默认值。
2. 任务页进入「并排比较与人工评审」，可填写另一任务号对照不同模型。分别记录肢体结构、需求符合度、是否愿意保留和具体问题。
3. 评审保存到产物记录。任务 JSON 和「导出参数与评审记录」均保留评审、输入、提示词、模型请求版本与种子；未评审的 review 为 null。
4. 点击「局部修复」带入原图，拖动矩形选区，或输入四边百分比坐标。写清楚修改指令再提交；原图和候选都保留。

修复使用 Qwen-Image-Edit 2509：选区加 50% 上下文边距（最少 32px）→ 指令编辑 → 缩放回裁切尺寸 → 仅在选区内向内羽化合成。不是专用蒙版训练模型；跨边界结构和接缝可能仍有问题。PNG 透明度在选区外保留，JPEG 按解码且 EXIF 方向校正后的像素比较；输出为原尺寸 PNG。输入最多 1600 万像素，选区至少 8×8 像素。

## 批量实验

`aladin/people_cases.json` 固定 8 个场景；每个模型默认 32 张。先选择一个模型、一个场景做小批次，避免直接生成全部组合。

```sh
python tools/people_benchmark.py --model qwen-image-2.1 --case cup --out /tmp/people-plan.json
# 真正提交时换输出文件名并加 --submit --url <服务源站>
```

脚本默认只写计划，不提交 GPU。使用 `--submit` 后每次提交保存任务号，报错即停止，不自动重试。状态 submitted_not_reviewed 仅表示已提交；后续通过任务页查看完成状态与人工评审。manifest 中 case/model/job_id 将评审导出与测试集关联起来。

评估分别记录：全部生成数、已评审数、肢体通过/失败/不确定/不适用、符合需求数，以及两项都通过的数量。只在已评审且适用的样本中计算通过率，同时明确不确定样本数量；不要把未评审样本当作通过。计时从提交到完成，保留冷启动；费用只有实际账单能够归因时才填写，否则标记未知。跨模型同 seed 不代表同构图；动漫用途需另建同画风测试组。

## API

- `GET /api/v1/benchmarks/people`：测试场景。
- `PUT /api/v1/jobs/{id}/artifacts/{name}/review`：人工评审；anatomy 为 pass/fail/unsure/not_applicable，matches_request 为 pass/fail/unsure，preferred 布尔，notes 最多 2000 字。
- `POST /api/v1/edits/base64`：原有请求加 `mode: "edit", region: [0.2,0.3,0.5,0.7]`。region 是 EXIF 方向校正后的原图四边坐标，取值 0–1。
- `POST /api/v1/edits`：multipart 的 region 为上述数组的 JSON 字符串。

CFG=1 时负向提示词不参与采样，页面、API 参数说明与 planner 已同步说明。未更改各模型默认 CFG。

## 发布与验证

需要迁移 `0007_artifact_review.sql`，更新 API/worker 镜像，并同步部署引用 `aladin/worker.py` 的三个 Modal 图像 app，确保 workerRevision 一致。本地测试验证请求、像素合成、路由和评审持久化；上线前还需真实 GPU 修复样本与人工审图，不以合成测试代替质量验收。

2026-09-23 本地验证：122 项 unittest 通过；三个前端脚本语法检查及依赖锁校验通过。Chrome 中使用独立数据库和明确标记的示意图，验证并排显示、评审保存、原图自动带入、拖动框选、百分比坐标输入和修复任务入队。未启动该预览数据库的 GPU worker；未部署，未运行人物质量基准或真实 GPU 修复。
