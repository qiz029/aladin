#!/usr/bin/env python3
"""aladin 的薄 CLI：提交任务、等完成、取产物。

只用标准库，任何 python3 都能跑——省掉让 agent 手拼 curl 和 JSON 引号。
产物落在本地磁盘，服务端只回状态。
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import urlencode
from pathlib import Path

def _dotenv(key: str) -> str | None:
    """从仓库根目录的 .env 读一个键（skill 可能被软链到别处，按真实路径找仓库）。

    只做最简单的 KEY=VALUE 解析，不执行任何东西；.env 不入库，服务地址等机器相关配置都放那里。
    """
    path = Path(__file__).resolve().parents[4] / '.env'
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return None
    for line in lines:
        name, sep, value = line.strip().partition('=')
        if sep and name.strip() == key and not name.startswith('#'):
            return value.strip().strip('\'"') or None
    return None


BASE = (os.environ.get('ALADIN_URL') or _dotenv('ALADIN_URL')
        or 'http://127.0.0.1:8765').rstrip('/')
TERMINAL = ('succeeded', 'failed')


def lora_choices(specs: list[str]) -> list[dict]:
    """--lora id 或 id:强度 → API 的 loras 字段。"""
    return [dict(id=spec.split(':', 1)[0],
                 **({'strength': float(spec.split(':', 1)[1])} if ':' in spec else {}))
            for spec in specs]


def call(method: str, path: str, body: dict | None = None, timeout: int = 60,
         form: bool = False):
    """返回 (状态码, 解析后的 body)。HTTP 错误不抛异常，交给调用方判断。

    form=True 按表单编码发送（POST /api/v1/gallery 收的是表单字段，不是 JSON）。
    """
    if body is not None and form:
        data = urlencode(body).encode()
        headers = {'Content-Type': 'application/x-www-form-urlencoded'}
    else:
        data = json.dumps(body).encode() if body is not None else None
        headers = {'Content-Type': 'application/json'} if data else {}
    request = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read()
            return response.status, (json.loads(payload) if payload else None)
    except urllib.error.HTTPError as error:
        payload = error.read()
        try:
            return error.code, json.loads(payload)
        except ValueError:
            return error.code, payload.decode(errors='replace')
    except urllib.error.URLError as error:
        return 0, f'连不上 {BASE}：{error.reason}'


def fail(status: int, body) -> int:
    detail = body.get('detail') if isinstance(body, dict) else body
    if isinstance(detail, list):
        detail = '；'.join(str(item) for item in detail)
    print(f'失败（HTTP {status}）：{detail}', file=sys.stderr)
    return 1


def wait(job_id: str, every: int = 5) -> dict:
    """轮询到终态，边等边把进度打到 stderr（stdout 留给机器读）。"""
    seen = None
    while True:
        status, body = call('GET', f'/api/v1/jobs/{job_id}', timeout=30)
        if status != 200:
            # 保留 id：调用方还要靠它继续 status 查询，show 也不会 KeyError
            return {'id': job_id, 'state': 'unknown', 'error': body}
        progress = body.get('progress') or {}
        mark = progress.get('percent')
        # 只在真正有采样进度时打印，终态那一步没有 percent，打了反而像出错
        if mark != seen and progress.get('step'):
            seen = mark
            print(f'  [{mark}%] 第 {progress.get("image")}/{progress.get("total")} 张 · '
                  f'采样 {progress.get("step")}/{progress.get("max")} 步', file=sys.stderr)
        if body['state'] in TERMINAL:
            return body
        time.sleep(every)


def download(job: dict, out_dir: Path) -> list[Path]:
    saved = []
    target = out_dir / job['id']
    target.mkdir(parents=True, exist_ok=True)
    for item in job.get('artifacts', []):
        # 名字来自服务端，归一化掉任何路径成分，别让它写到目录外
        name = Path(item['name']).name
        try:
            # 产物是二进制，不能走会做 JSON 解析的 call()
            request = urllib.request.Request(BASE + item['url'])
            with urllib.request.urlopen(request, timeout=120) as response:
                data = response.read()
        except (urllib.error.HTTPError, urllib.error.URLError) as error:
            # 一个产物拉不到不该让整条命令崩掉，报出来继续
            print(f'  跳过 {name}：{error}', file=sys.stderr)
            continue
        path = target / name
        if path.is_file() and path.stat().st_size == len(data):
            saved.append(path)          # 内容一模一样，不重写
            continue
        path.write_bytes(data)
        saved.append(path)
    manifest = target / 'generation.json'
    manifest.write_text(json.dumps(job, ensure_ascii=False, indent=2))
    saved.append(manifest)
    return saved


def show(job: dict, out_dir: Path | None) -> int:
    print(f"job {job.get('id', '?')}")
    print(f"state {job['state']}")
    if job.get('error'):
        print(f"error {job['error']}")
    if job['state'] != 'succeeded':
        for item in job.get('artifacts', []):
            print(f"artifact {item['url']}")
        return 1
    if out_dir is None:
        for item in job.get('artifacts', []):
            print(f"artifact {item['url']}")
        return 0
    for path in download(job, out_dir):
        print(f"file {path}")
    return 0


def main() -> int:
    global BASE

    parser = argparse.ArgumentParser(description='aladin 图片生成服务')
    parser.add_argument('--url', help=f'服务地址（默认 {BASE}，也可用 ALADIN_URL）')
    sub = parser.add_subparsers(dest='command', required=True)

    submit = sub.add_parser('submit', help='文生图')
    submit.add_argument('prompt')
    submit.add_argument('--size', default='square', help='square / portrait / landscape')
    submit.add_argument('--images', type=int, default=1)
    submit.add_argument('--steps', type=int, default=None)
    submit.add_argument('--model', default='qwen-image-2.1', choices=('qwen-image-2.1', 'anima-base-1.0', 'pony-realism-2.2'))
    submit.add_argument('--cfg', type=float)
    submit.add_argument('--sampler')
    submit.add_argument('--scheduler')
    submit.add_argument('--seed', type=int, default=None, help='省略为随机')
    submit.add_argument('--negative', default=None)
    submit.add_argument('--lora', action='append', default=[], metavar='ID[:STRENGTH]',
                        help='叠加 LoRA（仅 Pony / Anima），可重复；可选 id 见 GET /api/v1/loras')
    submit.add_argument('--rating', choices=('general', 'suggestive', 'explicit'),
                        help='尺度：日常 / 暗示 / 露骨；省略用服务端默认')
    submit.add_argument('--wait', action='store_true')
    submit.add_argument('--out', default='aladin-out', help='产物落盘目录')

    director = sub.add_parser('director', help='一句话规划并生成图片')
    director.add_argument('brief')
    director.add_argument('--count', type=int, default=None)
    director.add_argument('--must-keep', default='', help='必须保持的设定，最多 1000 字符')
    director.add_argument('--may-change', default='', help='允许变化的设定，最多 1000 字符')
    director.add_argument('--change-only', default='', help='本轮只修改哪些内容，最多 1000 字符')
    director.add_argument('--rating', choices=('general', 'suggestive', 'explicit'),
                        help='尺度：日常 / 暗示 / 露骨；省略用服务端默认')
    director.add_argument('--model', default=None,
                          choices=('qwen-image-2.1', 'anima-base-1.0', 'pony-realism-2.2'),
                          help='目标模型；planner 按它的写法写提示词')
    director.add_argument('--lora', action='append', default=[], metavar='ID[:STRENGTH]',
                          help='整批共用的 LoRA（仅 Pony / Anima），可重复')
    director.add_argument('--review', action='store_true',
                          help='规划完先停下，用 approve 命令确认后才生成')
    director.add_argument('--preset', choices=('manga',), default=None,
                          help='manga：日式漫画一页多格（--count 为格数 4–8，默认 6）')
    director.add_argument('--wait', action='store_true')

    approve = sub.add_parser('approve', help='确认等待中的一句话出图计划（原样确认）')
    approve.add_argument('job_id')
    director.add_argument('--out', default='aladin-out')

    edit = sub.add_parser('edit', help='改图（图生图 / 指令编辑）')
    edit.add_argument('--image', required=True)
    edit.add_argument('--mode', default='img2img', choices=('img2img', 'edit'))
    edit.add_argument('--prompt', default='')
    edit.add_argument('--images', type=int, default=1)
    edit.add_argument('--steps', type=int, default=20)
    edit.add_argument('--denoise', type=float, default=0.6)
    edit.add_argument('--region', type=float, nargs=4, metavar=('LEFT', 'TOP', 'RIGHT', 'BOTTOM'), help='edit 局部修复区域，0–1 坐标')
    edit.add_argument('--seed', type=int, default=None, help='省略为随机')
    edit.add_argument('--rating', choices=('general', 'suggestive', 'explicit'),
                        help='尺度：日常 / 暗示 / 露骨；省略用服务端默认')
    edit.add_argument('--wait', action='store_true')
    edit.add_argument('--out', default='aladin-out')

    status = sub.add_parser('status', help='查任务')
    status.add_argument('job_id')
    status.add_argument('--out', default=None)

    remove = sub.add_parser('delete', help='删除已结束的任务及其本地产物（收藏副本保留）')
    remove.add_argument('job_id')

    listing = sub.add_parser('jobs', help='任务列表')
    listing.add_argument('--limit', type=int, default=10)
    listing.add_argument('--state', default=None)

    gallery = sub.add_parser('gallery', help='图库：列表/检索、收藏、标签与星级')
    gallery.add_argument('--add', nargs=2, metavar=('JOB_ID', 'NAME'))
    gallery.add_argument('--remove', type=int, metavar='ITEM_ID')
    gallery.add_argument('--tags', nargs=2, metavar=('ITEM_ID', 'TAGS'),
                         help='整体替换标签，逗号分隔；传空字符串清空')
    gallery.add_argument('--stars', nargs=2, type=int, metavar=('ITEM_ID', 'N'), help='星级 0–5')
    gallery.add_argument('--q', default='', help='搜索提示词')
    gallery.add_argument('--tag', default='')
    gallery.add_argument('--model', default='')
    gallery.add_argument('--kind', default='', choices=('', 'image', 'video'))
    gallery.add_argument('--min-stars', type=int, default=0)
    gallery.add_argument('--sort', default='newest', choices=('newest', 'oldest', 'stars'))

    params = sub.add_parser('params', help='参数边界与预设')

    video = sub.add_parser('video', help='图生视频（LTX-2.5 / LTX-2.3，几秒钟的有声 webm）')
    video.add_argument('--image', required=True, help='起始图：画面内容由它决定')
    video.add_argument('--prompt', default='', help='描述想要的动作、镜头与声音，可留空')
    video.add_argument('--model', default='ltx-2.5', choices=('ltx-2.5', 'ltx-2.3'),
                       help='ltx-2.5 画质更好 / ltx-2.3 NSFW LoRA 更多')
    video.add_argument('--duration', default='normal', choices=('short', 'normal', 'long'),
                       help='short 2s / normal 5s / long 8s')
    video.add_argument('--size', default='landscape',
                       help='landscape / portrait / landscape-hd / portrait-hd')
    video.add_argument('--lora', action='append', default=[], metavar='ID[:STRENGTH]',
                       help='叠加 LoRA（按 --model 分族），可重复；可选 id 见 GET /api/v1/loras')
    video.add_argument('--seed', type=int, default=None, help='省略为随机')
    video.add_argument('--rating', choices=('general', 'suggestive', 'explicit'),
                        help='尺度：日常 / 暗示 / 露骨；省略用服务端默认')
    video.add_argument('--wait', action='store_true')
    video.add_argument('--out', default='aladin-out')
    billing = sub.add_parser('billing', help='Modal 账单快照')

    args = parser.parse_args()
    if args.url:
        BASE = args.url.rstrip('/')

    out_dir = Path(getattr(args, 'out', None) or 'aladin-out')

    if args.command == 'params':
        status, body = call('GET', '/api/v1/params')
        print(json.dumps(body, ensure_ascii=False, indent=2))
        return 0 if status == 200 else fail(status, body)

    if args.command == 'billing':
        status, body = call('GET', '/api/v1/billing')
        if status != 200:
            return fail(status, body)
        if not body.get('available'):
            print(body.get('note', '还没有账单快照'))
            return 0
        print(f"本期计量 ${body['metered_cost']:.2f}   应付 ${body['billed_cost']:.2f}"
              f"   快照 {body['fetched_at']}")
        line = f"credits 已抵扣 ${body['credits_applied']:.2f}"
        if body.get('balance_estimate') is not None:
            line += f"   余额 ≈ ${body['balance_estimate']:.2f}（本期额度 ${body['credit_grant']:.2f}）"
        print(line)
        return 0

    if args.command == 'jobs':
        filters = {'limit': args.limit}
        if args.state:
            filters['state'] = args.state
        query = '?' + urlencode(filters)
        status, body = call('GET', '/api/v1/jobs' + query)
        if status != 200:
            return fail(status, body)
        for job in body:
            print(f"{job['id']}  {job.get('state', ''):<10} {job.get('mode', ''):<8} "
                  f"{(job.get('prompt') or '')[:60]}")
        return 0

    if args.command == 'approve':
        status, body = call('POST', f'/api/v1/jobs/{args.job_id}/approve', {})
        if status != 200:
            return fail(status, body)
        print(f"approved {body['id']}  {body['params']['images']} 张，开始生成")
        return 0

    if args.command == 'delete':
        status, body = call('DELETE', f'/api/v1/jobs/{args.job_id}')
        if status != 200:
            return fail(status, body)
        print(f"deleted {args.job_id}")
        return 0

    if args.command == 'gallery':
        if args.add:
            status, body = call('POST', '/api/v1/gallery',
                                {'job_id': args.add[0], 'name': args.add[1]}, form=True)
            if status != 201:
                return fail(status, body)
            print(f"added {body.get('added')} id {body.get('id')}")
            return 0
        if args.remove is not None:
            status, body = call('DELETE', f'/api/v1/gallery/{args.remove}')
            if status != 200:
                return fail(status, body)
            print(f"removed {args.remove}")
            return 0
        if args.tags or args.stars:
            item_id, change = (args.tags[0], {'tags': [t.strip() for t in args.tags[1].split(',') if t.strip()]}) \
                if args.tags else (args.stars[0], {'stars': args.stars[1]})
            status, body = call('PATCH', f'/api/v1/gallery/{item_id}', change)
            if status != 200:
                return fail(status, body)
            print(f"{body['id']}  {'★' * body['stars']:<5} {','.join(body['tags'])}")
            return 0
        query = {k: v for k, v in {'q': args.q, 'tag': args.tag, 'model': args.model,
                                   'kind': args.kind, 'min_stars': args.min_stars,
                                   'sort': args.sort}.items() if v}
        status, body = call('GET', '/api/v1/gallery' + ('?' + urlencode(query) if query else ''))
        if status != 200:
            return fail(status, body)
        for item in body:
            tags = ','.join(item.get('tags') or [])
            print(f"{item['id']}  {'★' * item.get('stars', 0):<5} {item.get('model') or '':<22} "
                  f"{tags:<20} {item['prompt'][:50]}")
        return 0

    if args.command == 'status':
        status, body = call('GET', f'/api/v1/jobs/{args.job_id}')
        if status != 200:
            return fail(status, body)
        return show(body, Path(args.out) if args.out else None)

    if args.command == 'director':
        payload = {'brief': args.brief, 'count': args.count, 'rating': args.rating,
                   'review': args.review, 'loras': lora_choices(args.lora)}
        if args.model:
            payload['model'] = args.model
        if args.preset:
            payload['preset'] = args.preset
        spec = {key: getattr(args, key) for key in ('must_keep', 'may_change', 'change_only') if getattr(args, key)}
        if spec:
            payload['creative_spec'] = spec
        status, body = call('POST', '/api/v1/director', payload, timeout=120)
        if status not in (200, 202):
            return fail(status, body)
        if args.review:
            # review 不是终态：规划完会停下等确认，--wait 在这里等不到结果
            print(f"job {body['id']}\nstate {body['state']}\n规划完成后在任务页确认，"
                  f"或运行：aladin.py approve {body['id']}")
            return 0
        if args.wait:
            body = wait(body['id'], every=8)
        return show(body, out_dir if args.wait else None)

    if args.command == 'video':
        data = Path(args.image).read_bytes()
        if len(data) > 8 * 1024 * 1024:
            print('图片超过 8 MiB', file=sys.stderr)
            return 1
        payload = {'image_base64': base64.b64encode(data).decode(),
                   'prompt': args.prompt, 'model': args.model, 'duration': args.duration,
                   'size': args.size, 'seed': args.seed, 'rating': args.rating,
                   'loras': lora_choices(args.lora)}
        status, body = call('POST', '/api/v1/videos/base64', payload, timeout=120)
        if status not in (200, 202):
            return fail(status, body)
        if not body.get('created'):
            print(f"note 同参数任务已存在，复用 {body['id']}", file=sys.stderr)
        # 视频慢得多：冷启动加两段采样要几分钟，轮询间隔放宽
        if args.wait:
            body = wait(body['id'], every=8)
        return show(body, out_dir if args.wait else None)

    # submit / edit
    if args.command == 'submit':
        payload = {'prompt': args.prompt, 'size': args.size, 'images': args.images,
                   'steps': args.steps, 'seed': args.seed, 'negative': args.negative,
                   'model': args.model, 'cfg': args.cfg, 'sampler': args.sampler,
                   'scheduler': args.scheduler, 'rating': args.rating}
        if args.lora:
            payload['loras'] = lora_choices(args.lora)
        payload = {k: v for k, v in payload.items() if v is not None}
    else:
        data = Path(args.image).read_bytes()
        if len(data) > 8 * 1024 * 1024:
            print('图片超过 8 MiB', file=sys.stderr)
            return 1
        payload = {'image_base64': base64.b64encode(data).decode(), 'mode': args.mode,
                   'prompt': args.prompt, 'images': args.images, 'steps': args.steps,
                   'seed': args.seed, 'denoise': args.denoise, 'region': args.region,
                   'rating': args.rating}
        if args.mode == 'edit' and not args.prompt.strip():
            print('指令编辑必须给 --prompt', file=sys.stderr)
            return 1

    path = '/api/v1/images' if args.command == 'submit' else '/api/v1/edits/base64'
    # 用幂等键做 request id 没意义（服务端自己算），这里只要一次网络往返
    status, body = call('POST', path, payload, timeout=120)
    if status not in (200, 202):
        return fail(status, body)
    if not body.get('created'):
        print(f"note 同参数任务已存在，复用 {body['id']}", file=sys.stderr)

    if args.wait:
        body = wait(body['id'])
    return show(body, out_dir if args.wait else None)


if __name__ == '__main__':
    sys.exit(main())
