# 一句话出图接续与实测（2026-09-22 PDT）

从 DSCODE 会话 `session-562be061-4b7b-4d65-928b-be187f6429df` 接续。

## 发现并修正

- workstation 已使用 Q8，但 image.html 留着 Q4 标签。
- Dockerfile 未复制 video_request.py，视频页面/API 存在却无法真正提交。
- 图片去重键没含模型版本，升级权重后可能复用旧任务；现包含模型配置、worker 与 Comfy/GGUF 版本。
- build_worker.py 落后于生成的 worker.py，再运行会退回旧 Q4 与共享参数实现；已同步模板并加回归。
- planner 链接器找不到间接依赖的 SONAME libcuda.so.1。构建期增加桩库软链及 rpath-link，编译后移除软链，运行时使用真实驱动。
- 部署脚本以前把文件无变化当作镜像已部署，且可能吞掉 rsync 错误；现在同步失败即停，始终完成 api/worker 构建。

## 完整功能

页面 `/apps/director` 与 `POST /api/v1/director` 共用 params 校验及 pipeline.enqueue。
只输入 brief，张数由模型选择（1–8），API 也可指定 count。
同一任务行分规划/生成阶段保存，原子切换请求；已知 call_id 只接管不重投。
规划回执在独立 results Volume 中作为恢复中转，模型 Volume 只作缓存。
任务页展示计划、改写/重试记录、逐图 prompt/negative/尺寸/步数/CFG/seed。
单图可以回填生成表单微调；ZIP 和 CLI 下载保存 generation.json。

## 证据

- 99 项测试通过：`python -m unittest aladin.test_director aladin.test_worker aladin.test_web aladin.test_pipeline probe.test_watchdog`。
- CPU planner_probe：llamaServerBuilt=true，真实容器可导入。
- planner 指纹：`1184bcd45a812f57290e56bb165cdc5b4442a49c56b3ad66c7e95befdb9dbafc`。
- 图像 worker：`67d276c896b82f8937caaaab75853ace39467aea807de8923deaf7fbe0425ad1`。
- 视频 worker：`454029dbbcb5e9259d0179f5e1246dc5b5d63a82eda92b6d47d87ec3a76b18b2`。
- workstation api 镜像的 worker、request.py、video_request.py、director 模板哈希与本地一致。

### 实际规划 → 生图 → 下载

任务：`f52a8173be8b4b22b16da38d887a33d2`，状态 succeeded。
需求为两张海边咖啡馆照片，没有指定 API count，模型自主选择了两张：

| 产物 | 尺寸 | 步数 | CFG | Seed |
| --- | --- | --- | --- | --- |
| 海边白色小屋远景 | 1152×768 | 30 | 1.5 | 100001 |
| 窗边白瓷咖啡杯与花 | 1024×1024 | 30 | 1.5 | 100002 |

首次规划含 19.54 GB 下载，容器内 137.28 秒；第一次输出未通过解析，一次自动重试成功，notes 如实记录。
从 API 提交到两图下载完成约 269 秒。已实际查看两张图片。
ZIP 包含 generation.json 和两张 PNG；每个产物 SHA-256 与账本一致，逐图参数与有效计划逐字段一致。
这些是首次运行数据，不代表缓存权重后的延迟或质量保证；本轮没有测缓存后的规划延迟。

### 实际视频

任务：`2b566f36143144c69488f2359894b792`，状态 succeeded。
通过 workstation base64 API 提交已有黄铜物件探针图，约 71 秒取得文件。
ffprobe：VP9、832×480、16 fps、2.063 秒、209703 字节；SHA-256 与账本一致。
浏览器验证任务页播放器及放大观看对话框。

运行日志、原始 API 响应与下载文件保留在本地 `.cache/live-verification/`（不入库）。
