#!/usr/bin/env bash
# 把当前 checkout 同步到工作站并重建。必须重建镜像，见下面 --build 的说明。
#
# 改了 aladin/worker.py 或 aladin_modal_app.py 时，还要在任意一台有 modal CLI 的
# 机器上跑一次 modal deploy aladin_modal_app.py，否则任务会因版本不匹配全部失败。
# 脚本会检测到并提醒。完整说明见 docs/deploy.md。
set -euo pipefail
cd "$(dirname "$0")/.."   # rsync 同步的是仓库根目录，.env 也在这里

# 目标机器与目录是个人配置，放在仓库根目录的 .env（不入库）；环境变量优先。
# 只读这两个键，不 source 整个 .env——那里还有给 compose 用的其他配置。
dotenv() { [ -f .env ] && sed -n "s/^$1=//p" .env | tail -1 | tr -d '"'"'"; }
HOST=${ALADIN_HOST:-$(dotenv ALADIN_HOST)}
DIR=${ALADIN_REMOTE_DIR:-$(dotenv ALADIN_REMOTE_DIR)}
DIR=${DIR:-"~/docker/aladin"}
if [ -z "$HOST" ]; then
  echo "未配置部署目标：在 .env 里设置 ALADIN_HOST（SSH 主机名），见 .env.example" >&2
  exit 5
fi
if [ $# -gt 0 ]; then
  echo "不接受参数（脚本始终重建镜像）: $1" >&2
  exit 2
fi

# DIR 在远端 shell 里展开（要保留 ~ 的展开，所以不加引号），
# 因此先挡掉空格与 shell 元字符：既防手滑，也防命令注入。
case $DIR in
  *[!A-Za-z0-9_./~-]*) echo "ALADIN_REMOTE_DIR 含不安全字符: $DIR" >&2; exit 4 ;;
esac

# 防手滑：--delete 会删掉远端多出来的文件，先确认目标确实是本项目的 compose 目录
if ! ssh $HOST "test -f $DIR/docker-compose.yml"; then
  echo "远端 $HOST:$DIR 不像 aladin 的 compose 目录，已中止。" >&2
  exit 3
fi

echo "==> 同步到 $HOST:$DIR"
CHANGES=$(rsync -az --delete --itemize-changes \
  --exclude .git --exclude .venv --exclude .cache --exclude .docker --exclude data \
  --exclude __pycache__ --exclude "*.pyc" \
  --exclude probe/artifacts --exclude "probe/report-*.json" \
  --include .env.example --exclude ".env*" \
  ./ "$HOST:$DIR/")
FILES=$(printf "%s" "$CHANGES" | grep -c . || true)
echo "    变更 $FILES 项"

if printf "%s" "$CHANGES" | grep -qE "aladin/worker\.py|aladin_modal_app\.py"; then
  echo
  echo "!! 容器侧的 worker 或 Modal app 变了，现在必须补一次："
  echo "!!   uv run modal deploy aladin_modal_app.py"
  echo "!! 否则所有任务会因 Worker revision mismatch 失败。"
  echo
fi

if printf "%s" "$CHANGES" | grep -qE "aladin/video_worker\.py|aladin_video_modal_app\.py"; then
  echo
  echo "!! 视频切片变了，现在必须补一次："
  echo "!!   uv run modal deploy aladin_video_modal_app.py"
  echo "!! 否则视频任务会因 Video worker revision mismatch 失败。"
  echo
fi

if printf "%s" "$CHANGES" | grep -qE "aladin/(extra_image_worker|extra_image_request|image_models|worker)\.py|aladin_extra_image_modal_app\.py"; then
  echo "!! 新图像模型代码有变更，请确保两个 Modal app 均已部署："
  echo "!! ALADIN_EXTRA_IMAGE_MODEL=anima-base-1.0 .venv/bin/modal deploy aladin_extra_image_modal_app.py"
  echo "!! ALADIN_EXTRA_IMAGE_MODEL=pony-realism-2.2 .venv/bin/modal deploy aladin_extra_image_modal_app.py"
fi

# 即使 rsync 没差异，也可能是上次同步后构建中断。Docker 自己复用构建缓存；
# 必须完成容器重建，不能把“源文件已同步”当成“运行镜像已更新”。
echo "==> 重建 api 与 worker（保留 postgres）"
ssh "$HOST" "cd $DIR && docker compose up -d --build --force-recreate api worker"
ssh "$HOST" "cd $DIR && docker compose exec -T api python -c 'import request, video_request; from aladin import director; print(\"runtime modules OK\")'"

echo "==> 状态"
ssh $HOST "cd $DIR && docker compose ps"
