#!/bin/bash
# DeepSeek-OCR 部署脚本 (Ubuntu + NVIDIA GPU)
# 运行: bash scripts/deploy.sh

set -e

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
VENV="$PROJECT_DIR/.venv"
SERVICE_FILE="$PROJECT_DIR/scripts/deepseek-ocr.service"
SERVICE_NAME="deepseek-ocr"

echo "=== DeepSeek-OCR 部署 ==="
echo "项目目录: $PROJECT_DIR"

# 1. 安装 Ollama（如果未安装）
if ! command -v ollama &> /dev/null && ! /snap/bin/ollama --version &> /dev/null 2>&1; then
    echo "安装 Ollama..."
    sudo snap install ollama
fi

# 2. 创建 Python 虚拟环境并安装项目
if [ ! -d "$VENV" ]; then
    echo "创建虚拟环境..."
    python3.12 -m venv "$VENV"
fi

echo "安装 Python 依赖..."
"$VENV/bin/pip" install -e "$PROJECT_DIR" -q

# 3. 启动 Ollama 服务并拉取模型
echo "启动 Ollama 服务..."
if command -v ollama &> /dev/null; then
    OLLAMA_CMD="ollama"
elif /snap/bin/ollama --version &> /dev/null 2>&1; then
    OLLAMA_CMD="/snap/bin/ollama"
fi

if ! curl -s http://localhost:11434/api/tags > /dev/null 2>&1; then
    $OLLAMA_CMD serve &
    sleep 5
fi

echo "拉取 deepseek-ocr 模型..."
$OLLAMA_CMD pull deepseek-ocr

# 4. 安装 systemd 服务
echo "安装 systemd 服务..."
# 替换 service 文件中的路径为实际项目路径
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
