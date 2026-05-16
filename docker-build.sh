#!/bin/bash
# rsi-monitor Docker 构建脚本
# 在项目根目录运行即可

set -e

echo "========================================"
echo "构建 rsi-monitor Docker 镜像"
echo "========================================"

# 如果本地有旧镜像，先清理
docker rmi rsi-monitor:latest 2>/dev/null || true

# 构建新镜像
docker build -t rsi-monitor:latest .

echo ""
echo "✅ 构建完成！"
echo ""
echo "运行测试："
echo "  docker run --rm -v \"$(pwd)/docs:/app/docs\" rsi-monitor:latest"
echo ""
echo "群晖 NAS 任务计划器设置："
echo "  任务类型: 用户定义的脚本"
echo "  运行命令: docker run --rm -v /path/to/rsi-monitor/docs:/app/docs rsi-monitor:latest"
echo "  时间: 每日 15:30"
echo ""
