# DeepSeek-OCR

A local-first tool that converts scanned English PDFs into **searchable dual-layer PDFs** and **Markdown** files, powered by [DeepSeek-OCR-2](https://huggingface.co/deepseek-ai/DeepSeek-OCR-2) model with [vLLM](https://github.com/vllm-project/vllm) inference. Supports translating OCR results into target languages via OpenAI-compatible LLM APIs, producing translated PDFs and bilingual side-by-side PDFs.

All processing happens locally on your machine — no cloud API calls, no data leaves your device.

## Features

- **DeepSeek-OCR-2 model** — Latest Visual Causal Flow architecture with DeepEncoder V2 for superior OCR quality
- **vLLM inference** — High-performance GPU inference with Flash Attention, running in-process (no separate service)
- **Two PDF output modes**:
  - **Dual Layer** — Original scanned images preserved with an invisible, searchable text layer. Visually identical to the original, fully searchable and selectable.
  - **Rewrite** — Text areas are redrawn with vector fonts for crisp, clear text. Charts, images, and tables are preserved from the original scan. LaTeX formulas are rendered with matplotlib's mathtext engine.
- **Markdown output** — Clean Markdown extraction of document text.
- **Translation** — Translate OCR results to target languages via OpenAI-compatible LLM API, generating translated PDFs and bilingual side-by-side PDFs
- **Text PDF support** — Automatically detects text-based (non-scanned) PDFs, extracts text directly without OCR
- **LaTeX formula rendering** — Mathematical equations (both display `\[...\]` and inline `\(...\)`) are rendered as proper math symbols in Rewrite mode
- **Multi-process parallel rendering** — PDF generation uses `ProcessPoolExecutor` with forkserver for true multi-core acceleration
- **OCR result caching** — Per-page OCR results are cached by PDF MD5 hash, enabling instant re-generation without re-running OCR
- **GPU accelerated** — Requires NVIDIA GPU with CUDA support
- **CLI + Web interface** — Command line for scripting/automation, or drag-and-drop web UI with real-time progress via SSE

## Requirements

- **Python** 3.12+
- **NVIDIA GPU** with CUDA 11.8+ (e.g., RTX 3090/4090, A100)
- **CUDA toolkit** (optional but recommended)
- **~20 GB disk space** for model weights + dependencies

## Installation

### Quick Install

```bash
git clone https://github.com/mmletgo/deepseek-ocr.git
cd deepseek-ocr
bash scripts/setup_vllm.sh
```

The setup script will:
1. Check NVIDIA GPU and CUDA
2. Create Python virtual environment (`.venv`)
3. Install PyTorch 2.6.0 + vLLM 0.8.5 + Flash Attention
4. Download DeepSeek-OCR-2 inference modules
5. Verify all components

### Manual Install

```bash
python3.12 -m venv .venv
source .venv/bin/activate

# Install PyTorch
pip install torch==2.6.0 torchvision==0.21.0 --index-url https://download.pytorch.org/whl/cu118

# Install vLLM
pip install vllm==0.8.5

# Install Flash Attention (optional, recommended for performance)
pip install flash-attn==2.7.3 --no-build-isolation

# Install project
pip install -e .
```

### Download Model

The model will be auto-downloaded from HuggingFace on first use. To pre-download:

```bash
# Set mirror for China users
export HF_ENDPOINT=https://hf-mirror.com

pip install huggingface_hub
huggingface-cli download deepseek-ai/DeepSeek-OCR-2
```

### Verify Installation

```bash
deepseek-ocr check
```

## Usage

### CLI

```bash
# Check environment (GPU, vLLM, model, etc.)
deepseek-ocr check

# Convert a scanned PDF (outputs dual-layer PDF + Markdown)
deepseek-ocr convert input.pdf

# Specify output directory
deepseek-ocr convert input.pdf -o ./results

# Use Rewrite mode (vector text + LaTeX rendering)
deepseek-ocr convert input.pdf --pdf-mode rewrite

# Batch convert all PDFs in a directory
deepseek-ocr convert ./scanned_pdfs/

# Skip Markdown generation
deepseek-ocr convert input.pdf --no-markdown

# Specify custom model path
deepseek-ocr convert input.pdf --model-path /path/to/local/model

# OCR + Translate to Chinese
deepseek-ocr convert input.pdf --translate --translation-api-key sk-xxx

# Translate only (equivalent to convert --translate)
deepseek-ocr translate input.pdf --translation-api-key sk-xxx

# Start the web server
deepseek-ocr serve
```

**CLI Options for `convert`:**

| Option | Env Variable | Default | Description |
|--------|-------------|---------|-------------|
| `--output, -o` | `OUTPUT_DIR` | `./output` | Output directory |
| `--dpi` | `PDF_DPI` | `200` | PDF rendering DPI |
| `--pdf-mode` | `PDF_OUTPUT_MODE` | `dual_layer` | Output mode: `dual_layer` or `rewrite` |
| `--no-pdf` | — | — | Skip PDF generation |
| `--no-markdown` | — | — | Skip Markdown generation |
| `--model-path` | `VLLM_MODEL_PATH` | `deepseek-ai/DeepSeek-OCR-2` | Model path (HF ID or local) |
| `--translate` | — | — | Enable translation |
| `--source-lang` | — | `English` | Source language |
| `--target-lang` | — | `Simplified Chinese` | Target language |
| `--translation-api-key` | `TRANSLATION_API_KEY` | — | Translation LLM API key |
| `--translation-base-url` | `TRANSLATION_BASE_URL` | `https://api.openai.com/v1` | Translation API URL |
| `--translation-model` | `TRANSLATION_MODEL` | `gpt-4o-mini` | Translation model |

### Web Interface

```bash
deepseek-ocr serve
# Or specify host/port:
deepseek-ocr serve --host 0.0.0.0 --port 8080
```

Then open http://localhost:8080 in your browser:

1. **Select output mode** — Choose "Dual Layer" (default) or "Rewrite"
2. **Upload PDF** — Drag and drop or click to select (supports multiple files)
3. **Watch progress** — Real-time OCR progress via SSE, with phase indicators
4. **Download results** — Searchable PDF, Markdown, and optionally translated/bilingual PDFs

### API

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/api/upload` | Upload PDF file(s) with `pdf_mode` and `translate` parameters |
| `GET` | `/api/progress/{task_id}` | SSE stream of conversion progress |
| `GET` | `/api/download/{task_id}/pdf` | Download searchable PDF |
| `GET` | `/api/download/{task_id}/markdown` | Download Markdown |
| `GET` | `/api/download/{task_id}/translated_pdf` | Download translated PDF |
| `GET` | `/api/download/{task_id}/bilingual_pdf` | Download bilingual PDF |
| `GET` | `/api/health` | Service health (GPU, model, engine status) |

## How It Works

```
PDF → PDFTypeDetector (auto-detect)
  ├─ [Scanned PDF] → PDFReader (PNG per page)
  │       │
  │       v
  │   OCREngine (vLLM + DeepSeek-OCR-2, grounding mode)
  │       │
  │       v
  │   OutputParser → TextBlocks with coordinates (0-999)
  │       │
  │       ├──> DualLayerPDFWriter → Searchable PDF
  │       ├──> MarkdownWriter → .md file
  │       └──> Translator → TranslatedPDFWriter
  │               ├──> Translated PDF ({stem}_{lang}.pdf)
  │               └──> Bilingual PDF ({stem}_bilingual.pdf)
  │
  └─ [Text PDF] → TextPDFExtractor (PyMuPDF direct extract)
          │
          └──> ParsedPage → same rendering pipeline
```

## Configuration

Copy `.env.template` to `.env` and customize:

```bash
cp .env.template .env
```

**Priority: CLI flags > `.env` / env vars > defaults.**

| Env Variable | Default | Description |
|-------------|---------|-------------|
| **vLLM Engine** | | |
| `VLLM_MODEL_PATH` | `deepseek-ai/DeepSeek-OCR-2` | Model path (HuggingFace ID or local) |
| `VLLM_GPU_MEMORY_UTILIZATION` | `0.85` | GPU memory usage ratio (0.0-1.0) |
| `VLLM_MAX_MODEL_LEN` | `8192` | Max model context length |
| `VLLM_DTYPE` | `bfloat16` | Model data type |
| `VLLM_TENSOR_PARALLEL_SIZE` | `1` | Number of GPUs for tensor parallelism |
| `VLLM_MAX_RETRIES` | `3` | Max OCR retry attempts |
| `VLLM_MAX_CONCURRENCY` | `100` | Max concurrent pages for PDF mode |
| **PDF** | | |
| `PDF_DPI` | `200` | Render resolution for page images |
| `PDF_OUTPUT_MODE` | `dual_layer` | Default output mode |
| **Web** | | |
| `WEB_PORT` | `8080` | Web server port |
| **Translation** | | |
| `TRANSLATION_API_KEY` | — | LLM API key for translation |
| `TRANSLATION_MODEL` | `gpt-4o-mini` | Translation model name |
| **Output** | | |
| `OUTPUT_DIR` | `./output` | Default output directory |

## Deployment (systemd)

```bash
# Use the provided deployment script
bash scripts/deploy.sh

# Or manually:
sudo cp scripts/deepseek-ocr.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now deepseek-ocr
```

## Project Structure

```
src/deepseek_ocr/
├── config.py              # Global configuration (VLLMConfig, PDFConfig, etc.)
├── core/
│   ├── ocr_engine.py      # vLLM + DeepSeek-OCR-2 inference engine
│   ├── output_parser.py   # Parse OCR output -> TextBlocks with coordinates
│   ├── pdf_reader.py      # PDF -> page images (PNG)
│   ├── pdf_writer.py      # Generate PDF (dual-layer / rewrite with LaTeX)
│   ├── markdown_writer.py # Generate Markdown output
│   ├── translator.py      # LLM translation (OpenAI-compatible)
│   ├── pipeline.py        # Orchestrates the full conversion flow
│   └── ...
├── cli/
│   └── main.py            # Click CLI commands (convert/translate/check/serve)
├── web/
│   ├── app.py             # FastAPI app factory with lifespan management
│   ├── routes.py          # API routes + global OCR engine singleton
│   └── static/            # Frontend (HTML/CSS/JS)
└── utils/
    └── logger.py          # Logging utilities
```

## License

MIT

---

# DeepSeek-OCR（中文文档）

基于 [DeepSeek-OCR-2](https://huggingface.co/deepseek-ai/DeepSeek-OCR-2) 模型 + [vLLM](https://github.com/vllm-project/vllm) 推理的本地 PDF OCR 工具，可将扫描英文 PDF 转化为**可搜索双层 PDF** 和 **Markdown** 文件。支持通过 OpenAI 兼容接口将 OCR 结果翻译为目标语言，生成翻译版 PDF 和双语对照 PDF。

所有处理均在本地完成，数据不出本机。

## 功能特性

- **DeepSeek-OCR-2 模型** — 全新 Visual Causal Flow 架构，DeepEncoder V2 编码器，OCR 质量大幅提升
- **vLLM 推理引擎** — 高性能 GPU 推理，Flash Attention 加速，进程内嵌引擎（无需独立服务）
- **两种 PDF 输出模式**：
  - **双层模式 (Dual Layer)** — 保留原始扫描图像，叠加不可见文字层。视觉效果与原版一致，支持搜索和文字选取
  - **重绘模式 (Rewrite)** — 文字区域用矢量字体重绘，更清晰锐利。图表、图片和表格保留原始扫描。LaTeX 公式通过 matplotlib 渲染
- **Markdown 输出** — 提取文档文本生成干净的 Markdown
- **翻译功能** — 通过 OpenAI 兼容 API 翻译 OCR 结果，生成翻译 PDF 和左右对照双语 PDF
- **文本 PDF 支持** — 自动检测文本类型 PDF（非扫描版），直接提取文字跳过 OCR
- **LaTeX 公式渲染** — 行间公式 `\[...\]` 和行内公式 `\(...\)` 均可在重绘模式下正确渲染
- **多进程并行渲染** — PDF 生成使用 `ProcessPoolExecutor(forkserver)` 实现真正的多核加速
- **OCR 结果缓存** — 按页缓存 OCR 结果（PDF MD5 为 key），重复处理无需重新 OCR
- **GPU 加速** — 需要 NVIDIA GPU + CUDA
- **CLI + Web 界面** — 命令行用于脚本/自动化，Web 界面支持拖拽上传和实时进度展示

## 系统要求

- **Python** 3.12+
- **NVIDIA GPU**，CUDA 11.8+（如 RTX 3090/4090、A100）
- **CUDA toolkit**（可选，推荐）
- **约 20 GB 磁盘空间**（模型权重 + 依赖）

## 安装

### 一键安装

```bash
git clone https://github.com/mmletgo/deepseek-ocr.git
cd deepseek-ocr
bash scripts/setup_vllm.sh
```

安装脚本会自动：
1. 检查 NVIDIA GPU 和 CUDA
2. 创建 Python 虚拟环境（`.venv`）
3. 安装 PyTorch 2.6.0 + vLLM 0.8.5 + Flash Attention
4. 获取 DeepSeek-OCR-2 推理模块
5. 验证所有组件

### 手动安装

```bash
python3.12 -m venv .venv
source .venv/bin/activate

# 安装 PyTorch
pip install torch==2.6.0 torchvision==0.21.0 --index-url https://download.pytorch.org/whl/cu118

# 安装 vLLM
pip install vllm==0.8.5

# 安装 Flash Attention（可选，推荐）
pip install flash-attn==2.7.3 --no-build-isolation

# 安装项目
pip install -e .
```

### 下载模型

模型会在首次使用时自动从 HuggingFace 下载。如需预下载：

```bash
# 国内用户设置镜像加速
export HF_ENDPOINT=https://hf-mirror.com

pip install huggingface_hub
huggingface-cli download deepseek-ai/DeepSeek-OCR-2
```

### 验证安装

```bash
deepseek-ocr check
```

## 使用方法

### 命令行

```bash
# 检查环境（GPU、vLLM、模型等）
deepseek-ocr check

# 转换扫描 PDF（输出双层 PDF + Markdown）
deepseek-ocr convert input.pdf

# 指定输出目录
deepseek-ocr convert input.pdf -o ./results

# 使用重绘模式（矢量文字 + LaTeX 渲染）
deepseek-ocr convert input.pdf --pdf-mode rewrite

# 批量转换目录中所有 PDF
deepseek-ocr convert ./scanned_pdfs/

# 不生成 Markdown
deepseek-ocr convert input.pdf --no-markdown

# 指定自定义模型路径
deepseek-ocr convert input.pdf --model-path /path/to/local/model

# OCR + 翻译为中文
deepseek-ocr convert input.pdf --translate --translation-api-key sk-xxx

# 仅翻译（等效于 convert --translate）
deepseek-ocr translate input.pdf --translation-api-key sk-xxx

# 启动 Web 服务
deepseek-ocr serve
```

**`convert` 命令参数：**

| 参数 | 环境变量 | 默认值 | 说明 |
|------|---------|--------|------|
| `--output, -o` | `OUTPUT_DIR` | `./output` | 输出目录 |
| `--dpi` | `PDF_DPI` | `200` | PDF 渲染 DPI |
| `--pdf-mode` | `PDF_OUTPUT_MODE` | `dual_layer` | 输出模式：`dual_layer` 或 `rewrite` |
| `--no-pdf` | — | — | 不生成 PDF |
| `--no-markdown` | — | — | 不生成 Markdown |
| `--model-path` | `VLLM_MODEL_PATH` | `deepseek-ai/DeepSeek-OCR-2` | 模型路径（HF ID 或本地路径） |
| `--translate` | — | — | 启用翻译 |
| `--source-lang` | — | `English` | 源语言 |
| `--target-lang` | — | `Simplified Chinese` | 目标语言 |
| `--translation-api-key` | `TRANSLATION_API_KEY` | — | 翻译 API 密钥 |
| `--translation-base-url` | `TRANSLATION_BASE_URL` | `https://api.openai.com/v1` | 翻译 API 地址 |
| `--translation-model` | `TRANSLATION_MODEL` | `gpt-4o-mini` | 翻译模型名称 |

### Web 界面

```bash
deepseek-ocr serve
# 或指定地址和端口：
deepseek-ocr serve --host 0.0.0.0 --port 8080
```

然后在浏览器打开 http://localhost:8080：

1. **选择输出模式** — 双层模式（默认）或重绘模式
2. **上传 PDF** — 拖拽或点击选择（支持多文件）
3. **查看进度** — 通过 SSE 实时显示 OCR 进度和阶段
4. **下载结果** — 可搜索 PDF、Markdown，可选翻译版/双语对照 PDF

### API 接口

| 方法 | 端点 | 说明 |
|------|------|------|
| `POST` | `/api/upload` | 上传 PDF（支持 `pdf_mode` 和 `translate` 参数） |
| `GET` | `/api/progress/{task_id}` | SSE 转换进度流 |
| `GET` | `/api/download/{task_id}/pdf` | 下载可搜索 PDF |
| `GET` | `/api/download/{task_id}/markdown` | 下载 Markdown |
| `GET` | `/api/download/{task_id}/translated_pdf` | 下载翻译 PDF |
| `GET` | `/api/download/{task_id}/bilingual_pdf` | 下载双语对照 PDF |
| `GET` | `/api/health` | 健康检查（GPU、模型、引擎状态） |

## 工作原理

```
PDF → PDFTypeDetector（自动检测类型）
  ├─ [扫描PDF] → PDFReader（逐页 PNG）
  │       │
  │       v
  │   OCREngine（vLLM + DeepSeek-OCR-2，grounding 模式）
  │       │
  │       v
  │   OutputParser → TextBlock + 坐标（0-999）
  │       │
  │       ├──> DualLayerPDFWriter → 可搜索 PDF
  │       ├──> MarkdownWriter → .md 文件
  │       └──> Translator → TranslatedPDFWriter
  │               ├──> 翻译 PDF（{stem}_{lang}.pdf）
  │               └──> 双语对照 PDF（{stem}_bilingual.pdf）
  │
  └─ [文本PDF] → TextPDFExtractor（PyMuPDF 直接提取）
          │
          └──> ParsedPage → 同一渲染管线
```

## 配置

复制 `.env.template` 为 `.env` 并按需修改：

```bash
cp .env.template .env
```

**优先级：CLI 参数 > `.env` / 环境变量 > 默认值。**

| 环境变量 | 默认值 | 说明 |
|---------|--------|------|
| **vLLM 引擎** | | |
| `VLLM_MODEL_PATH` | `deepseek-ai/DeepSeek-OCR-2` | 模型路径（HuggingFace ID 或本地路径） |
| `VLLM_GPU_MEMORY_UTILIZATION` | `0.85` | GPU 显存使用比例（0.0-1.0） |
| `VLLM_MAX_MODEL_LEN` | `8192` | 最大上下文长度 |
| `VLLM_DTYPE` | `bfloat16` | 模型数据类型 |
| `VLLM_TENSOR_PARALLEL_SIZE` | `1` | 张量并行 GPU 数量 |
| `VLLM_MAX_RETRIES` | `3` | OCR 最大重试次数 |
| `VLLM_MAX_CONCURRENCY` | `100` | PDF 模式最大并发页数 |
| **PDF** | | |
| `PDF_DPI` | `200` | 页面图像渲染分辨率 |
| `PDF_OUTPUT_MODE` | `dual_layer` | 默认输出模式 |
| **Web** | | |
| `WEB_PORT` | `8080` | Web 服务端口 |
| **翻译** | | |
| `TRANSLATION_API_KEY` | — | 翻译 API 密钥 |
| `TRANSLATION_MODEL` | `gpt-4o-mini` | 翻译模型名称 |
| **输出** | | |
| `OUTPUT_DIR` | `./output` | 默认输出目录 |

## 部署（systemd）

```bash
# 使用部署脚本
bash scripts/deploy.sh

# 或手动部署：
sudo cp scripts/deepseek-ocr.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now deepseek-ocr
```

## 许可证

MIT
