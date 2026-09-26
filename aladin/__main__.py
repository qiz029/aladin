"""本地服务入口。

    python -m aladin api      只跑 web（页面 / SSE / 下载）
    python -m aladin worker   只跑任务循环（提交 / 轮询 / 拉产物）
    python -m aladin          两者同进程，方便本机调试
    python -m aladin cleanup --days 30 [--dry-run]
                              删除 30 天前结束、且没有人工评审的任务及其本地文件
    python -m aladin loras-sync [--only ID ...] [--dry-run]
                              把 LoRA 目录下载、校验并上传到 Modal 模型 Volume（本机跑，key 在 .env）
    python -m aladin strip-metadata [--dry-run]
                              去掉已有产物/收藏里内嵌的 workflow 与提示词（一次性）
    python -m aladin timings [--since 2026-09-25] [--app image] [--limit 50]
                              逐任务的生成耗时分段与按模型 × 冷热的汇总
    python -m aladin timings --backfill [--dry-run]
                              给没有 timings 的老任务补上 Volume 回执里的耗时（一次性）

容器里 api 与 worker 分开跑；同进程模式只是省事，不用于生产拓扑。
"""
from __future__ import annotations

import argparse
import threading


def main() -> None:
    parser = argparse.ArgumentParser(description='aladin')
    parser.add_argument('command', nargs='?', default='all',
                        choices=['api', 'worker', 'all', 'cleanup', 'strip-metadata', 'loras-sync',
                                 'timings'])
    parser.add_argument('--host', default='127.0.0.1',
                        help='默认只监听本机；容器里传 0.0.0.0')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--days', type=int, default=30,
                        help='cleanup：清理多少天前结束的任务（图库副本不受影响）')
    parser.add_argument('--dry-run', action='store_true',
                        help='cleanup / strip-metadata / loras-sync / timings --backfill：只列出，不改动')
    parser.add_argument('--only', nargs='+', help='loras-sync：只同步这些 LoRA id')
    parser.add_argument('--since', help='timings：只看这个日期之后创建的任务')
    parser.add_argument('--app', choices=['image', 'video'], help='timings：只看图片或视频')
    parser.add_argument('--limit', type=int, default=50, help='timings：最多列多少个任务')
    parser.add_argument('--backfill', action='store_true', help='timings：补老任务的耗时')
    args = parser.parse_args()

    if args.command == 'loras-sync':
        # 只碰 Civitai 与 Modal，不需要数据库
        from . import lora_sync

        count = lora_sync.sync(args.only, dry_run=args.dry_run)
        print(f'上传了 {count} 个 LoRA')
        return

    from . import db, pipeline

    db.init()          # 迁移在这里统一执行，api 与 worker 都会走到

    if args.command == 'strip-metadata':
        from . import cleanup

        count = cleanup.strip_existing_metadata(dry_run=args.dry_run)
        print(f"{'将清理' if args.dry_run else '已清理'} {count} 个文件")
        return

    if args.command == 'timings':
        from . import timings

        if args.backfill:
            count = timings.backfill(dry_run=args.dry_run)
            print(f"{'将补' if args.dry_run else '已补'} {count} 个任务")
        else:
            print(timings.report(args.since, args.app, args.limit))
        return

    if args.command == 'cleanup':
        from . import cleanup

        count = cleanup.purge(args.days, dry_run=args.dry_run)
        print(f"{'将删除' if args.dry_run else '已删除'} {count} 个任务")
        return

    if args.command == 'worker':
        pipeline.run_forever()
        return

    if args.command == 'all':
        threading.Thread(target=pipeline.run_forever, daemon=True,
                         name='worker').start()

    import uvicorn

    uvicorn.run('aladin.web:app', host=args.host, port=args.port, log_level='info')


if __name__ == '__main__':
    main()
