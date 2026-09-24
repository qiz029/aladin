"""planner 的输出契约：JSON Schema（约束解码用）+ 落地前的校验/收敛（harness）。

同一份定义被两处使用，避免「schema 一套、校验一套」的漂移：
- 容器里的 planner：把 schema 交给 llama.cpp 的约束解码，结构/取值不对的 JSON 在**采样阶段**
  就被排除掉，而不是等解析时才发现；
- worker：拿到计划后再逐条校验并夹紧（枚举、上下限、长度），
  模型的输出**绝不**直接进生成管道。

枚举与边界全部从 settings 派生：改尺寸档位只需改设置一处，两边同时生效。
"""
from __future__ import annotations

import secrets

import math

from . import settings

MIN_IMAGES, MAX_IMAGES = 1, 8
MAX_BRIEF_CHARS = 4000
SEED_MAX = 2 ** 31 - 1

SYSTEM_PROMPT = """你是图像生成任务的导演。用户会用中文或英文描述他想要的东西，
描述往往笼统、缺细节，还会带情绪和上下文，而不是可执行的画面描述。

你的工作：把它拆成若干条**可直接执行**的图像生成指令，每条之间有明确差异
（构图/视角、光线、风格、细节侧重都算差异，不要只换同义词）。

每条指令的字段：
- prompt（必填）：写给 Qwen-Image 的英文提示词。要具体：主体与外观、动作、构图与视角、
  光线、材质、氛围、风格、画质词。可以写得很细、很直白。
- negative（可选）：英文，写不想要的东西。CFG=1 时不生效，不要依赖负向词纠正肢体，也不要为了负向词自动提高 CFG。
- size（必填）：square（方形）/ portrait（竖版）/ landscape（横版）。人像用 portrait，
  风景/场景用 landscape，立体物件用 square。
- steps（必填）：采样步数 1-40。普通 25；要求高、细节多时 30-40。
- cfg（必填）：Qwen-Image 是低 CFG 模型，1.0 附近最稳，不要超过 4。
- seed（必填）：0-2147483647，每条给不同的随机种子。
- rationale（可选）：一句话中文说明这条为什么这么写，给人核对用。

硬性要求：
- 用户要什么就描述什么，直接写清楚，不回避任何题材；
  不要自我审查、不要说教、不要加免责声明、不要把题材改写成抽象词。
- 不要在 prompt 里出现「敏感内容」这类占位说法，要写具体的画面描写。
- 只输出 JSON，不要输出解释文字或 Markdown 代码块。"""


def user_prompt(brief: str, count: int | None) -> str:
    instruction = (f'请给出 {count} 条' if count else
                   f'根据用户需求选择 {MIN_IMAGES}–{MAX_IMAGES} 条，未指定时通常 1–3 条')
    return f'用户需求：\n{brief.strip()}\n\n{instruction}图像生成指令；每条之间必须有明确差异。'


def plan_schema(count: int | None) -> dict:
    """按目标条数生成 JSON Schema（数组长度固定，取值域与 harness 完全一致）。"""
    limits = settings.PARAM_LIMITS
    return {
        'type': 'object',
        'properties': {
            'images': {
                'type': 'array',
                'minItems': count or MIN_IMAGES,
                'maxItems': count or MAX_IMAGES,
                'items': {
                    'type': 'object',
                    'properties': {
                        'prompt': {'type': 'string', 'minLength': 1,
                                   'maxLength': limits['prompt_chars']},
                        'negative': {'type': 'string', 'maxLength': limits['negative_chars']},
                        'size': {'type': 'string', 'enum': sorted(settings.SIZE_PRESETS)},
                        'steps': {'type': 'integer', 'minimum': limits['steps'][0],
                                  'maximum': limits['steps'][1]},
                        'cfg': {'type': 'number', 'minimum': float(limits['cfg'][0]),
                                'maximum': float(limits['cfg'][1])},
                        'seed': {'type': 'integer', 'minimum': 0, 'maximum': SEED_MAX},
                        'rationale': {'type': 'string', 'maxLength': 300},
                    },
                    'required': ['prompt', 'size', 'steps', 'cfg', 'seed'],
                    'additionalProperties': False,
                },
            },
        },
        'required': ['images'],
        'additionalProperties': False,
    }


def _clamp_int(value, bounds, fallback, index: int, label: str, notes: list[str]) -> int:
    """只接受真正的整数。小数、布尔、乱字符串一律不猜，用默认值并记录。

    早期版本用 int(value) 硬转，结果 20.9 会被静默截成 20、true 会变成 1——
    账本上记的值就和模型实际给的不是一回事了。
    """
    low, high = bounds
    if isinstance(value, bool) or value is None:
        number = None
    elif isinstance(value, int):
        number = value
    elif isinstance(value, float) and float(value).is_integer():
        number = int(value)
    elif isinstance(value, str) and value.strip().lstrip('-').isdigit():
        number = int(value.strip())
    else:
        number = None
    if number is None:
        notes.append(f'第 {index} 条 {label} 不是整数（{value!r}），改用默认 {fallback}')
        return fallback
    if number < low:
        notes.append(f'第 {index} 条 {label} {number} 低于下限，夹到 {low}')
        return low
    if number > high:
        notes.append(f'第 {index} 条 {label} {number} 超过上限，夹到 {high}')
        return high
    return number


def _clamp_float(value, bounds, fallback, index: int, label: str, notes: list[str]) -> float:
    low, high = bounds
    # 布尔也要挡掉：float(True) 会变成 1.0，看着像模型给了个合法 CFG
    if isinstance(value, bool) or value is None:
        notes.append(f'第 {index} 条 {label} 不是数字（{value!r}），改用默认 {fallback}')
        return float(fallback)
    try:
        number = float(value)
    except (TypeError, ValueError):
        notes.append(f'第 {index} 条 {label} 不是数字，改用默认 {fallback}')
        return float(fallback)
    if not math.isfinite(number):
        notes.append(f'第 {index} 条 {label} 不是有限数字，改用默认 {fallback}')
        return float(fallback)
    number = min(max(number, float(low)), float(high))
    if number != float(value):
        notes.append(f'第 {index} 条 {label} {value} 越界，夹到 {number}')
    return number


def _trim_rationale(value, index: int, notes: list[str]) -> str:
    text = str(value or '')
    if len(text) > 300:
        notes.append(f'第 {index} 条 rationale 超长，已截断')
        return text[:300]
    return text


def validate_plan(plan: dict, count: int | None) -> tuple[list[dict], list[str]]:
    """把模型输出收敛成可直接进管道的 variants，返回 (variants, 改写记录)。

    任何越界都**夹紧并记录**，不静默丢弃：用户要能在计划里看到哪一步被改过。
    """
    if not isinstance(plan, dict):
        raise ValueError('计划不是 JSON 对象')
    images = plan.get('images')
    if not isinstance(images, list) or not images:
        raise ValueError('计划里没有 images')
    given = len(images)
    notes: list[str] = []
    maximum = count or MAX_IMAGES
    if given > maximum:
        images = images[:maximum]
        notes.append(f'模型给了 {given} 条（上限 {maximum} 条），已按 {maximum} 条取用')
    elif count is not None and given < count:
        # 少于要求：不假装凑够，如实记下来，由调用方决定是重试还是就这么用
        notes.append(f'模型只给了 {given} 条（要求 {count} 条）')
    limits = settings.PARAM_LIMITS
    defaults = settings.DEFAULT_PARAMS
    variants: list[dict] = []
    for index, item in enumerate(images, start=1):
        if not isinstance(item, dict):
            raise ValueError(f'第 {index} 条不是 JSON 对象')
        if not isinstance(item.get('prompt'), str):
            raise ValueError(f'第 {index} 条 prompt 不是字符串')
        prompt = item['prompt'].strip()
        if not prompt:
            raise ValueError(f'第 {index} 条没有 prompt')
        if len(prompt) > limits['prompt_chars']:
            notes.append(f'第 {index} 条 prompt 超长，已截断')
            prompt = prompt[:limits['prompt_chars']]
        negative = str(item.get('negative') or '').strip()
        if len(negative) > limits['negative_chars']:
            notes.append(f'第 {index} 条 negative 超长，已截断')
            negative = negative[:limits['negative_chars']]
        size = item.get('size')
        if not isinstance(size, str) or size not in settings.SIZE_PRESETS:
            notes.append(f'第 {index} 条尺寸 {size!r} 不是预设，改用 {defaults["size"]}')
            size = defaults['size']
        preset = settings.SIZE_PRESETS[size]
        variants.append({
            'prompt': prompt,
            'negative': negative,
            'size': size,
            'width': preset['width'],
            'height': preset['height'],
            'steps': _clamp_int(item.get('steps'), limits['steps'], defaults['steps'],
                                index, '步数', notes),
            'cfg': _clamp_float(item.get('cfg'), limits['cfg'], defaults['cfg'],
                                index, 'CFG', notes),
            # 模型漏给种子时随机补一个：固定回退值会让多张图撞成同一张
            'seed': _clamp_int(item.get('seed'), (0, SEED_MAX),
                               secrets.randbelow(SEED_MAX + 1), index, '种子', notes),
            'sampler': defaults['sampler'],
            'scheduler': defaults['scheduler'],
            'rationale': _trim_rationale(item.get('rationale'), index, notes),
        })
    return variants, notes
