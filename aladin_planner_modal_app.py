"""aladin 的第三个 Modal app：把一句自然语言需求变成一组可执行的图像生成指令。

- 模型：Qwen3.8-27B 的 abliterated 版（`OBLITERATUS/Qwen3.8-27B-OBLITERATED`，非 gated，
  Apache-2.0），取仓库里的 **Q5_K_M GGUF（19.54GB）**，用 llama.cpp 在 L40S 上跑。
- 为什么不用 vLLM + 动态 fp8：实测「加载 28.06GiB 花了 432.87 秒」（fp8 量化在加载时做），
  因此改用已量化 GGUF。实际 GGUF 延迟由真实探针记录，不把估算当作结果。
- 为什么镜像里编译 llama.cpp：llama-cpp-python 的官方 CUDA 轮子只到 0.2.66（2024），
  加载不了 2026 的 `qwen35` 架构。这里钉 v0.4.1 源码编译（Layer 由 Modal 缓存，只付一次）。
- JSON 用 llama.cpp server 的 `response_format: json_schema`（内部转 GBNF 语法）约束，
  采样阶段就排除非法结构；返回前再过一遍 `aladin/planner_schema.validate_plan`（夹紧越界值）。
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

import modal

MODEL_REPO = 'OBLITERATUS/Qwen3.8-27B-OBLITERATED'
MODEL_REVISION = 'a58c3b53b3ce71551eafde2ed5ec8df48e0f4ff8'
GGUF = 'Qwen3.8-27B-OBLITERATED-Q5_K_M.gguf'
GGUF_BYTES = 19535692832
LLAMA_TAG = 'v0.4.1'
MODEL_DIR = Path('/models/planner')
SERVER = 'http://127.0.0.1:8080'
CONTEXT = 12288
MAX_OUTPUT_TOKENS = 6000
SERVER_LOG = '/tmp/llama-server.log'
SERVER_BIN = '/opt/llama.cpp/build/bin/llama-server'


def _image() -> modal.Image:
    return (modal.Image.from_registry('nvidia/cuda:12.8.1-devel-ubuntu22.04',
                                      add_python='3.12')
            .apt_install('git', 'cmake', 'build-essential', 'libcurl4-openssl-dev')
            .env({'HF_HOME': '/models/planner/hf',
                  'PYTHONPATH': '/opt',
                  # 没有它链接会失败：libggml-cuda.so 需要 CUDA **driver** 的桩库
                  # （实测报 undefined reference to `cuGetErrorString`），
                  # devel 镜像把桩库放在 lib64/stubs，链接期必须能找到。
                  'LIBRARY_PATH': '/usr/local/cuda/lib64/stubs'})
            .run_commands(
                'git clone --quiet https://github.com/ggml-org/llama.cpp /opt/llama.cpp',
                f'git -C /opt/llama.cpp checkout --quiet {LLAMA_TAG}',
                # 链接期必须能按名字找到 CUDA driver 的桩库 libcuda.so：
                # 否则 libggml-cuda.so 会留下 cuGetErrorString 等未定义符号（实测）。
                # 间接依赖按 SONAME 找 libcuda.so.1；只在链接期用，构建后移除，不进入运行时搜索路径。
                'ln -sf libcuda.so /usr/local/cuda/lib64/stubs/libcuda.so.1',
                # 只编 L40S 的 sm_89：默认会为一大堆架构离线编译，慢好几倍
                'cmake -S /opt/llama.cpp -B /opt/llama.cpp/build -DGGML_CUDA=ON'
                ' -DLLAMA_CURL=OFF -DCMAKE_BUILD_TYPE=Release'
                ' -DCMAKE_CUDA_ARCHITECTURES=89'
                ' -DCMAKE_EXE_LINKER_FLAGS=-Wl,-rpath-link,/usr/local/cuda/lib64/stubs'
                ' -DCMAKE_SHARED_LINKER_FLAGS=-L/usr/local/cuda/lib64/stubs',
                'cmake --build /opt/llama.cpp/build --target llama-server -j 16',
                'rm /usr/local/cuda/lib64/stubs/libcuda.so.1')
            .uv_pip_install('huggingface-hub>=0.36')
            .add_local_file(Path(__file__).resolve(),
                            '/opt/aladin_planner_modal_app.py', copy=True)
            .add_local_file(Path(__file__).resolve().parent / 'aladin' / 'planner_schema.py',
                            '/opt/aladin/planner_schema.py', copy=True)
            .add_local_file(Path(__file__).resolve().parent / 'aladin' / 'settings.py',
                            '/opt/aladin/settings.py', copy=True))


app = modal.App('aladin-planner-v1', include_source=False)
cache = modal.Volume.from_name('aladin-planner-models-v1', create_if_missing=True)
results = modal.Volume.from_name('aladin-planner-results-v1', create_if_missing=True)


def revision() -> str:
    """本模块的 sha256：请求里钉它，避免新旧代码混用（与两个 worker 同一套做法）。"""
    root = Path(__file__).resolve().parent
    digest = hashlib.sha256()
    for name in ('aladin_planner_modal_app.py', 'aladin/planner_schema.py', 'aladin/settings.py'):
        digest.update((root / name).read_bytes())
    return digest.hexdigest()


def validate_request(request: dict) -> dict:
    from aladin import planner_schema as contract

    if not isinstance(request, dict):
        raise ValueError('Request must be an object')
    if request.get('plannerRevision') != revision():
        raise ValueError(
            'Planner revision mismatch; deploy the current planner first '
            '(request=' + str(request.get('plannerRevision'))[:16] +
            ' deployed=' + revision()[:16] + ')')
    brief = request.get('brief')
    if not isinstance(brief, str) or not brief.strip():
        raise ValueError('brief 不能为空')
    if len(brief) > contract.MAX_BRIEF_CHARS:
        raise ValueError(f'brief 太长（上限 {contract.MAX_BRIEF_CHARS} 字符）')
    preset = request.get('preset')
    if preset is not None and preset not in contract.PRESETS:
        raise ValueError('未知的预设')
    count = request.get('count')
    if count is not None and (type(count) is not int or not contract.MIN_IMAGES <= count <= contract.MAX_IMAGES):
        raise ValueError(f'count 需在 {contract.MIN_IMAGES}–{contract.MAX_IMAGES} 之间')
    if preset == 'manga' and count not in contract.MANGA_LAYOUTS:
        raise ValueError(f'漫画格数需在 {contract.MANGA_MIN}–{contract.MANGA_MAX} 之间')
    rating = request.get('rating')
    if rating is not None and rating not in contract.RATING_ORDER:
        raise ValueError('未知的尺度')
    key = request.get('key')
    if key is not None and (not isinstance(key, str) or not re.fullmatch(r'[a-f0-9]{64}', key)):
        raise ValueError('无效的结果 key')
    # 目标模型规格（尺寸、边界、默认值、LoRA 说明）由宿主给出；老请求没有它，按 Qwen 处理
    target = contract.validate_target(request.get('target'))
    contract.creative_spec(request.get('creative_spec'))
    return {'brief': brief.strip(), 'count': count, 'target': target, 'preset': preset, 'rating': rating}


def _tail(lines: int = 8) -> str:
    try:
        return ' / '.join(Path(SERVER_LOG).read_text(errors='replace').splitlines()[-lines:])
    except FileNotFoundError:
        return '(没有日志)'


def ensure_model() -> Path:
    """确保 GGUF 在 Volume 里。按精确字节数校验，避免半截文件被当成完整权重。"""
    target = MODEL_DIR / GGUF
    if target.is_file() and target.stat().st_size == GGUF_BYTES:
        print('model cache hit', flush=True)
        return target
    from huggingface_hub import hf_hub_download

    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    print(f'downloading {GGUF} ({GGUF_BYTES / 1e9:.2f} GB)', flush=True)
    hf_hub_download(repo_id=MODEL_REPO, filename=GGUF, revision=MODEL_REVISION,
                    local_dir=str(MODEL_DIR))
    if target.stat().st_size != GGUF_BYTES:
        raise RuntimeError(f'权重字节数不符：{target.stat().st_size} != {GGUF_BYTES}')
    cache.commit()
    return target


def start_server(model: Path):
    log = open(SERVER_LOG, 'w')
    process = subprocess.Popen(
        [SERVER_BIN, '-m', str(model), '--host', '127.0.0.1', '--port', '8080',
         '-ngl', '99', '-c', str(CONTEXT), '-np', '1', '--jinja'],
        stdout=log, stderr=subprocess.STDOUT)
    deadline = time.time() + 900
    while time.time() < deadline:
        try:
            urllib.request.urlopen(SERVER + '/health', timeout=2).read()
            log.close()
            return process
        except Exception:
            if process.poll() is not None:
                log.close()
                raise RuntimeError('llama-server 退出：' + _tail())
            time.sleep(1)
    process.kill()
    process.wait(timeout=10)
    log.close()
    raise RuntimeError('llama-server 未在 900s 内就绪：' + _tail())


def complete(messages: list[dict], schema: dict) -> str:
    """一次约束解码。JSON schema 由 llama.cpp 转成 GBNF，采样阶段就排除非法结构。"""
    body = json.dumps({
        'messages': messages,
        'temperature': 0.7,
        'top_p': 0.9,
        'max_tokens': MAX_OUTPUT_TOKENS,
        'chat_template_kwargs': {'enable_thinking': False},
        # llama.cpp（v0.4.1 tools/server/server-common.cpp）对 type=json_schema 只读
        # response_format.json_schema.schema；schema 直接放在 response_format 下会被**静默忽略**，
        # 约束解码不生效——之前每次规划都要靠「无法解析 → 重试」多跑一轮就是这个原因。
        'response_format': {'type': 'json_schema', 'json_schema': {'name': 'plan', 'schema': schema}},
    }).encode()
    request = urllib.request.Request(SERVER + '/v1/chat/completions', data=body,
                                     headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(request, timeout=900) as response:
            payload = json.loads(response.read())
    except urllib.error.HTTPError as error:
        detail = error.read().decode('utf-8', errors='replace')[:800]
        raise RuntimeError(f'llama-server 拒绝请求：HTTP {error.code} {detail}') from None
    return payload['choices'][0]['message']['content']


def _parse(text: str) -> tuple[dict, str]:
    """取出 JSON 对象。约束解码下几乎就是纯 JSON，这里只兜一层代码块/前后缀。"""
    candidate = (text or '').strip()
    try:
        return json.loads(candidate), candidate
    except ValueError:
        pass
    start, end = candidate.find('{'), candidate.rfind('}')
    if start != -1 and end > start:
        blob = candidate[start:end + 1]
        return json.loads(blob), blob
    raise ValueError('planner 没有输出可解析的 JSON: ' + candidate[:200])


@app.function(image=_image(), gpu='L40S', cpu=8, memory=32768, timeout=2400,
              startup_timeout=2400, retries=0, min_containers=0, max_containers=1,
              scaledown_window=2, volumes={'/models': cache, '/results': results})
def plan(request: dict) -> dict:
    """一句需求 → N 条图像指令。返回 JSON 原语（含改写记录与模型钉版）。"""
    from aladin import planner_schema as contract

    cleaned = validate_request(dict(request))
    brief, count, target = cleaned['brief'], cleaned['count'], cleaned['target']
    receipt = Path('/results') / request['key'] / 'plan.json' if request.get('key') else None
    if receipt is not None and receipt.is_file():
        saved = json.loads(receipt.read_text())
        if saved.get('request') == request:
            return saved['plan']
    started = time.time()
    model = ensure_model()
    manga = cleaned['preset'] == 'manga'
    if manga:
        rating = cleaned['rating']
        schema = contract.manga_schema(count, target, rating)
        messages = [{'role': 'system', 'content': contract.manga_system_prompt(target, count, rating)},
                    {'role': 'user', 'content': contract.manga_user_prompt(brief, count)}]
    else:
        schema = contract.plan_schema(count, target)
        messages = [{'role': 'system', 'content': contract.system_prompt(target)},
                    {'role': 'user', 'content': contract.user_prompt(brief, count)}]

    messages[-1]['content'] += contract.creative_instructions(request.get('creative_spec'))

    def harness(parsed: dict) -> tuple[list[dict], dict, list[str]]:
        if manga:
            return contract.validate_manga(parsed, count, target, cleaned['rating'])
        variants, notes = contract.validate_plan(parsed, count, target)
        return variants, {}, notes
    process = start_server(model)
    try:
        notes: list[str] = []
        raw = ''
        try:
            raw = complete(messages, schema)
            parsed, raw = _parse(raw)
            variants, story, notes = harness(parsed)
        except (ValueError, json.JSONDecodeError) as first_error:
            # 只重试一次，并把失败原因回灌给模型（约束解码下极少走到这里）
            retry_messages = list(messages)
            if raw.strip():
                retry_messages.append({'role': 'assistant', 'content': raw[:2000]})
            retry_messages.append({
                'role': 'user',
                'content': '上面的输出无法解析（' + str(first_error)[:120]
                           + '）。请只输出符合 schema 的 JSON。'})
            first_raw = raw
            raw = complete(retry_messages, schema)
            parsed, raw = _parse(raw)
            variants, story, notes = harness(parsed)
            # 记下第一次为什么失败：否则只知道「重试过」，查不出每次都要多跑一轮的原因
            notes = [f'首次输出无法解析，已重试一次（{type(first_error).__name__}: {str(first_error)[:160]}；'
                     f'输出 {len(first_raw)} 字符，结尾：{first_raw[-120:]!r}）'] + notes
        result = {**story, 'variants': variants, 'notes': notes, 'raw': raw,
                'model': MODEL_REPO, 'modelRevision': MODEL_REVISION, 'quantization': GGUF,
                'plannerRevision': revision(),
                'elapsedSeconds': round(time.time() - started, 2)}
        if receipt is not None:
            receipt.parent.mkdir(parents=True, exist_ok=True)
            temporary = receipt.with_suffix('.tmp')
            temporary.write_text(json.dumps({'request': request, 'plan': result}, ensure_ascii=False))
            temporary.replace(receipt)
            results.commit()
        return result
    finally:
        process.terminate()
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)


@app.function(image=_image(), timeout=300)
def planner_probe() -> dict:
    """运维用：报告 planner 侧的钉版与构建结果（不加载模型、不占 GPU）。"""
    from aladin import planner_schema

    data = Path(__file__).read_bytes()
    built = Path(SERVER_BIN).is_file()
    return {'revision': revision(),
            'sha256': hashlib.sha256(data).hexdigest(),
            'model': MODEL_REPO, 'modelRevision': MODEL_REVISION,
            'gguf': GGUF, 'ggufBytes': GGUF_BYTES, 'llamaTag': LLAMA_TAG,
            'llamaServerBuilt': built,
            'size_presets': sorted(planner_schema.settings.SIZE_PRESETS),
            'steps_bounds': list(planner_schema.settings.PARAM_LIMITS['steps'])}
