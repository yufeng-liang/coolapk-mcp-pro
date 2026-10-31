# 酷安 MCP Server 远程部署镜像（streamable-http 形态，详见 README「远程部署」章）
FROM python:3.14-slim

WORKDIR /app

# 只拷贝打包所需文件（pyproject 仅 include coolapk_mcp*，uvicorn / starlette 由 mcp 传递依赖带入）
COPY pyproject.toml ./
COPY coolapk_mcp/ ./coolapk_mcp/

RUN pip install --no-cache-dir .

# 远程形态默认走 HTTP；容器内必须绑 0.0.0.0 才能被端口映射访问，token 由运行时 -e 传入
ENV COOLAPK_MCP_TRANSPORT=streamable-http \
    COOLAPK_HTTP_HOST=0.0.0.0

EXPOSE 8000

# 设备码 / 登录态 / auth_blob 持久化目录，建议 -v 挂载宿主机目录
VOLUME /root/.coolapk-mcp

ENTRYPOINT ["coolapk-mcp"]
