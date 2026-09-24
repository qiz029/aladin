"""Text-to-image model contracts, shared by UI, API and the isolated workers."""
DEFAULT_MODEL = 'qwen-image-2.1'
COMFY_REVISION = 'c194dd00cd42aa18d9dbf27d977bf6b85d9ea565'

def sizes(portrait=(832, 1216)):
    return {k: dict(label=l, hint=f'{w}×{h}', width=w, height=h)
            for k, l, w, h in [('square', '方形', 1024, 1024),
                              ('portrait', '竖版', *portrait),
                              ('landscape', '横版', *reversed(portrait))]}

MODELS = {
    DEFAULT_MODEL: dict(label='Qwen-Image 2.1 UC', app='aladin-image-v1',
        defaults=dict(size='square', negative='', steps=25, cfg=1.0, sampler='euler', scheduler='simple', seed=None),
        sizes=sizes((768, 1152)), steps=(1, 40),
        samplers=['euler', 'euler_ancestral', 'dpmpp_2m', 'dpmpp_2m_sde'], schedulers=['simple', 'normal', 'beta'],
        hint='擅长自然语言与画面文字。低 CFG 模型，建议从 1 开始。'),
    'anima-base-1.0': dict(label='Anima Base 1.0', app='aladin-anima-image-v1',
        defaults=dict(size='square', negative='worst quality, low quality, score_1, score_2, score_3, artist name, blurry, jpeg artifacts, chromatic aberration', steps=35, cfg=4.5, sampler='er_sde', scheduler='simple', seed=None),
        sizes=sizes(), steps=(1, 50),
        samplers=['er_sde', 'euler_ancestral', 'dpmpp_2m_sde_gpu', 'euler'], schedulers=['simple', 'normal', 'beta'],
        hint='动漫 / 插画基础模型。建议 30–50 步、CFG 4–5；可在提示词中加入 masterpiece, best quality, score_7。',
        repo='circlestone-labs/Anima', revision='f973fc41ec7545364ac9776c2440285f43ff2a30',
        files=[('split_files/diffusion_models/anima-base-v1.0.safetensors', 'diffusion_models/anima-base-v1.0.safetensors', 4182218328, 'bd43b7cffe1ed1153d9c41e7beb2f18cb1273eafbaa3af3edd6a173dc90a006e'),
               ('split_files/text_encoders/qwen_3_06b_base.safetensors', 'text_encoders/qwen_3_06b_base.safetensors', 1192135096, 'cd2a512003e2f9f3cd3c32a9c3573f820bb28c940f73c57b1ddaa983d9223eba'),
               ('split_files/vae/qwen_image_vae.safetensors', 'vae/qwen_image_vae.safetensors', 253806246, 'a70580f0213e67967ee9c95f05bb400e8fb08307e017a924bf3441223e023d1f')]),
    'pony-realism-2.2': dict(label='Pony Realism 2.2', app='aladin-pony-image-v1',
        defaults=dict(size='square', negative='score_4, score_5, score_6', steps=30, cfg=6.5, sampler='dpmpp_2m_sde', scheduler='karras', seed=None),
        sizes=sizes(), steps=(1, 50), clip_skip=2,
        samplers=['dpmpp_2m_sde', 'dpmpp_2s_ancestral', 'dpmpp_sde', 'euler_ancestral'], schedulers=['karras', 'exponential', 'normal'],
        hint='写实 Pony / SDXL，内置 VAE，固定 Clip Skip 2。建议 CFG 6–7、30 步以上；标签式提示词可加入 score_9, score_8_up, score_7_up。',
        repo='Ine007/ponyRealism_v22MainVAE', revision='8920738fb34a8b286d80a8af65e37fc41786d88a',
        files=[('ponyRealism_v22MainVAE.safetensors', 'checkpoints/ponyRealism_v22MainVAE.safetensors', 7105348856, '7c97ecf786a50a54835a22277c35703787b840e98c04c318a4e3fef9d3b463f7')]),
}

def public_models():
    return {key: {**{k: v for k, v in spec.items() if k not in ('files', 'app')},
                  'defaults': dict(spec['defaults'], model=key)} for key, spec in MODELS.items()}
