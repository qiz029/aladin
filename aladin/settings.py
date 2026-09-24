"""全局配置。路径与 Modal 资源名集中在这里，避免散落。"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PUBLIC_WEB_URL = os.environ.get('ALADIN_PUBLIC_URL', '')
# 容器内由 compose 提供；宿主上默认连 compose 发布到回环的那个端口
DATABASE_URL = os.environ.get(
    'DATABASE_URL', 'postgresql://aladin:aladin-local@127.0.0.1:5433/aladin')
DATA = Path(os.environ.get('ALADIN_DATA', ROOT / 'data'))
JOBS_DIR = DATA / 'jobs'      # 生成历史：可清理的中间产物
GALLERY_DIR = DATA / 'gallery'  # 图库：用户手动挑进来的，永久保留
INPUT_DIR = DATA / 'uploads'   # 上传的输入图（图生图 / 指令编辑用）

# 上传图上限。图是 base64 后随 Modal 请求走的，太大既慢又占内存；
# 8 MiB 足够 1536×1536 的 PNG，编辑用的输入图远小于此。
MAX_UPLOAD_BYTES = 8 * 1024 * 1024

# 三种生成模式共用同一套任务/产物/账本，只是工作流不同。
MODES = {
    'director': '一句话出图',
    'txt2img': '文生图',
    'img2img': '图生图（同一模型，以图为起点重画）',
    'edit': '指令编辑（Qwen-Image-Edit-2509）',
    'i2v': '图生视频（10Eros-Max，让一张图动起来）',
}
EDIT_DEFAULT_PARAMS = {
    'steps': 20,
    'cfg': 1.0,
    'sampler': 'euler',
    'scheduler': 'simple',
    'seed': None,     # None = 每次提交随机
    'denoise': 0.6,   # 仅 img2img 用
}

# Modal 侧资源。app/结果 Volume 归 aladin 自己（见 AGENTS.md 的命名约定）；
# 模型 Volume 只读共享 agent-media-lab 已验证的 14.63GB 权重缓存，避免重复下载。
APP_IMAGE = 'aladin-image-v1'
FUNCTION_IMAGE = 'generate'
VOLUME_MODELS = 'agent-media-lab-gpu-models-v1'
VOLUME_RESULTS = 'aladin-image-results-v1'
# 视频切片：独立 Modal app 与独立 Volume（见 aladin_video_modal_app.py）
APP_VIDEO = 'aladin-video-h3-v1'
FUNCTION_VIDEO = 'generate'
VOLUME_RESULTS_VIDEO = 'aladin-video-results-v1'
# 账单展示：worker 每 BILLING_REFRESH_SECONDS 秒拉一次 Modal 账单写快照表。
# Modal 没有查「余额」的 API，页面余额 = CREDIT_GRANT（本期额度，美元）减本期
# 已抵扣的 credits；不配额度就只显示开销与抵扣，不显示余额。
BILLING_REFRESH_SECONDS = 30 * 60
CREDIT_GRANT = (float(os.environ['MODAL_CREDIT_GRANT'])
                if os.environ.get('MODAL_CREDIT_GRANT') else None)

# container 内 ComfyUI 的地址与进程内模型目录（worker.py 里使用）
COMFY_URL = 'http://127.0.0.1:8188'
MODEL_ROOT = '/models/qwen-image'

# 生成参数边界，与 image_worker.validate() 一一对应。改动必须两边同步。
PARAM_LIMITS = {
    'images': (1, 8),
    'prompt_chars': 2000,
    'negative_chars': 2000,
    'size': (512, 1536),
    'size_multiple': 8,
    'steps': (1, 40),
    'cfg': (0.0, 10.0),
    'seed': (0, 2 ** 63 - 1),
}
SAMPLERS = ('euler', 'euler_ancestral', 'dpmpp_2m', 'dpmpp_2m_sde')
SCHEDULERS = ('simple', 'normal', 'beta')
# 主界面只暴露三个尺寸预设；宽高区间与 8 的倍数约束仍由 PARAM_LIMITS 守着，
# 以后要加自定义尺寸只是多一个入口。
SIZE_PRESETS = {
    'square': {'label': '方形', 'hint': '1024×1024', 'width': 1024, 'height': 1024},
    'portrait': {'label': '竖版', 'hint': '768×1152', 'width': 768, 'height': 1152},
    'landscape': {'label': '横版', 'hint': '1152×768', 'width': 1152, 'height': 768},
}
# Qwen-Image 是低 CFG 模型：参考实现已验证的计划用 1.0，不是 SD 习惯的 7。
DEFAULT_PARAMS = {
    'negative': '',
    'size': 'square',
    'steps': 25,
    'cfg': 1.0,
    'sampler': 'euler',
    'scheduler': 'simple',
    'seed': None,     # None = 每次提交随机；幂等键用提交时定下的真实值
}


# ── 视频（10Eros-Max / MiniMax-H3）─────────────────────────────────────────
# H3 按 24fps 生成，帧数必须为 17n+5，尺寸为 32 的倍数。
VIDEO_FPS = 24
VIDEO_DURATIONS = {
    'short': {'label': '2.3 秒', 'hint': '56 帧', 'frames': 56},
    'normal': {'label': '5.2 秒', 'hint': '124 帧', 'frames': 124},
    'long': {'label': '8 秒', 'hint': '192 帧', 'frames': 192},
}
VIDEO_SIZES = {
    'landscape': {'label': '横版', 'hint': '832×480', 'width': 832, 'height': 480},
    'portrait': {'label': '竖版', 'hint': '480×832', 'width': 480, 'height': 832},
    'landscape-hd': {'label': '横版 HD', 'hint': '1280×736（未实测）',
                     'width': 1280, 'height': 736},
    'portrait-hd': {'label': '竖版 HD', 'hint': '736×1280（未实测）',
                    'width': 736, 'height': 1280},
}
VIDEO_LIMITS = {
    'prompt_chars': 2000, 'negative_chars': 2000,
    'frames': (22, 192), 'size': (256, 1344), 'size_multiple': 32,
    'steps': (1, 40), 'cfg': (0.0, 10.0), 'shift': (0.01, 20.0),
    # 旧 API 字段保留，只允许中性值；Turbo 加速已融合在 checkpoint 中。
    'lora_strength': (1.0, 1.0), 'seed': (0, 2 ** 63 - 1),
}
VIDEO_DEFAULT_PARAMS = {
    'negative': '', 'duration': 'normal', 'size': 'landscape',
    'steps': 6, 'cfg': 1.0, 'shift': 12.0, 'sampler': 'res_multistep',
    'scheduler': 'simple', 'seed': None, 'loraStrength': 1.0,
}
VIDEO_SAMPLERS = ('euler', 'euler_ancestral', 'dpmpp_2m', 'dpmpp_2m_sde', 'uni_pc',
                  'res_multistep', 'er_sde', 'lcm')


def ensure_dirs() -> None:
    for path in (DATA, JOBS_DIR, GALLERY_DIR, INPUT_DIR):
        path.mkdir(parents=True, exist_ok=True)
