"""验证 aladin 依赖的两条通道：spawn 提交 + FunctionCall.logs.stream() 实时日志。

刻意不碰 GPU 生图也能跑（smoke 模式），用最小代价确认凭据、调用、日志流三者连通。
默认只读，不写任何 Modal Volume。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import modal_reference as ref  # noqa: E402


def read_result_from_volume(key: str):
    """返回值不可信时，从 Volume 读权威产物清单。"""
    import modal

    volume = modal.Volume.from_name(ref.VOLUME_RESULTS)
    data = bytearray()
    for chunk in volume.read_file(key + '/result.json'):
        data.extend(chunk)
    return json.loads(bytes(data))


def parse_args():
    parser = argparse.ArgumentParser(description='aladin Modal 通道探针')
    parser.add_argument('--mode', choices=['smoke', 'image'], default='smoke',
                        help='smoke: 只验证通道（默认）；image: 真实生图，会产生 GPU 费用')
    parser.add_argument('--gpu', default='L4', choices=sorted(ref.GPU_FUNCTIONS))
    parser.add_argument('--id', default='probe-smoke', help='任务标识，参与幂等键')
    parser.add_argument('--prompt', default='a lone lighthouse at dusk, calm sea')
    parser.add_argument('--steps', type=int, default=20)
    parser.add_argument('--cfg', type=float, default=4.0)
    parser.add_argument('--size', type=int, default=1024)
    parser.add_argument('--seed', type=int, default=0)
    parser.add_argument('--sampler', default='euler')
    parser.add_argument('--scheduler', default='simple')
    parser.add_argument('--model-revision', default='',
                        help='HuggingFace 模型 commit（40 位 hex）；冷启动会下载 14.63GB 权重')
    parser.add_argument('--stream-timeout', type=float, default=120.0,
                        help='日志流无新输出时的退出阈值（秒）')
    parser.add_argument('--max-wait', type=float, default=2400.0,
                        help='等待结果的硬上限（秒）')
    parser.add_argument('--report', default='', help='把测量结果写成 JSON')
    parser.add_argument('--volume', action='store_true',
                        help='返回值不可反序列化时，改从 Results Volume 读回产物')
    return parser.parse_args()


def build(args):
    # modal_app.py 的 invoke(request, source) 需要两个位置参数；smoke 不接受媒体，传空 bytes。
    if args.mode == 'smoke':
        key = hashlib.sha256((args.id + '/gpu-smoke/' + args.gpu).encode()).hexdigest()
        return ref.APP_GPU, resource_function(args.gpu), \
            ref.storage_request(key=key, gpu=args.gpu, task='gpu-smoke'), key, b''
    request = ref.image_request(
        prompts=[args.prompt], width=args.size, height=args.size, steps=args.steps,
        cfg=args.cfg, seed=args.seed, sampler=args.sampler, scheduler=args.scheduler,
        model_revision=args.model_revision,
    )
    key = hashlib.sha256((args.id + '/image/' + json.dumps(
        {k: request[k] for k in ('prompts', 'steps', 'cfg', 'seed', 'sampler', 'scheduler', 'width', 'height')},
        sort_keys=True)).encode()).hexdigest()
    request['key'] = key
    # image_worker.execute() 只写 Volume，不接收输入字节；generate(request) 只收一个参数。
    return ref.APP_IMAGE, 'generate', request, key, None


def resource_function(gpu: str) -> str:
    return ref.GPU_FUNCTIONS[gpu]


def main() -> int:
    args = parse_args()
    if args.mode == 'image' and not args.model_revision:
        print('probe    image 模式必须提供 --model-revision（HuggingFace 的 40 位 commit）',
              file=sys.stderr, flush=True)
        return 2
    app_name, function_name, request, key, source = build(args)

    import modal

    print(f'client   modal {modal.__version__}', flush=True)
    print(f'target   {app_name}::{function_name}', flush=True)
    print(f'gpu      {args.gpu}   key {key[:16]}...', flush=True)

    started = time.monotonic()
    function = modal.Function.from_name(app_name, function_name)
    call = function.spawn(request) if source is None else function.spawn(request, source)
    submitted = time.monotonic()
    print(f'spawn    ok, call_id {call.object_id}', flush=True)

    first_log = None
    result = None
    expired = False
    failure = None
    try:
        for entry in call.logs.stream(timeout=args.stream_timeout):
            now = time.monotonic()
            if first_log is None:
                first_log = now
                print(f'... first log after {first_log - submitted:.1f}s', flush=True)
            print(entry.message, end='', flush=True)
            if now - started > args.max_wait:
                print('\nprobe    max-wait reached, stop streaming', flush=True)
                break
            # Modal 文档：timeout=0 表示“立即轮询、不等待”，据此判断是否已完成。
            try:
                value = call.get(timeout=0)
            except TimeoutError:
                continue
            except modal.exception.OutputExpiredError:
                expired = True
                break
            except Exception as error:  # 容器内异常会被重新抛出，不要吞掉
                failure = type(error).__name__ + ': ' + str(error)
                break
            result = value
            break
    except KeyboardInterrupt:
        print('\nprobe    interrupted; job 仍在 Modal 上运行', flush=True)

    if result is None and not expired:
        try:
            result = call.get(timeout=0)
        except TimeoutError:
            print('probe    job 仍在运行（未拿到结果）', flush=True)
        except modal.exception.OutputExpiredError:
            expired = True
        except Exception as error:
            failure = type(error).__name__ + ': ' + str(error)

    # 返回值不可反序列化时，Volume 里的 result.json 才是权威产物。
    if result is None and args.volume:
        try:
            result = read_result_from_volume(key)
            print('probe    返回值不可用，已从 Volume 读回 result.json', flush=True)
            failure = None
        except Exception as error:
            print('probe    Volume 读回失败: ' + str(error)[:160], flush=True)

    report = {
        'mode': args.mode,
        'app': app_name,
        'function': function_name,
        'gpu': args.gpu,
        'call_id': call.object_id,
        'key': key,
        'submit_seconds': round(submitted - started, 2),
        'first_log_seconds': None if first_log is None else round(first_log - submitted, 2),
        'total_seconds': round(time.monotonic() - started, 2),
        'output_expired': expired,
        'failure': failure,
        'result': summarize(result),
    }
    print('\n' + json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    if args.report:
        Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    return 0 if (result is not None or expired) else 1


def summarize(result):
    if not isinstance(result, dict):
        return result
    keep = ('elapsedSeconds', 'hardware', 'task', 'schemaVersion')
    summary = {k: result[k] for k in keep if k in result}
    if 'artifacts' in result:
        summary['artifacts'] = result['artifacts']
    if 'images' in result:
        # 容器返回值不受我们控制；非 dict 元素不应让已经成功的报告崩掉。
        summary['images'] = [{k: i.get(k) for k in ('index', 'file', 'sha256', 'bytes')}
                             for i in result['images'] if isinstance(i, dict)]
    return summary or '<收到结果，字段未识别>'


if __name__ == '__main__':
    raise SystemExit(main())
