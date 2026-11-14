# 酷安 MCP Server 远程部署镜像（streamable-http 形态，详见 README「远程部署」章）
FROM python:3.14-slim

# 非 root 运行：容器逃逸面 × 供应链投毒后的破坏面都能收窄；
# 10001 是容器编排里常用的无特权 UID 段，避免与宿主机 1000 用户撞车
ARG APP_UID=10001
ARG APP_USER=coolapk

ENV HOME=/home/${APP_USER} \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# 先把「只影响依赖解析」的文件单独一层：只改代码时这一步能命中缓存。
# 用一个只含 __init__ 的空壳包把依赖装好，随后真正的源码 COPY 会覆盖它。
COPY pyproject.toml README.md LICENSE NOTICE.md ./
COPY coolapk_mcp/__init__.py ./coolapk_mcp/__init__.py
# 缓存要挂在 pip 真正用的目录（HOME 已切到 /home/coolapk），且不能同时带 --no-cache-dir
RUN --mount=type=cache,target=/home/${APP_USER}/.cache/pip pip install .

# 真正源码（改动这一层不会让依赖重装）
COPY coolapk_mcp/ ./coolapk_mcp/
RUN pip install --no-cache-dir --no-deps . \
    && useradd --create-home --shell /usr/sbin/nologin --uid ${APP_UID} ${APP_USER} \
    && mkdir -p /home/${APP_USER}/.coolapk-mcp \
    && chown -R ${APP_USER}:${APP_USER} /home/${APP_USER} /app

# 设备码 / 登录态 / auth_blob / img_secret 持久化目录，建议 -v 挂载宿主机目录。
# 挂载点属主必须与 APP_UID 一致（否则容器内写不进 img_secret，每次重启都会换密钥）：
#   mkdir -p ~/.coolapk-mcp && sudo chown -R 10001:10001 ~/.coolapk-mcp
VOLUME /home/${APP_USER}/.coolapk-mcp

USER ${APP_USER}

# 远程形态默认走 HTTP；容器内必须绑 0.0.0.0 才能被端口映射访问，
# token 由运行时 -e COOLAPK_HTTP_TOKEN=... 传入（不传则拒绝启动）
ENV COOLAPK_MCP_TRANSPORT=streamable-http \
    COOLAPK_HTTP_HOST=0.0.0.0

EXPOSE 8000

# 探活 /health（免鉴权端点）；slim 镜像无 curl，用标准库 urllib。
# exec 形式避免再套一层 /bin/sh -c，start-period 给足首次登录态探测的时间
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4)"]

ENTRYPOINT ["coolapk-mcp"]
