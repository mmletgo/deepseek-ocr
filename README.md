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
