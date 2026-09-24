"""Host-side prompt defaults, frozen into each job before GPU submission.

「尺度」是一个与模型无关的选择（日常 / 暗示 / 露骨），落到每个模型族时换成它自己认的词：
- Anima：Danbooru 系分级词 safe / sensitive / nsfw / explicit，放在提示词**开头**（模型卡的顺序约定）。
- Pony：Pony V6 的 rating_safe / rating_questionable / rating_explicit，同样放开头。
- Qwen-Image 与视频：文本编码器是 LLM，吃自然语言，分级标签作用有限；只补少量直白词，放结尾。
这张表是起点，不是定论——按实际出图效果调整 FAMILIES 即可，已有任务不受影响（配置冻结在任务里）。
"""
import os
import re

RATINGS = {'general': '日常', 'suggestive': '暗示', 'explicit': '露骨'}
FAMILIES = {
    'qwen': {'label': 'Qwen-Image', 'placement': 'suffix',
             'tags': {'general': [], 'suggestive': ['sensual', 'suggestive'],
                      'explicit': ['nsfw', 'explicit', 'uncensored']}},
    'anima': {'label': 'Anima', 'placement': 'prefix',
              'tags': {'general': ['safe'], 'suggestive': ['sensitive'],
                       'explicit': ['explicit']}},
    'pony': {'label': 'Pony', 'placement': 'prefix',
             'tags': {'general': ['rating_safe'], 'suggestive': ['rating_questionable'],
                      'explicit': ['rating_explicit']}},
    'video': {'label': '10Eros-Max 视频', 'placement': 'suffix',
              'tags': {'general': [], 'suggestive': ['sensual', 'suggestive'],
                       'explicit': ['nsfw', 'explicit', 'uncensored']}},
}
MODEL_FAMILIES = {'anima-base-1.0': 'anima', 'pony-realism-2.2': 'pony'}


def default_rating() -> str:
    value = os.environ.get('ALADIN_DEFAULT_RATING', 'explicit').strip()
    return value if value in RATINGS else 'explicit'


def family_for(model: str | None = None, app: str = 'image') -> str:
    if app == 'video':
        return 'video'
    return MODEL_FAMILIES.get(model or '', 'qwen')


def tags_for(family: str, rating: str) -> list[str]:
    return list(FAMILIES[family]['tags'][rating])


def rating_error(rating) -> str:
    return '' if rating is None or rating in RATINGS else '尺度只能是 ' + ' / '.join(RATINGS)


def compile_prompt(prompt: str, tags: list[str], placement: str = 'suffix') -> str:
    prompt = (prompt or '').strip()
    missing = [tag for tag in tags if not re.search(
        r'(?<!\w)' + re.escape(tag) + r'(?!\w)', prompt, flags=re.IGNORECASE)]
    parts = ([prompt] if prompt else [])
    return ', '.join(missing + parts if placement == 'prefix' else parts + missing)


def prepare(prompt: str, params: dict, tags: list[str] | None = None,
            placement: str | None = None) -> dict:
    """冻结本任务的标签策略。params 里的 rating / prompt_family 由参数校验写入。

    显式给 tags 时（一句话出图在规划时冻结的那组）按给定的用，不再查表。
    """
    original = (prompt or '').strip()
    saved = params.get('prompt_defaults')
    # Seed variation keeps the exact prompt policy captured by the original job.
    if saved and original in (saved['original'], saved['effective']):
        policy = saved
    else:
        family = params.get('prompt_family') or 'qwen'
        rating = params.get('rating') or default_rating()
        if tags is None:
            tags = tags_for(family, rating)
        placement = placement or FAMILIES[family]['placement']
        effective = compile_prompt(original, list(tags), placement)
        # LoRA 触发词总在最后；提示词里已经写过的不重复
        triggers = list(params.get('lora_triggers') or [])
        if triggers:
            effective = compile_prompt(effective, triggers, 'suffix')
        policy = {'original': original, 'effective': effective,
                  'tags': list(tags), 'rating': rating, 'family': family,
                  'placement': placement}
        if triggers:
            policy['triggers'] = triggers
    return dict(params, prompt_defaults=policy)


def effective_prompt(prompt: str, params: dict) -> str:
    return prepare(prompt, params)['prompt_defaults']['effective']


def validate_length(prompt: str, params: dict, limit: int, errors: list[str], tags=None) -> dict:
    built = prepare(prompt, params, tags)
    if len(built['prompt_defaults']['effective']) > limit:
        errors.append(f'加上默认标签后的提示词最多 {limit} 字符，请缩短输入')
    return built


def describe() -> dict:
    """给 /api/v1/params 和表单用：每个尺度在每个模型族下会补什么。"""
    return {'ratings': RATINGS, 'default_rating': default_rating(),
            'families': {key: {'label': spec['label'], 'placement': spec['placement'],
                               'tags': spec['tags']} for key, spec in FAMILIES.items()},
            'model_families': MODEL_FAMILIES}
