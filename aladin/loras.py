"""LoRA 目录：能用哪些、钉在哪个版本、怎么用。

只在宿主侧使用。容器只收「文件名 + sha256 + 强度」，不认识这张表——所以增删 LoRA、
调默认强度都**不需要重新部署 Modal**：改这里，再跑一次 `python -m aladin loras-sync`。

每项都钉死 Civitai 的版本 ID 与 sha256：同名 LoRA 会更新，复现只能靠版本。
文件在 Volume 上统一命名为 `loras/civitai-<版本ID>.safetensors`，与上游文件名无关。

字段：
- family：pony / anima，决定能配哪个底模（跨底模的 LoRA 会静默无效，必须挡住）
- kind：quality 画质 / slider 滑杆 / pose 体位视角 / concept 概念 / style 画风
- strength：默认强度；range：允许区间（滑杆可以是负数）
- triggers：选中即自动补到提示词末尾的触发词；suggest：可选词，页面上做成可点的标签
- group：同组互斥（例如两个漫画生成器、多个体位）
- settings：选中时建议的采样参数（页面自动填，服务端不强制）
来源与建议值取自各 LoRA 作者在 Civitai 页面上的说明。
"""
from __future__ import annotations

import re

MAX_STACK = 6            # 叠太多会互相打架；页面在超过 4 个时提示
FAMILY_OF_MODEL = {'pony-realism-2.2': 'pony', 'anima-base-1.0': 'anima'}
VOLUME_OF_FAMILY = {'pony': 'aladin-pony-models-v1', 'anima': 'aladin-anima-models-v1'}
KINDS = {'quality': '画质', 'slider': '滑杆', 'pose': '体位 / 视角', 'concept': '概念', 'style': '画风'}


def _lora(id, name, family, kind, model, version, sha256, size_mb, strength, range_,
          triggers=(), suggest=(), group=None, settings=None, notes=''):
    return dict(id=id, name=name, family=family, kind=kind, civitai_model=model,
                civitai_version=version, sha256=sha256, size_mb=size_mb,
                strength=strength, range=list(range_), triggers=list(triggers),
                suggest=list(suggest), group=group, settings=settings or {}, notes=notes,
                file=f'civitai-{version}.safetensors',
                url=f'https://civitai.com/models/{model}?modelVersionId={version}')


CATALOG = [
    # ── Pony（Pony Realism 2.2）──────────────────────────────────────────────
    _lora('pony-realism-enhancer', 'Pony Realism Enhancer', 'pony', 'quality', 927305, 1439429,
          '044e22cf6cd3d20a8e3b2e57b887662166069fc8d701bffe4533a12a246c8336', 650, 0.7, (0, 1.2),
          notes='在 Pony Realism v2.2 上训练，与本模型最对口；作者建议 0.5–0.9'),
    _lora('real-skin-slider', 'Real Skin Slider', 'pony', 'slider', 1486921, 1681921,
          '229fd1d9a157dc9ad2438a860790bec8019abbc822b7fd788dddf66ef701d3db', 5, 2.0, (-2, 4),
          notes='皮肤质感；作者建议 2–3'),
    _lora('pony-amateur', 'Pony Amateur', 'pony', 'style', 480835, 717403,
          'e85ce7ea7ff86b1c242cead714d7315fea8972e66bcbd13bd2374561746d58be', 435, 0.8, (0, 1.2),
          suggest=('photo', 'grainy', 'amateur', 'lowres', '2000s nostalgia', 'webcam photo', 'flash'),
          notes='随手拍 / 业余照片质感，弱化 AI 味'),
    _lora('breast-size-slider', 'Breast Size Slider', 'pony', 'slider', 481016, 534952,
          'a8588f4530bef4cee49ee483a5d4e90d9dc1679f4690af6195c26e92a3bb03b9', 8, 1.0, (-3, 3),
          notes='正值变大、负值变小'),
    _lora('penis-size-slider', 'Penis Size Slider', 'pony', 'slider', 465379, 517898,
          'b217fe8ca23d7fdac559c2e9581c40bb0171c24b1c0a31ca5af61e27894fc55b', 8, 1.0, (-3, 3),
          notes='正值变大、负值变小；作者说小幅调整时变化不明显'),
    _lora('mating-press', 'Mating press', 'pony', 'pose', 90532, 722613,
          'a3d47ff9e258ef282909899087e1d65e48c425f0fda2b8325788b9177fc4d1c8', 55, 0.8, (0, 1.2),
          triggers=('mating press',), group='pose'),
    _lora('cowgirl-third-person', 'Cowgirl Position（第三人称）', 'pony', 'pose', 145887, 510266,
          'b4629b181343eea72345de85557699108fb1e9c56bcc019f6f4d087740304415', 218, 0.8, (0, 1.2),
          triggers=('cowgirl position', 'girl on top'), suggest=('1boy', 'sex', 'straddling'), group='pose'),
    _lora('female-pov', 'Female POV', 'pony', 'pose', 427349, 960900,
          '94db3709d5c3c510428479ed984f7278e99d736861a70075a1c8ac24db5482e4', 218, 0.8, (0, 1.2),
          triggers=('f3mp0v',), group='pose', notes='女性第一人称视角'),
    _lora('uncensored-ponyxl', 'Uncensored PonyXL', 'pony', 'concept', 339269, 416689,
          '630bef63a59b6b501bc836504ceb1c112e30106a1562873b63697acc2804d4c1', 109, 0.7, (0, 1.2),
          suggest=('spread pussy', 'clitoris', 'close-up'), notes='私密部位细节'),
    _lora('cumtrail', 'Pull out Cumtrail', 'pony', 'concept', 467896, 1130602,
          '3c7a695ce32926cf3f4e09ca897d285a1167e269af4042cb77ed0839ce8032f8', 218, 0.8, (0, 1.2),
          triggers=('cumtrail',),
          suggest=('cum string', 'pulling out penis', 'cumtrail vagina to penis', 'cumtrail mouth to penis',
                   'cumtrail breast to penis', 'after sex')),
    _lora('style-photo-2', 'Not Artists Styles · Photo 2', 'pony', 'style', 264290, 363388,
          '979cbe2ec41f4870d5b27b2badbd25d994c907a93c4ae74c177287289de7dafa', 218, 0.8, (0, 1.2),
          triggers=('raw', 'photo', 'realistic'), group='style',
          notes='用真实照片（Unsplash）训练，写实首选；作者建议 0.8–1'),
    _lora('style-photo', 'Not Artists Styles · Photo', 'pony', 'style', 264290, 300686,
          'bd9a91aa94241890c5c086383a62a82478a0e1511fa99383a20802838d285119', 218, 0.8, (0, 1.2),
          triggers=('realistic',), group='style'),
    _lora('style-smooth-anime', 'Not Artists Styles · Smooth Anime', 'pony', 'style', 264290, 298238,
          'cb65a31c8b4a741cee298d4291ff0ff8f4dbed933b2b503629ee4287a8e631d5', 218, 0.8, (0, 1.2),
          group='style', notes='平滑动漫，这组风格里下载最多；会把写实底模拉向动漫'),
    _lora('style-smooth-anime-2', 'Not Artists Styles · Smooth Anime 2', 'pony', 'style', 264290, 333607,
          '0a83e3d6a93e494d44fb55ac87461502d5a705489ca5cd6b048988152aaeb6ba', 218, 0.8, (0, 1.2),
          group='style', notes='更新的一版平滑动漫'),
    _lora('style-anime-2', 'Not Artists Styles · Anime 2', 'pony', 'style', 264290, 333590,
          'fa51b1e29716b9cac1f8d9e9c9a08feefc9abd7239748e8ddc757d3f1eb4649e', 218, 0.8, (0, 1.2),
          group='style', notes='纯动漫'),
    _lora('expressiveh', 'ExpressiveH', 'pony', 'style', 341353, 382152,
          '7b53da0391712dbe9412d5aa5fd074659413058773f72f41bd7a37e886031d43', 218, 0.6, (0, 1.2),
          triggers=('Expressiveh',), group='style', notes='里番动画画风，强烈拉向动漫；配写实底模要低强度'),
    # ── Anima ──────────────────────────────────────────────────────────────
    _lora('anima-turbo', 'Anima Turbo LoRA', 'anima', 'quality', 2560840, 2979642,
          '1b55e40bdb1d0e5a78cb498f245fccfdaae97823265db957d2aabdcf4cd3caf1', 142, 1.0, (0, 1),
          settings={'cfg': 1.0, 'steps': 10, 'sampler': 'euler'},
          notes='步数与 CFG 蒸馏：CFG 1、8–12 步；作者说 er_sde 采样器容易糊，选中时自动改用 euler'),
    _lora('zoda-nsfw-detailer', 'Zoda NSFW Detailer', 'anima', 'quality', 1879987, 3130764,
          'ecfbc656b6dc58355089522ada330741d9d581f24391a6c12b325457aa01969c', 66, 1.0, (0, 1.8),
          notes='露骨部位细节；作者建议 Anima 30–45 步'),
    _lora('hentai-studio-quality', 'Hentai Studio Quality', 'anima', 'quality', 1459030, 2814440,
          '4f36cc55371b1adc1a627c2fce0bfda8e648c76fc4ed3f5e278cb6d7f17952b2', 132, 0.8, (0, 1.2),
          triggers=('hentai_studio_quality',), suggest=('shiny skin',)),
    _lora('mature-slider', 'Mature slider', 'anima', 'slider', 2118055, 2979797,
          'b5d0cf524f47e16abeae3239a3ef17cb3bd996217589ae628f0239ed142ed116', 12, 2.0, (0, 6),
          notes='让角色显得更成熟；作者说 0–6 都可用'),
    _lora('hentai-comic-color', 'Hentai Comic Generator（彩色）', 'anima', 'concept', 585589, 3008158,
          'bc242c306f324425814bebd3361b759bc9aaa77d70659521cfd755a42d27c4d9', 66, 0.9, (0, 1.2),
          triggers=('hentai comic style', 'multi-view', 'speech bubbles'),
          suggest=('focus lines', 'sound effects', 'comic-style facial expressions', 'cowboy shot'),
          group='comic', notes='一张图生成 3–5 格漫画页'),
    _lora('hentai-comic-hardcore', 'Hentai Comic Generator（硬核）', 'anima', 'concept', 588622, 3014282,
          '3e4c549ab16774fb0ba3933c7f49f01ce2561babe4b29cd2023cb681dc2c7cd1', 66, 0.9, (0, 1.2),
          triggers=('hentai comic style', 'multi-view', 'speech bubbles'),
          suggest=('focus lines', 'sound effects', 'comic-style facial expressions'),
          group='comic', notes='同上，内容更露骨'),
    _lora('anime-in-real', 'Anime in real', 'anima', 'style', 1979448, 3202009,
          'f517e791fadba65eaa6e90fcb29984c26f11d1ce094c5ed38c14cb2aec043d82', 264, 0.8, (0, 1.2),
          triggers=('@Ani3rel',), group='style', notes='动漫角色真人化风格（免费的 Anima 版）'),
    _lora('buanime-nsfw', 'BuAnime NSFW Style Pack', 'anima', 'style', 2645819, 3301514,
          '91b6b168339086e0750387e198435dc49c13a99971f4597d5c95c1d3f7d910b7', 132, 0.8, (0, 1.2),
          group='style', notes='V2，无需触发词'),
]
BY_ID = {item['id']: item for item in CATALOG}

# 内置预设；用户自己的预设存在浏览器里
PRESETS = [
    dict(id='pony-real-base', family='pony', name='写实底子',
         loras=[dict(id='pony-realism-enhancer', strength=0.7), dict(id='real-skin-slider', strength=2.0)]),
    dict(id='pony-amateur', family='pony', name='随手拍质感',
         loras=[dict(id='pony-realism-enhancer', strength=0.6), dict(id='pony-amateur', strength=0.8),
                dict(id='real-skin-slider', strength=1.5)]),
    dict(id='pony-photo', family='pony', name='照片风格',
         loras=[dict(id='style-photo-2', strength=0.8), dict(id='real-skin-slider', strength=2.0)]),
    dict(id='anima-fast', family='anima', name='快速出图',
         loras=[dict(id='anima-turbo', strength=1.0)]),
    dict(id='anima-hentai', family='anima', name='里番质感',
         loras=[dict(id='hentai-studio-quality', strength=0.8), dict(id='zoda-nsfw-detailer', strength=1.0),
                dict(id='mature-slider', strength=2.0)]),
]


def family_of(model: str | None) -> str | None:
    return FAMILY_OF_MODEL.get(model or '')


def public_catalog() -> dict:
    """给页面和 /api/v1/loras：目录、预设、限制。"""
    # kinds 用有序列表：模板的 tojson 会按键名排序，字典的顺序到不了页面
    return {'loras': CATALOG, 'presets': PRESETS, 'kinds': [[k, v] for k, v in KINDS.items()],
            'max_stack': MAX_STACK,
            'families': {family: [m for m, f in FAMILY_OF_MODEL.items() if f == family]
                         for family in VOLUME_OF_FAMILY}}


def resolve(choices, model: str) -> tuple[list[dict], list[str]]:
    """把用户的选择 [{id, strength}] 校验并展开成任务记录用的完整条目。

    返回 (条目列表, 错误)。条目带版本、文件、sha256——任务靠它精确复现，目录以后改了也不受影响。
    """
    if not choices:
        return [], []
    errors: list[str] = []
    family = family_of(model)
    if family is None:
        return [], ['这个模型不支持 LoRA（目前只有 Pony 与 Anima）']
    if not isinstance(choices, list):
        return [], ['loras 必须是数组']
    if len(choices) > MAX_STACK:
        errors.append(f'最多叠加 {MAX_STACK} 个 LoRA')
    resolved, seen, groups = [], set(), {}
    for choice in choices:
        if not isinstance(choice, dict) or not isinstance(choice.get('id'), str):
            errors.append('每个 LoRA 需要 id')
            continue
        item = BY_ID.get(choice['id'])
        if item is None:
            errors.append(f"未知的 LoRA：{choice['id']}")
            continue
        if item['family'] != family:
            errors.append(f"{item['name']} 只能配 {item['family']} 底模")
            continue
        if item['id'] in seen:
            errors.append(f"{item['name']} 重复了")
            continue
        seen.add(item['id'])
        strength = choice.get('strength', item['strength'])
        low, high = item['range']
        if isinstance(strength, bool) or not isinstance(strength, (int, float)) \
                or not low <= strength <= high:
            errors.append(f"{item['name']} 的强度需在 {low}–{high} 之间")
            continue
        if item['group'] in ('comic',):          # 真正互斥的只挡住；体位/画风叠加只在页面提示
            if item['group'] in groups:
                errors.append(f"{item['name']} 与 {groups[item['group']]} 不能同时使用")
                continue
            groups[item['group']] = item['name']
        resolved.append({'id': item['id'], 'name': item['name'],
                         'version': item['civitai_version'], 'file': item['file'],
                         'sha256': item['sha256'], 'strength': round(float(strength), 3)})
    return resolved, errors


def triggers_for(resolved: list[dict]) -> list[str]:
    """选中的 LoRA 需要的触发词，按选择顺序去重。"""
    words: list[str] = []
    for entry in resolved:
        for word in BY_ID[entry['id']]['triggers']:
            if word.casefold() not in {w.casefold() for w in words}:
                words.append(word)
    return words


FILE_PATTERN = re.compile(r'^civitai-\d+\.safetensors$')
