FROM python:3.11-slim

ENV TZ=Asia/Shanghai
ENV PYTHONUNBUFFERED=1

# 安装时区和 curl（用于健康检查）
RUN apt-get update && apt-get install -y --no-install-recommends \
    tzdata \
    && ln -sf /usr/share/zoneinfo/Asia/Shanghai /etc/localtime \
    && echo "Asia/Shanghai" > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 先装依赖（利用 Docker 缓存）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制脚本
COPY daily_update.py .

# 默认以 --no-push 模式运行（NAS 无法直连 GitHub）
ENTRYPOINT ["python", "daily_update.py", "--no-push"]
