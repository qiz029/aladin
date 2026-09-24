"""把目录里的 LoRA 从 Civitai 下载、校验，再上传到对应底模的 Modal 模型 Volume。

在本机跑（需要 Modal 凭据）：

    python -m aladin loras-sync [--only ID ...] [--dry-run]

Civitai 的 API key 只从环境变量或仓库根目录 .env 的 CIVITAI_API_KEY 读取，**只在本机使用**：
它不进代码、不进日志、不上传到 Modal。GPU 容器只从 Volume 读文件，不接触 Civitai。

Authorization 头用 unredirected header 加：Civitai 会 302 到 CDN 的预签名地址，
那个第三方主机不应该收到你的 key。
"""
from __future__ import annotations

import hashlib
import os
import tempfile
import urllib.request
from pathlib import Path

from . import loras, settings

DOWNLOAD_URL = 'https://civitai.com/api/download/models/{version}?type=Model&format=SafeTensor'
CHUNK = 1 << 20


def api_key() -> str:
    """环境变量优先，其次仓库根目录的 .env。找不到就报错，绝不打印值。"""
    value = os.environ.get('CIVITAI_API_KEY')
    if not value:
        path = settings.ROOT / '.env'
        if path.is_file():
            for line in path.read_text().splitlines():
                name, sep, raw = line.strip().partition('=')
                if sep and name.strip() == 'CIVITAI_API_KEY':
                    value = raw.strip().strip('\'"')
    if not value:
        raise SystemExit('缺少 CIVITAI_API_KEY：写进仓库根目录的 .env（不入库），见 .env.example')
    return value


def remote_sizes(volume) -> dict[str, int]:
    try:
        return {Path(entry.path).name: entry.size for entry in volume.listdir('loras')}
    except Exception:              # 目录还不存在
        return {}


def download(item: dict, key: str, directory: Path) -> Path:
    """流式下载并边下边算 sha256；不符就删掉临时文件并报错。"""
    request = urllib.request.Request(DOWNLOAD_URL.format(version=item['civitai_version']),
                                     headers={'User-Agent': 'aladin-lora-sync'})
    request.add_unredirected_header('Authorization', 'Bearer ' + key)
    target = directory / item['file']
    digest = hashlib.sha256()
    with urllib.request.urlopen(request, timeout=120) as response, target.open('wb') as out:
        while chunk := response.read(CHUNK):
            digest.update(chunk)
            out.write(chunk)
    if digest.hexdigest() != item['sha256']:
        target.unlink(missing_ok=True)
        raise RuntimeError(f"{item['name']}: sha256 与目录钉住的版本不符（Civitai 返回了别的文件？）")
    return target


def sync(only: list[str] | None = None, dry_run: bool = False) -> int:
    import modal

    wanted = [item for item in loras.CATALOG if not only or item['id'] in only]
    unknown = set(only or []) - {item['id'] for item in wanted}
    if unknown:
        raise SystemExit('目录里没有：' + ', '.join(sorted(unknown)))
    key = None if dry_run else api_key()
    uploaded = 0
    for family, volume_name in loras.VOLUME_OF_FAMILY.items():
        items = [item for item in wanted if item['family'] == family]
        if not items:
            continue
        volume = modal.Volume.from_name(volume_name)
        present = remote_sizes(volume)
        missing = [item for item in items if item['file'] not in present]
        print(f'{volume_name}: 目录 {len(items)} 个，已在 Volume {len(items) - len(missing)} 个，'
              f'待上传 {len(missing)} 个（约 {sum(i["size_mb"] for i in missing)} MB）', flush=True)
        if dry_run or not missing:
            for item in missing:
                print(f"  将上传 {item['id']:<24} {item['size_mb']:>4} MB  {item['name']}")
            continue
        with tempfile.TemporaryDirectory(prefix='aladin-lora-') as temporary:
            for item in missing:
                print(f"  下载 {item['id']} …", end='', flush=True)
                path = download(item, key, Path(temporary))
                with volume.batch_upload() as batch:
                    batch.put_file(str(path), f"loras/{item['file']}")
                path.unlink()
                uploaded += 1
                print(f" 已校验并上传（{item['size_mb']} MB）", flush=True)
    return uploaded
