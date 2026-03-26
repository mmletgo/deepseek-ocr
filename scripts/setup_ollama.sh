#!/bin/bash
# Ollama 安装和 DeepSeek-OCR 模型拉取脚本

set -e

echo "=== DeepSeek-OCR 环境设置 ==="

# 检查是否已安装 Ollama
if ! command -v ollama &> /dev/null; then
    echo "正在安装 Ollama..."
    if command -v brew &> /dev/null; then
        brew install ollama
    else
        curl -fsSL https://ollama.com/install.sh | sh
    fi
    echo "Ollama 安装完成"
else
    echo "Ollama 已安装: $(ollama --version)"
fi

# 检查 Ollama 服务是否运行
if ! curl -s http://localhost:11434/api/tags > /dev/null 2>&1; then
    echo "正在启动 Ollama 服务..."
    ollama serve &
    sleep 3
fi

# 拉取 DeepSeek-OCR 模型
echo "正在拉取 deepseek-ocr 模型 (约6.7GB)..."
ollama pull deepseek-ocr

echo "=== 设置完成 ==="
echo "验证: ollama run deepseek-ocr 'Hello'"
