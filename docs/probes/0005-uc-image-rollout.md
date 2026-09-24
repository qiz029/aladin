# 文生图 / 图生图 UC 权重切换

日期：2026-09-23 UTC。范围仅 `txt2img`、`img2img`；两者共用图像模型。

## 权重来源

- 仓库：`abenzerps/Qwen-Image-2.1-Uncensored-GGUF`
- 固定 revision：`1206d38bb47ef93961bfb77bc2c700d43a25860e`
- 文件：`qwen-image-2.1-UC-Q8_0.gguf`
- 大小：7,591,557,920 bytes
- 发布者提供的 SHA256：`cde456c72ea3ecebfc1be783300e972711d875e0c5f1bed33d42b66b156affa8`
- [作者模型卡](https://huggingface.co/abenzerps/Qwen-Image-2.1-Uncensored-GGUF)
- [固定版本的校验表](https://huggingface.co/abenzerps/Qwen-Image-2.1-Uncensored-GGUF/blob/1206d38bb47ef93961bfb77bc2c700d43a25860e/SHA256SUMS)

原来选择的是 base 文件 `qwen-image-2.1-Q8_0.gguf`，revision
`c4de66efa2183fb25ecbc185bac509ee37952b41`。UC 文件使用独立文件名与缓存路径，
不将原文件改名冒充新权重。文本编码器、VAE 在新版本中的文件校验值与此前核对一致。

作者的 uncensored 标注属于来源声明，不等于已验证任意题材的输出行为。

## 实现与部署

修改 `aladin_modal_app.py`、`request.py` 和 worker 生成源 `tools/build_worker.py`，然后重新生成
`aladin/worker.py`。参数、API 路径、采样器和调度器保持现有契约。
`edit` 继续选择 Qwen-Image-Edit-2509；视频与规划模型不在此次切换范围。

worker revision：`bc9a3cf434e9b0d85a677f824019bd7cfa7a121afd578db0d48cdd4c9880a2cb`

- 现有 unittest：92 项通过。
- 请求端和 worker 的三个 mode 模型声明逐项比对通过。
- 已部署 `aladin-image-v1`；CPU `worker_probe` 返回与本地相同的 worker revision。
- 已部署 workstation API/worker；运行容器回读为 UC 文件与上述固定 revision。
- 部署前检查无运行中任务；数据库保留。

## 实际链路验证

验证使用普通陶瓷茶壶产品图，不用于评估特定题材能力。
文生图：25 步、CFG 1、Euler/simple、seed 20260923。
图生图：以上图作为输入，20 步、CFG 1、Euler/simple、denoise 0.45、seed 20260924。

首次文生图任务 `869765b58c0d4329a01b453829d279ad` 在权重加载阶段失败：
`Unexpected architecture type in GGUF file: 'qwen_image21'`。新 UC 文件明确携带
`qwen_image21` 元数据，旧加载器只支持自动识别这种架构，遗漏了显式架构白名单。

将图像应用的 ComfyUI-GGUF 固定版本从 `f912d5e5c25921e41eae2c0131eeb4d350e7c165`
升级为 `edd981b10e107d3b8f58e16c498f2d08f631bc47`，包含上游两个 loader.py 修复：
加入架构白名单、反量化所有被量化的一维张量。保留已有依赖构建缓存，再单独 checkout
新版本；没有变更 ComfyUI 或视频应用的加载器版本。
[上游差异](https://github.com/leejet/ComfyUI-GGUF/compare/f912d5e5c25921e41eae2c0131eeb4d350e7c165...edd981b10e107d3b8f58e16c498f2d08f631bc47)

修复后重新部署 Modal 和 workstation，92 项测试再次通过。
状态：两条实际链路均通过。通过 workstation API 提交，Modal 实际执行，图片回传存储后可下载。
请求记录和 Modal results Volume 的 result.json 均确认 UC 文件、固定模型 revision、
新 GGUF revision 和最终 worker revision。两张下载产物的 SHA256 与 API 记录一致；
图生图回执的 inputSha256 与文生图产物一致。

| 模式 | 任务 ID | worker 执行时间 | 提交至完成 | 结果 |
| --- | --- | --- | --- | --- |
| txt2img | `5d0107bdd89646bd84314fe6c81a2f41` | 49.36 s | 68.28 s | succeeded / 1024×1024 |
| img2img | `8447ccb8cc1d4fa888c2fcfa1eb17da1` | 44.39 s | 69.33 s | succeeded / 1024×1024 |

两张产物均经查看：文生图为蓝色陶瓷茶壶，图生图保留茶壶和构图，壶盖、壶口及表面细节出现小幅变化。
这是普通产品图的链路与输出验证，不是任意题材能力测试。
详细请求、回执及下载图片存于 `.cache/uc-rollout/`；首次失败证据保存在其中的 `failed-old-loader/`。


## 回滚

本次修改前的四个文件保存在 `.cache/uc-rollout/before/`。
如需回滚，恢复四个文件，再先部署 Modal、后同步部署 workstation。
恢复原权重选择即可；无需删除 UC 缓存、数据库或已有图片。
