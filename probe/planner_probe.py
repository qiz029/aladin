#!/usr/bin/env python3
"""宿主机侧跑一次规划：一句需求 → N 条图像指令。

用法：
    .venv/bin/python probe/planner_probe.py --brief "……" --count 3 [--out .cache/plan.json]

第一次调用要把 55GB 权重下进 Volume（只一次），之后每次是「权重已缓存 + 引擎重建」。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import aladin_planner_modal_app as planner   # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description='跑一次 planner')
    parser.add_argument('--brief', required=True)
    parser.add_argument('--count', type=int, default=3)
    parser.add_argument('--out', default='.cache/plan.json')
    parser.add_argument('--rounds', type=int, default=2,
                        help='调用几次（第二次用于测量「权重已缓存」的成本）')
    args = parser.parse_args()

    request = {'brief': args.brief, 'count': args.count,
               'plannerRevision': planner.revision()}
    print('plannerRevision', request['plannerRevision'][:16], flush=True)
    with planner.app.run():
        for round_index in range(1, args.rounds + 1):
            started = time.time()
            try:
                result = planner.plan.remote(request)
            except Exception as error:
                print(f'[第 {round_index} 次] 失败：{type(error).__name__}: {str(error)[:600]}',
                      flush=True)
                return 1
            wall = round(time.time() - started, 1)
            print(f'[第 {round_index} 次] 容器内耗时 {result["elapsedSeconds"]}s，'
                  f'端到端 {wall}s', flush=True)
            for index, variant in enumerate(result['variants'], start=1):
                print(f'  {index}. {variant["size"]} {variant["width"]}×{variant["height"]} '
                      f'steps={variant["steps"]} cfg={variant["cfg"]} seed={variant["seed"]}',
                      flush=True)
                print(f'     prompt: {variant["prompt"][:160]}', flush=True)
                if variant['negative']:
                    print(f'     negative: {variant["negative"][:100]}', flush=True)
                if variant['rationale']:
                    print(f'     为什么: {variant["rationale"][:100]}', flush=True)
            if result['notes']:
                print('  改写记录:', flush=True)
                for note in result['notes']:
                    print('    -', note, flush=True)
            Path(args.out).write_text(json.dumps(result, ensure_ascii=False, indent=2),
                                      encoding='utf-8')
            print(f'  原始输出已存到 {args.out}', flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
