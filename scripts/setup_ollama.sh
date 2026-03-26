#!/bin/bash
# Ollama 安装和 DeepSeek-OCR 模型拉取脚本
# 支持: Ubuntu/Linux (NVIDIA CUDA / CPU) 和 macOS (Apple Silicon / Intel)

set -e

echo "=== DeepSeek-OCR 环境设置 ==="

# 检查是否已安装 Ollama
if ! command -v ollama &> /dev/null; then
    echo "正在安装 Ollama..."
    if command -v brew &> /dev/null; then
        # macOS
        brew install ollama
    else
        # Linux (Ubuntu 等)
        curl -fsSL https://ollama.com/install.sh | sh
    fi
    echo "Ollama 安装完成"
else
    echo "Ollama 已安装: $(ollama --version)"
fi

# 检查 GPU 支持情况（仅供提示）
if command -v nvidia-smi &> /dev/null; then
    echo "检测到 NVIDIA GPU，Ollama 将使用 CUDA 加速"
    nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>/dev/null || true
elif [[ "$(uname)" == "Darwin" ]] && [[ "$(uname -m)" == "arm64" ]]; then
    echo "检测到 Apple Silicon，Ollama 将使用 Metal 加速"
else
    echo "未检测到 GPU，Ollama 将使用 CPU 运行（速度较慢）"
fi

# 检查 Ollama 服务是否运行
if ! curl -s http://localhost:11434/api/tags > /dev/null 2>&1; then
    echo "正在启动 Ollama 服务..."
    ollama serve &
    sleep 5
fi

# 拉取 DeepSeek-OCR 模型
echo "正在拉取 deepseek-ocr 模型 (约6.7GB)..."
ollama pull deepseek-ocr

echo "=== 设置完成 ==="
echo "验证: ollama run deepseek-ocr 'Hello'"
