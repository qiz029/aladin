"""LoRA 目录：能用哪些、钉在哪个版本、怎么用。

只在宿主侧使用。容器只收「文件名 + sha256 + 强度」，不认识这张表——所以增删 LoRA、
调默认强度都**不需要重新部署 Modal**：改这里，再跑一次 `python -m aladin loras-sync`。

每项都钉死版本与 sha256：同名 LoRA 会更新，复现只能靠版本。
- Civitai：钉版本 ID，Volume 上命名为 `loras/civitai-<版本ID>.safetensors`，本机下载后上传。
- Hugging Face：钉仓库提交，命名为 `loras/hf-<sha256 前 16 位>.safetensors`，由对应 Modal app
  在容器里直接下载（大文件不经本机，门控仓库用 app 的 HF secret）。

字段：
- family：pony / anima（生图）、ltx23 / ltx25（视频），决定能配哪个底模（跨底模的 LoRA 会静默无效，必须挡住）
- kind：quality 画质 / slider 滑杆 / pose 体位视角 / concept 概念 / style 画风 / motion 运动 / camera 镜头
- strength：默认强度；range：允许区间（滑杆可以是负数）
- triggers：选中即自动补到提示词末尾的触发词；suggest：可选词，页面上做成可点的标签
- group：同组互斥（例如两个漫画生成器、多个体位）
- settings：选中时建议的采样参数（页面自动填，服务端不强制）
来源与建议值取自各 LoRA 作者在 Civitai 页面上的说明。
"""
from __future__ import annotations

import re

MAX_STACK = 6            # 叠太多会互相打架；页面在超过 4 个时提示
FAMILY_OF_MODEL = {'pony-realism-2.2': 'pony', 'anima-base-1.0': 'anima',
                   'ltx-2.3': 'ltx23', 'ltx-2.5': 'ltx25'}
VOLUME_OF_FAMILY = {'pony': 'aladin-pony-models-v1', 'anima': 'aladin-anima-models-v1',
                    'ltx23': 'aladin-ltx23-models-v1', 'ltx25': 'aladin-ltx25-models-v1'}
# HF 来源的 LoRA 由这些 app 的 fetch_lora 在容器里下载
APP_OF_FAMILY = {'ltx23': 'aladin-video-ltx23-v1', 'ltx25': 'aladin-video-ltx25-v1'}
KINDS = {'quality': '画质', 'slider': '滑杆', 'pose': '体位 / 动作', 'concept': '概念', 'style': '画风',
         'motion': '运动', 'camera': '镜头'}


def _lora(id, name, family, kind, model, version, sha256, size_mb, strength, range_,
          triggers=(), suggest=(), group=None, settings=None, notes=''):
    return dict(id=id, name=name, family=family, kind=kind, civitai_model=model,
                civitai_version=version, sha256=sha256, size_mb=size_mb,
                strength=strength, range=list(range_), triggers=list(triggers),
                suggest=list(suggest), group=group, settings=settings or {}, notes=notes,
                file=f'civitai-{version}.safetensors',
                url=f'https://civitai.com/models/{model}?modelVersionId={version}')


def _hf_lora(id, name, family, kind, repo, revision, path, sha256, size, strength, range_,
             triggers=(), suggest=(), group=None, notes=''):
    return dict(id=id, name=name, family=family, kind=kind, hf_repo=repo, hf_revision=revision,
                hf_path=path, bytes=size, sha256=sha256, size_mb=round(size / 2 ** 20),
                strength=strength, range=list(range_), triggers=list(triggers),
                suggest=list(suggest), group=group, settings={}, notes=notes,
                file=f'hf-{sha256[:16]}.safetensors',
                url=f'https://huggingface.co/{repo}/blob/{revision}/{path}')


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
    # ── LTX-2.3 视频 ────────────────────────────────────────────────────────
    # 多数作者没给强度，默认 0.8；动作类同组只在页面提示。
    _lora('ltx23-dr34ml4y', 'DR34ML4Y All-In-One NSFW v3', 'ltx23', 'pose', 1811313, 3082662,
          '79aa47aafc9079dfac59bf1ddef722bc1878f775e19887e67c534d7615347efc', 1851, 0.8, (0, 1.2),
          suggest=('m15510n4ry', 'bl0wj0b', 'd0ubl3_bj', 'd0gg1e', 'c0wg1rl', 'r3v3rs3_c0wg1rl'),
          group='pose', notes='最热门的全能 NSFW：用建议里的体位词切换（传教士 / 口交 / 双人口交 / 后入 / 女上 / 反向女上）'),
    _lora('ltx23-penile-praxis', 'Penile Praxis v4', 'ltx23', 'pose', 2332473, 2772932,
          '9e301d35a97dc8188a2287290e08283c7bccd29bc4deaf0f37f65ce98fc9a789', 1248, 0.8, (0, 1.2),
          suggest=('This video is in an anime style.', 'This video is in a realistic style.'),
          group='pose', notes='通用性爱动作，写实 / 动漫 / 卡通都行；无触发词'),
    _lora('ltx23-deepthroat', "Daring's Deepthroat", 'ltx23', 'pose', 2476698, 2784573,
          '99ef82279896f09973f841c554d2fec058f382df5e02680de41c30061cbbad07', 1106, 1.0, (0, 1.2),
          triggers=('LTXdeepthroat',), group='pose', notes='口交；作者建议第一段 1.0'),
    _lora('ltx23-riding-pov', 'Riding POV', 'ltx23', 'pose', 2446218, 2754108,
          'b037a2fae053ee5f1b50d57120fe4e6acb680f654cbdc1f0a01c5189e2393f57', 1286, 0.7, (0, 1.2),
          suggest=('Riding backshot animation', 'Riding frontshot animation',
                   'moaning and wet slapping sounds', 'heavy breathing'),
          group='pose', notes='女上位第一人称，带音频；作者建议 0.5–0.8'),
    _lora('ltx23-doggystyle', 'SexGod DoggyStyle v2.5', 'ltx23', 'pose', 2515124, 2869862,
          'bb3e1f10798c447503445840759c5ff3ce647f308c0d5d9ec2bce10bfd6c31bb', 2571, 0.8, (0, 1.2),
          group='pose', notes='后入；体积很大（接近完整微调）'),
    _lora('ltx23-pussyjob', 'Pussyjob v1.1', 'ltx23', 'pose', 2492660, 2814696,
          '57aecc68b5b123b965895ce282a23b8761ba431e84c9449859df6d87c4e6a871', 816, 0.6, (0, 1.2),
          triggers=('Pussy job',), group='pose', notes='磨蹭；图生视频作者建议 0.4–0.8'),
    _lora('ltx23-nudity', 'SexGod Female Nudity v2', 'ltx23', 'concept', 2308157, 2778606,
          '9e2d10d10e17626eb305c12a1779a21c2585c6ce225ecc45c1db2647042a1a46', 1176, 0.8, (0, 1.2),
          triggers=('LTXNUDES',), notes='女性裸体'),
    _lora('ltx23-better-breasts', 'Better Breasts', 'ltx23', 'concept', 2536021, 2850154,
          'd9ddf98ac7a326c0c36cfe115389693ab3fb50a59d13565be04d5f77448a8f62', 643, 1.0, (0, 1.2),
          notes='胸部晃动物理（图生视频）'),
    _lora('ltx23-nsfw-motion', 'Better NSFW motion', 'ltx23', 'motion', 2344781, 2778953,
          '9039bc5d11aed4092c3837c7e509e797f44f1277fd836caecc9b73718d287906', 1056, 0.7, (0, 1.2),
          notes='通用 NSFW 动作质量，写实 / 2D / 3D'),
    _lora('ltx23-physltx', 'PhysLTX v2', 'ltx23', 'motion', 2592090, 2952846,
          'c9dcde33448ec5ce9c1286806a27772c6275e2a7f6e4e7491f83cadcbab82c1d', 1286, 0.7, (0, 1.2),
          notes='撞击与回弹物理；作者说到 1.0 会影响音频'),
    _lora('ltx23-vbvr', 'VBVR reasoning v3', 'ltx23', 'motion', 2497207, 2848299,
          '150ec404e517562a8e0692f4a9401c740cc0199793dc608d168490ba2d958203', 463, 1.0, (0, 1.2),
          notes='更听提示词、动作更合理；NSFW 与普通内容都适用'),
    _lora('ltx23-nsfw-merge', 'Multi-purpose NSFW merge', 'ltx23', 'motion', 2310920, 2783583,
          '5e5eb50396860082038584fdaa7018b1fc9191e0d30f5eb93e0bb82593d8ad84', 2352, 0.7, (0, 1.2),
          notes='通用动作增强；作者说部分量化底模上会发灰'),
    _lora('ltx23-crisp', 'Crisp Enhance', 'ltx23', 'quality', 2535622, 2849716,
          '020529377ce07b1235d8050505dd38adc3bc9dc49a191f318a7ccf3921354053', 673, 0.8, (0, 1.2),
          notes='细节与锐度；可与 Soft Enhance 叠用'),
    _lora('ltx23-soft', 'Soft Enhance', 'ltx23', 'quality', 2535622, 2849706,
          '47681fb3c11a90d95f826838400c69d5dfe210721ea277cc7872f7c98903d12a', 336, 0.8, (0, 1.2),
          notes='柔和质感'),
    _lora('ltx23-camera', 'Camera Controls', 'ltx23', 'camera', 2622189, 2944022,
          '39b91feb9b46dcbe75f6e71af4b6716a996299ad53ec27b2bf37c962fbdf2f83', 322, 1.0, (0, 1.2),
          suggest=('the camera tilts up', 'the camera tilts down', 'the camera pans to the left',
                   'the camera pans to the right', 'the camera zooms in', 'the camera zooms out'),
          notes='在提示词里写镜头运动短语'),
    _hf_lora('ltx23-sulphur', 'Sulphur 2（NSFW 底模 LoRA）', 'ltx23', 'style', 'SulphurAI/Sulphur-2-base',
             '65587bbb38c700622f9434a16e732c8b280c115e', 'sulphur_lora_rank_768.safetensors',
             'b7151fc78066457a38153f3f1c899851c667527aa2108e39a7f4be3e3b5e4f2d', 10268001040,
             1.0, (0, 1.2), notes='Sulphur 2 NSFW 微调的 LoRA 形式（10GB），打开等于换成 Sulphur 底模'),
    # ── LTX-2.5 视频 ────────────────────────────────────────────────────────
    _lora('ltx25-cumouf', 'CUMOUF', 'ltx25', 'pose', 2846978, 3214563,
          '918a19314dfe0530ce3a36d9f28d4d37256cf10944b451f51178978e2a874b32', 102, 1.0, (0, 1.2),
          triggers=('CUMOUF',), group='pose', notes='口交收尾，带音频；作者确认在 2.5 上好用'),
    _lora('ltx25-pjslide', 'PJSLIDE', 'ltx25', 'pose', 2849416, 3217529,
          'b49ed23420faf239cf62cc44e2120ed46fea31ced9346e4ba1f4bb3eeafaa0eb', 102, 1.0, (0, 1.2),
          triggers=('PJSLIDE',), group='pose', notes='外阴摩擦（不插入），带音频'),
    _lora('ltx25-beanflk', 'BEANFLK', 'ltx25', 'pose', 2829335, 3192441,
          '8e915c0336c78ed47f65fe710848ae4b0917d0e3579a29fab55c32f1e371d1a5', 204, 1.5, (0, 2),
          triggers=('BEANFLK',), group='pose', notes='自慰，带音频；作者建议 1.4–1.7，起始图里手要已经在位置上'),
    _lora('ltx25-full-nude', 'Excellent Full Nude', 'ltx25', 'concept', 2917371, 3300517,
          '3a85b86cbe474be00ad76ba7bdbe64f15f12722cff02192e709fcd64cdcc77f1', 643, 0.8, (0, 1.2),
          triggers=('Excellent_Full_Nude',), notes='为 2.5 训练，单人女性全裸'),
    _lora('ltx25-pov-missionary', 'POV Missionary Bouncy', 'ltx25', 'pose', 2888493, 3265418,
          'f59a509692668255bb9aa7a964c09b9767fec3b640e38baaf542607838c645be', 643, 0.8, (0, 1.2),
          suggest=('POV missionary', 'bouncing breasts up and down', 'mouth open moaning'),
          group='pose', notes='为 2.5 训练；作者第一次练，效果未知'),
    _lora('ltx25-finishes', 'Intense Fullbody Finishes', 'ltx25', 'pose', 2927084, 3312346,
          'e4918f6a2b4ceaaed250a2ceade40bae26981dd90ec973514bc4caa2a6727dff', 643, 0.8, (0, 1.2),
          suggest=('moans', 'throat sounds'), group='pose', notes='高潮收尾，能控制声音'),
    _lora('ltx25-3d-animation', 'LTX-2.5 3D Animation', 'ltx25', 'style', 2895989, 3273961,
          'ce2a0d7ad144df27b8487d9ada4a3d5a8f99ce1ac53066c76cb1b991fd7182f7', 643, 0.4, (0, 1.2),
          triggers=('3dsrx',), group='style', notes='3D 动画电影风格；作者建议图生视频 0.4、文生视频 0.8'),
    _hf_lora('ltx25-cinemagraph', 'Cinemagraph（官方）', 'ltx25', 'style',
             'Lightricks/LTX-2.5-22b-LoRA-Cinemagraph', '8e5e88efcf59ff0efa6f9dcf7164c7a7d72c4022',
             'ltx-2.5-22b-lora-cinemagraph-0.9.safetensors',
             'fcd06225e1646080cbb21958a25c9f339ee484519e798bfb00c9b91022cfccfa', 201462650,
             1.0, (0, 1.2), triggers=('CINEMAGRAPH_MOTION',), group='style',
             notes='局部动的动态照片；作者建议 1.0–1.2'),
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
        return [], ['这个模型不支持 LoRA（生图只有 Pony 与 Anima；视频是 LTX-2.3 / LTX-2.5）']
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
                         'version': item.get('civitai_version') or item['hf_revision'], 'file': item['file'],
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


FILE_PATTERN = re.compile(r'^(civitai-\d+|hf-[0-9a-f]{16})\.safetensors$')
