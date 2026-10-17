FROM python:3.12-alpine

LABEL maintainer="lidalao"
LABEL version="0.0.1"
LABEL description="Telegram Bot for ServerStatus"

# 原基础镜像 nyurik/alpine-python3-requests 只有 amd64, 在 ARM 主机上 exec format error;
# 官方 python 镜像是多架构的
RUN pip install --no-cache-dir requests

WORKDIR /app
COPY ./bot.py .
# -u: 不缓冲 stdout, docker compose logs 能实时看到输出
CMD [ "python", "-u", "./bot.py" ]
