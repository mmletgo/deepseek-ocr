#!/bin/bash
# DeepSeek-OCR vLLM 环境安装脚本 (Ubuntu + NVIDIA GPU)
# 运行: bash scripts/setup_vllm.sh

set -e

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
VENV="$PROJECT_DIR/.venv"
DEEPSEEK_OCR2_DIR="$PROJECT_DIR/.deepseek-ocr2"

echo "=== DeepSeek-OCR vLLM 环境安装 ==="
echo "项目目录: $PROJECT_DIR"

# 1. 检查 NVIDIA GPU
echo ""
echo "--- 检查 NVIDIA GPU ---"
if ! command -v nvidia-smi &> /dev/null; then
    echo "错误: 未检测到 NVIDIA GPU 驱动"
    echo "请先安装 NVIDIA 驱动: sudo apt install nvidia-driver-535"
    exit 1
fi
nvidia-smi --query-gpu=name,memory.total --format=csv,noheader
echo "GPU 检查通过"

# 2. 检查 CUDA
echo ""
echo "--- 检查 CUDA ---"
if [ -z "$CUDA_HOME" ]; then
    if [ -d "/usr/local/cuda" ]; then
        export CUDA_HOME="/usr/local/cuda"
    elif [ -d "/usr/local/cuda-11.8" ]; then
        export CUDA_HOME="/usr/local/cuda-11.8"
    elif [ -d "/usr/local/cuda-12" ]; then
        export CUDA_HOME="/usr/local/cuda-12"
    fi
fi

if [ -n "$CUDA_HOME" ] && [ -x "$CUDA_HOME/bin/nvcc" ]; then
    echo "CUDA 版本: $($CUDA_HOME/bin/nvcc --version | grep release | awk '{print $5}' | sed 's/,//')"
else
    echo "警告: 未检测到 CUDA toolkit，部分功能可能受限"
fi

# 3. 创建 Python 虚拟环境
echo ""
echo "--- 创建虚拟环境 ---"
if [ ! -d "$VENV" ]; then
    python3.12 -m venv "$VENV"
    echo "虚拟环境已创建: $VENV"
else
    echo "虚拟环境已存在: $VENV"
fi

# 4. 安装 PyTorch
echo ""
echo "--- 安装 PyTorch ---"
"$VENV/bin/pip" install torch==2.6.0 torchvision==0.21.0 --index-url https://download.pytorch.org/whl/cu118 -q

# 5. 安装 vLLM
echo ""
echo "--- 安装 vLLM ---"
"$VENV/bin/pip" install vllm==0.8.5 -q

# 6. 安装 flash-attn（可选但推荐）
echo ""
echo "--- 安装 Flash Attention (可选) ---"
if "$VENV/bin/python" -c "import flash_attn" 2>/dev/null; then
    echo "flash-attn 已安装"
else
    echo "安装 flash-attn (编译可能需要 10-30 分钟)..."
    "$VENV/bin/pip" install flash-attn==2.7.3 --no-build-isolation 2>/dev/null || {
        echo "警告: flash-attn 安装失败，将使用普通注意力机制"
    }
fi

# 7. 安装项目依赖
echo ""
echo "--- 安装项目依赖 ---"
"$VENV/bin/pip" install -e "$PROJECT_DIR" -q

# 8. 克隆 DeepSeek-OCR-2 仓库（获取推理模块）
echo ""
echo "--- DeepSeek-OCR-2 推理模块 ---"
if [ ! -d "$DEEPSEEK_OCR2_DIR" ]; then
    echo "克隆 DeepSeek-OCR-2 仓库..."
    git clone https://github.com/deepseek-ai/DeepSeek-OCR-2.git "$DEEPSEEK_OCR2_DIR"
else
    echo "仓库已存在: $DEEPSEEK_OCR2_DIR"
fi

# 将 deepseek_ocr2 模块添加到 Python path
SITE_PACKAGES=$("$VENV/bin/python" -c "import site; print(site.getsitepackages()[0])")
PTH_FILE="$SITE_PACKAGES/deepseek-ocr2.pth"
echo "$DEEPSEEK_OCR2_DIR/DeepSeek-OCR2-vllm" > "$PTH_FILE"
echo "已添加 .pth 文件: $PTH_FILE"

# 9. 下载模型（如果未下载）
echo ""
echo "--- 模型下载 ---"
echo "模型将在首次使用时自动从 HuggingFace 下载"
echo "如需预下载: $VENV/bin/huggingface-cli download deepseek-ai/DeepSeek-OCR-2"
echo ""
echo "国内用户可设置镜像: export HF_ENDPOINT=https://hf-mirror.com"

# 10. 验证安装
echo ""
echo "--- 验证安装 ---"
"$VENV/bin/python" -c "
import torch
print(f'PyTorch: {torch.__version__}')
print(f'CUDA available: {torch.cuda.is_available()}')
if torch.cuda.is_available():
    print(f'GPU: {torch.cuda.get_device_name(0)}')
" || echo "警告: PyTorch 验证失败"

"$VENV/bin/python" -c "
import vllm
print(f'vLLM: {vllm.__version__}')
" || echo "警告: vLLM 验证失败"

"$VENV/bin/python" -c "
from deepseek_ocr2 import DeepseekOCR2ForCausalLM
from process.image_process import DeepseekOCR2Processor
from process.ngram_norepeat import NoRepeatNGramLogitsProcessor
print('DeepSeek-OCR-2 模块: OK')
" || echo "警告: DeepSeek-OCR-2 模块验证失败"

echo ""
echo "=== 安装完成 ==="
echo "验证环境: $VENV/bin/deepseek-ocr check"
echo "启动Web:  $VENV/bin/deepseek-ocr serve"
