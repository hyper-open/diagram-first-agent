# syntax=docker/dockerfile:1
# 教学用镜像：标准库 HTTP 服务，零第三方依赖。
# 生产请换成 gunicorn/uvicorn + TLS 终结，见 docs/practice/deployment.md。

FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MINI_AGENT_HOST=0.0.0.0 \
    MINI_AGENT_PORT=8000 \
    MINI_AGENT_DATA_DIR=/data \
    MINI_AGENT_ENV=staging

WORKDIR /app

# 只复制运行需要的代码，不复制 docs/、offline/、node_modules。
COPY examples/__init__.py ./examples/__init__.py
COPY examples/mini_agent ./examples/mini_agent
COPY examples/mini_agent_v2 ./examples/mini_agent_v2
COPY examples/mini_agent_v3 ./examples/mini_agent_v3

# 以非 root 运行：容器内逃逸的第一步通常是拿到 root。
RUN useradd --create-home --uid 10001 appuser \
 && mkdir -p /data \
 && chown -R appuser:appuser /app /data

USER appuser
VOLUME ["/data"]
EXPOSE 8000

# 健康检查走真实端点，而不是只看进程是否存在。
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD python3 -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2).status==200 else 1)"

ENTRYPOINT ["python3", "-m", "examples.mini_agent_v3.app"]
