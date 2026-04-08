#!/bin/bash
# DeepSeek-OCR 部署脚本 (Ubuntu + NVIDIA GPU + vLLM)
# 运行: bash scripts/deploy.sh

set -e

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
VENV="$PROJECT_DIR/.venv"
SERVICE_FILE="$PROJECT_DIR/scripts/deepseek-ocr.service"
SERVICE_NAME="deepseek-ocr"

echo "=== DeepSeek-OCR 部署 ==="
echo "项目目录: $PROJECT_DIR"

# 1. 运行安装脚本
echo ""
echo "--- 安装依赖 ---"
bash "$PROJECT_DIR/scripts/setup_vllm.sh"

# 2. 安装 systemd 服务
echo ""
echo "--- 安装 systemd 服务 ---"
sed "s|/home/rongheng/python_project/deepseek-ocr|$PROJECT_DIR|g; s|User=rongheng|User=$(whoami)|g" \
    "$SERVICE_FILE" | sudo tee /etc/systemd/system/$SERVICE_NAME.service > /dev/null

sudo systemctl daemon-reload
sudo systemctl enable $SERVICE_NAME
sudo systemctl restart $SERVICE_NAME

echo ""
echo "=== 部署完成 ==="
echo "服务状态: sudo systemctl status $SERVICE_NAME"
echo "Web界面: http://localhost:8080"
echo "日志: sudo journalctl -u $SERVICE_NAME -f"
