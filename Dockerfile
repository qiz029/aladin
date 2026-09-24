# 单个镜像，api 与 worker 用不同命令启动：少一次构建、少一处版本漂移。
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

# 先只装依赖，让代码改动不失效这一层
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY aladin ./aladin
COPY migrations ./migrations
COPY aladin_modal_app.py aladin_video_modal_app.py aladin_planner_modal_app.py request.py video_request.py ./

# 产物目录由 compose 以 bind mount 提供；这里建好挂载点
RUN mkdir -p /data

EXPOSE 8765
CMD ["python", "-m", "aladin", "api"]
