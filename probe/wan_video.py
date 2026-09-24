#!/usr/bin/env python3
"""宿主侧跑一次图生视频：把一张图交给 aladin-video-v1，取回 webm。

用法：
    .venv/bin/python probe/wan_video.py --image in.jpg --prompt "..." \
        [--frames 81] [--steps 4] [--cfg 1.0] [--out .cache/wan-video.webm]

这是产品接线之前的探针：先在真容器里把「工作流跑得动、产物取得回」证掉，
再把同一套 video_worker 接进 aladin 的提交/排队/产物链路。
"""
from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import aladin_video_modal_app as vapp   # noqa: E402
import video_request as vr              # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description='跑一次 Wan2.2 图生视频')
    parser.add_argument('--image', required=True)
    parser.add_argument('--prompt', default='')
    parser.add_argument('--negative', default='')
    parser.add_argument('--frames', type=int, default=None)
    parser.add_argument('--steps', type=int, default=None)
    parser.add_argument('--cfg', type=float, default=None)
    parser.add_argument('--seed', type=int, default=0)
    parser.add_argument('--out', default='.cache/wan-video.webm')
    args = parser.parse_args()

    source = Path(args.image).read_bytes()
    overrides = {key: value for key, value in
                 (('frames', args.frames), ('steps', args.steps), ('cfg', args.cfg))
                 if value is not None}
    request = vr.build(args.prompt, hashlib.sha256(source).hexdigest(),
                       negative=args.negative, seed=args.seed, **overrides)
    print('key', request['key'], 'frames', request['frames'], 'steps', request['steps'],
          'cfg', request['cfg'], 'shift', request['shift'], flush=True)
    with vapp.app.run():
        result = vapp.generate.remote(request, source)
        print('generated', result['videos'], 'in', result['elapsedSeconds'], 's', flush=True)
        # 产物从 Volume 读回来：与 aladin/pipeline.py 的 _read_bytes 同一条路，
        # 不再单开一个只回传字节的函数——那会让整个视频在返回值里再走一圈。
        # 读必须在 app.run() 上下文内：退出上下文后再读会一直挂住（实测）。
        data = b''.join(vapp.results.read_file(result['key'] + '/video-01.webm'))
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    print('saved', out, len(data), 'bytes',
          'sha256', hashlib.sha256(data).hexdigest()[:16], flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
