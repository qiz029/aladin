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

# 每个模型的「方言」：提示词写法与负向词的作用。数值边界不写在这里——从目标规格里生成，
# 这样改尺寸 / 步数上限只需改一处。
DIALECTS = {
    'qwen': """prompt 写给 Qwen-Image：英文自然语言，要具体——主体与外观、动作、构图与视角、光线、材质、
氛围、风格、画质词，可以写得很细、很直白。Qwen-Image 是低 CFG 模型：CFG=1 时负向词不生效，
不要依赖负向词纠正肢体，也不要为了负向词提高 CFG。""",
    'pony': """prompt 写给 Pony Realism（SDXL / Pony 系）：英文、逗号分隔的标签式短语，
以 score_9, score_8_up, score_7_up 开头，然后依次写人数与主体、外观、服装、动作与姿势、表情、构图与视角、
场景、光线、质感（写实照片可加 photo, realistic, detailed skin），可以夹少量短句。
这个模型 CFG 较高，negative **会生效，必须写**：例如 score_4, score_5, score_6, blurry, deformed hands,
extra fingers, extra limbs, bad anatomy, text, watermark；再按画面补充。""",
    'anima': """prompt 写给 Anima（动漫 / 插画模型）：英文 Danbooru 标签为主，以 masterpiece, best quality 开头，
然后写人数标签（1girl / 1boy / 2girls …）、成年人外观（adult, mature female 等）、服装、动作、表情、
构图与视角、背景、画风标签，末尾可以补一句自然语言描述。
negative **会生效，必须写**：例如 worst quality, low quality, score_1, score_2, score_3, blurry,
jpeg artifacts, bad anatomy, text, watermark；再按画面补充。""",
}

RULES = """## 硬性要求
- 画面中的所有人物都是成年人。
- 用户要什么就描述什么，直接写清楚，不回避任何题材；
  不要自我审查、不要说教、不要加免责声明、不要把题材改写成抽象词。
- 不要在 prompt 里出现「敏感内容」这类占位说法，要写具体的画面描写。
- 只输出 JSON，不要输出解释文字或 Markdown 代码块。"""

BASE_PROMPT = """你是图像生成任务的导演。用户会用中文或英文描述他想要的东西，
描述往往笼统、缺细节，还会带情绪和上下文，而不是可执行的画面描述。

你的工作：把它拆成若干条**可直接执行**的图像生成指令，每条之间有明确差异
（构图/视角、光线、风格、细节侧重都算差异，不要只换同义词）。

## 目标模型：{model}
{dialect}

## 每条指令的字段
- prompt（必填）：按上面的写法。**不要**写分级标签（safe / explicit / rating_* 等）和 LoRA 触发词，
  系统会按用户选的尺度与 LoRA 自动补上。
- negative：不想要的东西，写法见上。
- size（必填）：{sizes}。人像用竖版，风景/场景用横版，立体物件用方形。
- steps（必填）：采样步数 {steps_low}–{steps_high}，常用 {steps_default}；细节多时取上限附近。
- cfg（必填）：{cfg_low}–{cfg_high}，常用 {cfg_default}。
- seed（必填）：0-2147483647，每条给不同的随机种子。
- rationale（可选）：一句话中文说明这条为什么这么写，给人核对用。
{loras}
""" + RULES

LORA_GUIDE = """
## 本次启用的 LoRA（对每张图都生效）
{items}
写提示词时要与它们一致：体位 / 视角类 LoRA 已经决定了姿势和视角，不要再写别的姿势；
画风类 LoRA 已经决定了风格，不要写冲突的风格词；概念类 LoRA 的内容要自然地写进画面。
"""


def default_target() -> dict:
    """老请求（没带 target）与 Qwen 目标：边界与默认值取自 settings，与引入目标规格之前完全一致。"""
    limits, defaults = settings.PARAM_LIMITS, settings.DEFAULT_PARAMS
    return {'model': 'qwen-image-2.1', 'family': 'qwen',
            'sizes': {key: {'width': p['width'], 'height': p['height'], 'label': p['label']}
                      for key, p in settings.SIZE_PRESETS.items()},
            'steps': list(limits['steps']), 'cfg': [float(limits['cfg'][0]), float(limits['cfg'][1])],
            'defaults': {'size': defaults['size'], 'steps': defaults['steps'],
                         'cfg': float(defaults['cfg']), 'negative': defaults['negative'],
                         'sampler': defaults['sampler'], 'scheduler': defaults['scheduler']},
            'loras': []}


def validate_target(target) -> dict:
    """目标规格来自宿主，但它会被拼进系统提示和 schema：结构、枚举和长度都要挡住。"""
    if target is None:
        return default_target()
    if not isinstance(target, dict) or target.get('family') not in DIALECTS:
        raise ValueError('目标规格无效')
    sizes = target.get('sizes')
    if not isinstance(sizes, dict) or not 1 <= len(sizes) <= 8:
        raise ValueError('目标尺寸无效')
    for key, size in sizes.items():
        if not isinstance(key, str) or not key.isidentifier() or not isinstance(size, dict):
            raise ValueError('目标尺寸无效')
        for field in ('width', 'height'):
            value = size.get(field)
            if type(value) is not int or not 256 <= value <= 2048 or value % 8:
                raise ValueError('目标尺寸无效')
    for field, kind in (('steps', int), ('cfg', (int, float))):
        bounds = target.get(field)
        if (not isinstance(bounds, list) or len(bounds) != 2
                or not all(isinstance(v, kind) and not isinstance(v, bool) for v in bounds)
                or not 0 <= bounds[0] <= bounds[1] <= (60 if field == 'steps' else 10)):
            raise ValueError(f'目标 {field} 边界无效')
    defaults = target.get('defaults')
    if not isinstance(defaults, dict) or defaults.get('size') not in sizes:
        raise ValueError('目标默认值无效')
    loras = target.get('loras', [])
    if not isinstance(loras, list) or len(loras) > 6:
        raise ValueError('目标 LoRA 无效')
    for item in loras:
        if not isinstance(item, dict) or not all(
                isinstance(item.get(k), str) and len(item[k]) <= 200 for k in ('name', 'kind', 'notes')):
            raise ValueError('目标 LoRA 无效')
    return target


def system_prompt(target: dict | None = None) -> str:
    target = validate_target(target)
    defaults = target['defaults']
    sizes = ' / '.join(f"{key}（{p.get('label', key)} {p['width']}×{p['height']}）"
                       for key, p in target['sizes'].items())
    loras = ''
    if target['loras']:
        loras = LORA_GUIDE.format(items='\n'.join(
            f"- {item['name']}（{item['kind']}）：{item['notes'] or '无说明'}" for item in target['loras']))
    return BASE_PROMPT.format(
        model=target['model'], dialect=DIALECTS[target['family']], sizes=sizes,
        steps_low=target['steps'][0], steps_high=target['steps'][1], steps_default=defaults['steps'],
        cfg_low=target['cfg'][0], cfg_high=target['cfg'][1], cfg_default=defaults['cfg'], loras=loras)


# 兼容：老代码与测试引用的 Qwen 系统提示
SYSTEM_PROMPT = system_prompt()


def user_prompt(brief: str, count: int | None) -> str:
    instruction = (f'请给出 {count} 条' if count else
                   f'根据用户需求选择 {MIN_IMAGES}–{MAX_IMAGES} 条，未指定时通常 1–3 条')
    return f'用户需求：\n{brief.strip()}\n\n{instruction}图像生成指令；每条之间必须有明确差异。'


def plan_schema(count: int | None, target: dict | None = None) -> dict:
    """按目标条数与目标模型生成 JSON Schema（数组长度固定，取值域与 harness 完全一致）。"""
    limits = settings.PARAM_LIMITS
    target = validate_target(target)
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
                        'size': {'type': 'string', 'enum': sorted(target['sizes'])},
                        'steps': {'type': 'integer', 'minimum': target['steps'][0],
                                  'maximum': target['steps'][1]},
                        'cfg': {'type': 'number', 'minimum': float(target['cfg'][0]),
                                'maximum': float(target['cfg'][1])},
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


def validate_plan(plan: dict, count: int | None,
                  target: dict | None = None) -> tuple[list[dict], list[str]]:
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
    target = validate_target(target)
    variants = [_variant(item, index, target, notes)
                for index, item in enumerate(images, start=1)]
    return variants, notes


def _variant(item, index: int, target: dict, notes: list[str], size: str | None = None) -> dict:
    """一条指令 → 一个生图 variant。给了 size 时由宿主定尺寸（漫画格子），不看模型给的。"""
    limits = settings.PARAM_LIMITS
    defaults = target['defaults']
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
    if size is None:
        size = item.get('size')
        if not isinstance(size, str) or size not in target['sizes']:
            notes.append(f'第 {index} 条尺寸 {size!r} 不是预设，改用 {defaults["size"]}')
            size = defaults['size']
    preset = target['sizes'][size]
    return {
        'prompt': prompt,
        'negative': negative,
        'size': size,
        'width': preset['width'],
        'height': preset['height'],
        'steps': _clamp_int(item.get('steps'), tuple(target['steps']), defaults['steps'],
                            index, '步数', notes),
        'cfg': _clamp_float(item.get('cfg'), tuple(target['cfg']), defaults['cfg'],
                            index, 'CFG', notes),
        # 模型漏给种子时随机补一个：固定回退值会让多张图撞成同一张
        'seed': _clamp_int(item.get('seed'), (0, SEED_MAX),
                           secrets.randbelow(SEED_MAX + 1), index, '种子', notes),
        'sampler': defaults['sampler'],
        'scheduler': defaults['scheduler'],
        'rationale': _trim_rationale(item.get('rationale'), index, notes),
    }


# ── 预设：日式漫画（一页多格）──────────────────────────────────────────────
# 普通模式要的是「几张互不相同的图」；漫画要的是「一条故事线」。两者的提示词、schema 和 harness
# 都分开写，只共用单格的数值收敛（_variant）。
PRESETS = {'manga': '日式漫画（一页多格）'}
RATING_ORDER = ('general', 'suggestive', 'explicit')
MANGA_MIN, MANGA_MAX, MANGA_DEFAULT = 4, 8, 6
MAX_CHARACTERS = 3
PAGE_RATIO = 1.414  # 页面高 / 宽（B5、A4 的比例）
# 每种格数一套版式。坐标是页面比例 (x, y, w, h)，原点在左上；列表顺序就是日漫的阅读顺序
# （从右上开始，右→左、上→下）。以后宿主端拼页直接按这组坐标放图。
MANGA_LAYOUTS = {
    4: [(0, 0, 1, .3), (.45, .3, .55, .4), (0, .3, .45, .4), (0, .7, 1, .3)],
    5: [(0, 0, 1, .28), (.5, .28, .5, .36), (0, .28, .5, .36), (.4, .64, .6, .36), (0, .64, .4, .36)],
    6: [(.35, 0, .65, .3), (0, 0, .35, .3), (.5, .3, .5, .3), (0, .3, .5, .3),
        (.4, .6, .6, .4), (0, .6, .4, .4)],
    7: [(0, 0, 1, .22), (2 / 3, .22, 1 / 3, .26), (1 / 3, .22, 1 / 3, .26), (0, .22, 1 / 3, .26),
        (.6, .48, .4, .52), (0, .48, .6, .26), (0, .74, .6, .26)],
    8: [(.4, 0, .6, .25), (0, 0, .4, .25), (.6, .25, .4, .25), (0, .25, .6, .25),
        (.4, .5, .6, .25), (0, .5, .4, .25), (.5, .75, .5, .25), (0, .75, .5, .25)],
}
BEATS = {'setup': '铺垫', 'build': '升温', 'turn': '转折', 'climax': '高潮', 'aftermath': '余韵'}
SHOTS = {'wide': '远景', 'full': '全身', 'medium': '中景', 'close_up': '特写',
         'detail': '局部特写', 'pov': '主观视角'}
SHAPE_LABELS = {'landscape': '横长', 'portrait': '竖长', 'square': '近方'}
MAX_DIALOGUE = 2

MANGA_PROMPT = """你是日式成人漫画的编剧兼分镜师。用户给你一个题材或情境，你要把它写成**一页有完整故事的漫画**：
先想清楚故事和人物，再逐格分镜，最后为每一格写图像生成提示词。

## 目标模型：{model}
{dialect}

## 先写故事（story / characters）
- story.title：短标题。story.logline：一句话讲清「谁、处在什么关系或处境、想要什么、什么让局面升级、最后怎样」。
  story.setting：时间、地点、环境的具体描写。
- characters：1–{max_characters} 个成年角色。appearance 用上面目标模型的写法写**固定外貌**：成年的年龄感、发型发色、
  瞳色、体型、服装与配饰。之后每一格出现这个角色时，prompt 里**逐字复用**这段 appearance，只改动作、表情和衣着状态——
  这是让各格看起来是同一个人的唯一办法。

## 再分镜（panels，按阅读顺序：从右上开始，右→左、上→下）
本页版式已定，共 {count} 格：
{slots}
- 大格留给关键节拍（开场建立、转折、高潮），小格用于反应、细节和过渡。
- 节拍 beat：setup 铺垫 → build 升温 → turn 转折 → climax 高潮 → aftermath 余韵。整页是一条连续的故事线，
  每格的 action 必须承接上一格（因为上一格发生了什么，所以这一格怎样），不能是互不相干的画面。
- 尺度 rating（可选 {ratings}）：{rating_rule}
- 镜头 shot：wide 远景（交代环境）/ full 全身 / medium 中景 / close_up 特写（情绪）/ detail 局部特写 / pov 主观视角。
  相邻两格不要用同一种镜头；情绪和反应多用特写。
- characters：这一格出现的角色，填上面 characters 数组的下标（从 0 开始）；空镜可以为空。
- action：中文，一两句话写这一格发生了什么。
- caption：中文旁白，可以为空字符串。dialogue：0–{max_dialogue} 句中文对白，每句很短，口语、有情绪、有潜台词。
  对白和旁白由系统排进对白框，**不要**画进图里：prompt 里不要写文字、对白框、拟声词，negative 里写上 text, speech bubble。

## 每一格的生成参数
- prompt（必填）：按上面的模型写法写这一格的画面：出现的角色（逐字复用 appearance）+ 动作与姿势 + 表情 + 镜头与视角
  + 场景 + 光线。整页所有格用同一组画风词，保持画风一致。**不要**写分级标签（safe / explicit / rating_* 等）
  和 LoRA 触发词，系统会按每格的 rating 与所选 LoRA 自动补上。尺寸由格子决定，不用写。
- negative：写法见上。
- steps（必填）：{steps_low}–{steps_high}，常用 {steps_default}。cfg（必填）：{cfg_low}–{cfg_high}，常用 {cfg_default}。
- seed（必填）：0-2147483647。整页用同一个种子，画风和人物更稳定。
- rationale（可选）：一句话中文说明这一格在故事里的作用。
{loras}
""" + RULES

RATING_RULES = {
    'explicit': '尺度要逐格递进：前 1–2 格用 general 或 suggestive 建立人物关系和情境，中段升温，'
                'explicit 集中在后段的高潮格，最后的余韵可以回落。不要每一格都是 explicit——没有铺垫就没有故事。',
    'suggestive': '最高 suggestive。前段用 general 建立情境，逐步升温到 suggestive。',
    'general': '全部用 general。',
}


def allowed_ratings(cap: str | None) -> list[str]:
    """用户选的尺度是上限：每格可以更低，不能更高。"""
    cap = cap if cap in RATING_ORDER else RATING_ORDER[-1]
    return list(RATING_ORDER[:RATING_ORDER.index(cap) + 1])


def slot_shape(slot) -> str:
    """格子在页面上的像素宽高比 → 最接近的生成尺寸档（拼页时再裁切填满）。"""
    _, _, w, h = slot
    aspect = w / (h * PAGE_RATIO)
    return 'landscape' if aspect > 1.25 else ('portrait' if aspect < 0.8 else 'square')


def manga_layout(count: int) -> list[dict]:
    if count not in MANGA_LAYOUTS:
        raise ValueError(f'漫画格数需在 {MANGA_MIN}–{MANGA_MAX} 之间')
    return [{'index': index, 'rect': list(slot), 'shape': slot_shape(slot),
             'large': slot[2] * slot[3] >= 0.2}
            for index, slot in enumerate(MANGA_LAYOUTS[count], start=1)]


def manga_system_prompt(target: dict | None, count: int, rating: str | None) -> str:
    target = validate_target(target)
    defaults = target['defaults']
    slots = '\n'.join(f"- 第 {slot['index']} 格：{SHAPE_LABELS[slot['shape']]}{'大格' if slot['large'] else '小格'}"
                      for slot in manga_layout(count))
    allowed = allowed_ratings(rating)
    loras = ''
    if target['loras']:
        loras = LORA_GUIDE.format(items='\n'.join(
            f"- {item['name']}（{item['kind']}）：{item['notes'] or '无说明'}" for item in target['loras']))
    return MANGA_PROMPT.format(
        model=target['model'], dialect=DIALECTS[target['family']], count=count, slots=slots,
        ratings=' / '.join(allowed), rating_rule=RATING_RULES[allowed[-1]],
        max_characters=MAX_CHARACTERS, max_dialogue=MAX_DIALOGUE,
        steps_low=target['steps'][0], steps_high=target['steps'][1], steps_default=defaults['steps'],
        cfg_low=target['cfg'][0], cfg_high=target['cfg'][1], cfg_default=defaults['cfg'], loras=loras)


def manga_user_prompt(brief: str, count: int) -> str:
    return f'用户需求：\n{brief.strip()}\n\n请先写故事和人物，再按版式写出 {count} 格分镜。'


def _text(maximum: int) -> dict:
    return {'type': 'string', 'maxLength': maximum}


def manga_schema(count: int, target: dict | None, rating: str | None) -> dict:
    """story 与 characters 排在 panels 前面且必填：约束解码按属性顺序生成，模型得先把故事写完再分镜。"""
    limits = settings.PARAM_LIMITS
    target = validate_target(target)
    manga_layout(count)
    panel = {
        'type': 'object',
        'properties': {
            'beat': {'type': 'string', 'enum': list(BEATS)},
            'shot': {'type': 'string', 'enum': list(SHOTS)},
            'rating': {'type': 'string', 'enum': allowed_ratings(rating)},
            'characters': {'type': 'array', 'maxItems': MAX_CHARACTERS,
                           'items': {'type': 'integer', 'minimum': 0, 'maximum': MAX_CHARACTERS - 1}},
            'action': _text(300),
            'caption': _text(80),
            'dialogue': {'type': 'array', 'maxItems': MAX_DIALOGUE,
                         'items': {'type': 'object',
                                   'properties': {'speaker': _text(20), 'text': _text(60)},
                                   'required': ['speaker', 'text'], 'additionalProperties': False}},
            'prompt': {'type': 'string', 'minLength': 1, 'maxLength': limits['prompt_chars']},
            'negative': _text(limits['negative_chars']),
            'steps': {'type': 'integer', 'minimum': target['steps'][0], 'maximum': target['steps'][1]},
            'cfg': {'type': 'number', 'minimum': float(target['cfg'][0]),
                    'maximum': float(target['cfg'][1])},
            'seed': {'type': 'integer', 'minimum': 0, 'maximum': SEED_MAX},
            'rationale': _text(300),
        },
        'required': ['beat', 'shot', 'rating', 'characters', 'action', 'caption', 'dialogue',
                     'prompt', 'steps', 'cfg', 'seed'],
        'additionalProperties': False,
    }
    return {
        'type': 'object',
        'properties': {
            'story': {'type': 'object',
                      'properties': {'title': _text(40), 'logline': _text(200), 'setting': _text(200)},
                      'required': ['title', 'logline', 'setting'], 'additionalProperties': False},
            'characters': {'type': 'array', 'minItems': 1, 'maxItems': MAX_CHARACTERS,
                           'items': {'type': 'object',
                                     'properties': {'name': _text(20), 'appearance': _text(300)},
                                     'required': ['name', 'appearance'], 'additionalProperties': False}},
            'panels': {'type': 'array', 'minItems': count, 'maxItems': count, 'items': panel},
        },
        'required': ['story', 'characters', 'panels'],
        'additionalProperties': False,
    }


def _clip(value, maximum: int, label: str, notes: list[str]) -> str:
    text = value.strip() if isinstance(value, str) else ''
    if len(text) > maximum:
        notes.append(f'{label} 超长，已截断')
        return text[:maximum]
    return text


def _choice(value, options, fallback: str, index: int, label: str, notes: list[str]) -> str:
    if value in options:
        return value
    notes.append(f'第 {index} 格{label} {value!r} 无效，改用 {fallback}')
    return fallback


def validate_manga(plan: dict, count: int | None, target: dict | None,
                   rating: str | None) -> tuple[list[dict], dict, list[str]]:
    """漫画计划 → (variants, 故事信息, 改写记录)。

    输出可以原样再喂回来（variant 与它的 panel 合并即是输入形状），宿主复核与人工确认都走这里。
    尺寸由格子决定，模型给的不看；每格尺度超过上限就夹到上限。
    """
    if not isinstance(plan, dict):
        raise ValueError('计划不是 JSON 对象')
    target = validate_target(target)
    notes: list[str] = []
    story = plan.get('story') if isinstance(plan.get('story'), dict) else {}
    story = {'title': _clip(story.get('title'), 40, '标题', notes),
             'logline': _clip(story.get('logline'), 200, '梗概', notes),
             'setting': _clip(story.get('setting'), 200, '场景', notes)}
    characters = []
    for item in (plan.get('characters') or [])[:MAX_CHARACTERS]:
        if isinstance(item, dict):
            characters.append({'name': _clip(item.get('name'), 20, '角色名', notes),
                               'appearance': _clip(item.get('appearance'), 300, '角色外貌', notes)})
    panels = plan.get('panels')
    if not isinstance(panels, list) or len(panels) < MANGA_MIN:
        raise ValueError(f'漫画计划至少要 {MANGA_MIN} 格')
    maximum = count or MANGA_MAX
    if len(panels) > maximum:
        notes.append(f'模型给了 {len(panels)} 格（上限 {maximum} 格），已按 {maximum} 格取用')
        panels = panels[:maximum]
    elif count is not None and len(panels) < count:
        notes.append(f'模型只给了 {len(panels)} 格（要求 {count} 格），改用 {len(panels)} 格版式')
    allowed = allowed_ratings(rating)
    layout = manga_layout(len(panels))
    variants = []
    for slot, item in zip(layout, panels):
        index = slot['index']
        size = slot['shape'] if slot['shape'] in target['sizes'] else target['defaults']['size']
        variant = _variant(item, index, target, notes, size=size)
        level = item.get('rating')
        if level not in RATING_ORDER:
            notes.append(f'第 {index} 格尺度 {level!r} 无效，改用 {allowed[-1]}')
            level = allowed[-1]
        elif level not in allowed:
            notes.append(f'第 {index} 格尺度 {level} 超过上限，夹到 {allowed[-1]}')
            level = allowed[-1]
        present = item.get('characters')
        present = [c for c in present if type(c) is int and 0 <= c < len(characters)] \
            if isinstance(present, list) else []
        dialogue = []
        for line in (item.get('dialogue') or [])[:MAX_DIALOGUE]:
            if isinstance(line, dict) and _clip(line.get('text'), 60, '', []):
                dialogue.append({'speaker': _clip(line.get('speaker'), 20, f'第 {index} 格说话人', notes),
                                 'text': _clip(line.get('text'), 60, f'第 {index} 格对白', notes)})
        variant['panel'] = {
            'index': index, 'rect': slot['rect'], 'shape': slot['shape'], 'large': slot['large'],
            'beat': _choice(item.get('beat'), BEATS, 'build', index, '节拍', notes),
            'shot': _choice(item.get('shot'), SHOTS, 'medium', index, '镜头', notes),
            'rating': level,
            'characters': sorted(set(present)),
            'action': _clip(item.get('action'), 300, f'第 {index} 格情节', notes),
            'caption': _clip(item.get('caption'), 80, f'第 {index} 格旁白', notes),
            'dialogue': dialogue,
        }
        variants.append(variant)
    return variants, {'preset': 'manga', 'story': story, 'characters': characters,
                      'layout': len(variants)}, notes


def panel_input(variant: dict) -> dict:
    """validate_manga 的输出 → 它的输入形状（复核 / 人工修改时用）。"""
    return dict(variant, **variant.get('panel', {}))
