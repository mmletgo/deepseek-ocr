# DeepSeek-OCR

A local-first tool that converts scanned English PDFs into **searchable dual-layer PDFs** and **Markdown** files, powered by the [DeepSeek-OCR](https://ollama.com/library/deepseek-ocr) model running on [Ollama](https://ollama.com).

All processing happens locally on your machine — no cloud API calls, no data leaves your device.

## Features

- **Two PDF output modes**:
  - **Dual Layer** — Original scanned images preserved with an invisible, searchable text layer. Visually identical to the original, fully searchable and selectable.
  - **Rewrite** — Text areas are redrawn with vector fonts for crisp, clear text. Charts, images, and tables are preserved from the original scan. LaTeX formulas are rendered with matplotlib's mathtext engine.
- **Markdown output** — Clean Markdown extraction of document text.
- **LaTeX formula rendering** — Mathematical equations (both display `\[...\]` and inline `\(...\)`) are rendered as proper math symbols in Rewrite mode, with automatic fallback to original scan for unsupported formulas.
- **Multi-process parallel rendering** — PDF generation uses `ProcessPoolExecutor` with forkserver for true multi-core acceleration.
- **OCR result caching** — Per-page OCR results are cached by PDF MD5 hash, enabling instant re-generation without re-running OCR.
- **GPU accelerated** — Supports NVIDIA CUDA (Linux), Apple Silicon Metal (macOS), and CPU fallback.
- **CLI + Web interface** — Command line for scripting/automation, or drag-and-drop web UI with real-time progress via SSE.

## Requirements

- **Python** 3.12+
- **Ollama** installed and running
- **deepseek-ocr** model pulled (~6.7 GB)

### Platform Support

| Platform | GPU Acceleration |
|----------|-----------------|
| Linux (Ubuntu) | NVIDIA CUDA |
| macOS | Apple Silicon Metal |
| Any | CPU (slower) |

## Installation

### 1. Set up Ollama and the model

```bash
# Install Ollama (see https://ollama.com for your platform)
# Linux:
curl -fsSL https://ollama.com/install.sh | sh

# macOS:
brew install ollama

# Start Ollama and pull the model
ollama serve               # Start the service (or use systemd)
ollama pull deepseek-ocr   # Download the model (~6.7GB)
```

### 2. Install DeepSeek-OCR

```bash
git clone https://github.com/mmletgo/deepseek-ocr.git
cd deepseek-ocr
pip install -e .
```

For development:

```bash
pip install -e ".[dev]"
```

## Usage

### CLI

```bash
# Check that Ollama and the model are ready
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

# Start the web server
deepseek-ocr serve
```

**CLI Options for `convert`:**

| Option | Default | Description |
|--------|---------|-------------|
| `--output, -o` | `./output` | Output directory |
| `--dpi` | `200` | PDF rendering DPI |
| `--pdf-mode` | `dual_layer` | Output mode: `dual_layer` or `rewrite` |
| `--no-pdf` | — | Skip PDF generation |
| `--no-markdown` | — | Skip Markdown generation |
| `--model` | `deepseek-ocr` | Ollama model name |
| `--ollama-host` | `http://localhost:11434` | Ollama service URL |

### Web Interface

```bash
deepseek-ocr serve
# Or specify host/port:
deepseek-ocr serve --host 0.0.0.0 --port 8080
```

Then open http://localhost:8080 in your browser:

1. **Select output mode** — Choose "Dual Layer" (default) or "Rewrite" using the radio buttons
2. **Upload PDF** — Drag and drop or click to select (supports multiple files)
3. **Watch progress** — Real-time OCR progress via SSE, with phase indicators
4. **Download results** — Searchable PDF and Markdown files

### PDF Output Modes

#### Dual Layer (default)

The output PDF contains the original scanned image as the visible layer, with an invisible text layer (PDF render mode 3) positioned on top. The PDF looks exactly like the original scan, but you can search, select, and copy text.

#### Rewrite

Text areas are covered with white rectangles and redrawn with vector fonts. This produces cleaner, crisper text compared to the original scan. Special handling:

| Content Type | Rendering |
|-------------|-----------|
| Plain text | Vector font (Helvetica) with auto line-wrapping |
| Display equations (`\[...\]`) | Rendered as math symbols via matplotlib |
| Inline math (`\(...\)`) | Mixed text + math rendering via matplotlib |
| Images & tables | Preserved from original scan |
| Failed LaTeX formulas | Falls back to original scan (equations) or OCR text (inline) |

### API

The web server exposes a REST API:

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/api/upload` | Upload PDF file(s) with `pdf_mode` parameter, returns task IDs |
| `GET` | `/api/progress/{task_id}` | SSE stream of conversion progress |
| `GET` | `/api/download/{task_id}/pdf` | Download the searchable PDF result |
| `GET` | `/api/download/{task_id}/markdown` | Download the Markdown result |
| `GET` | `/api/health` | Service health check (Ollama + model status) |

**Upload example:**

```bash
curl -X POST http://localhost:8080/api/upload \
  -F "files=@input.pdf" \
  -F "pdf_mode=rewrite"
```

## How It Works

```
Scanned PDF
    |
    v
PDFReader --- Renders each page as a PNG image
    |
    v
OCREngine --- Sends image to DeepSeek-OCR via Ollama (grounding mode)
    |
    v
OutputParser --- Extracts text blocks with normalized coordinates (0-999)
    |
    |---> DualLayerPDFWriter --- Multi-process parallel rendering --> Searchable PDF
    |         |-- dual_layer: invisible text over original images
    |         |-- rewrite: white cover + vector text / LaTeX rendering
    |
    |---> MarkdownWriter --- Formats extracted text --> Markdown file
    |
    |---> OCR Cache --- Per-page results cached by PDF MD5 hash
```

### Performance

- **Multi-process rendering**: PDF pages are rendered in parallel using `ProcessPoolExecutor` with forkserver context. Workers = CPU cores - 2.
- **OCR caching**: Results are persisted to `uploads/ocr_cache/{pdf_md5}/page_NNNN.json`. Re-uploading the same PDF skips OCR entirely.
- **Concurrency control**: GPU OCR is serialized (Semaphore(1)) to prevent VRAM overflow. PDF generation is also serialized to avoid PyMuPDF GIL contention.

## Configuration

Configuration is managed through `src/deepseek_ocr/config.py` with sensible defaults:

| Config | Default | Env Variable | Description |
|--------|---------|-------------|-------------|
| Ollama host | `http://localhost:11434` | `OLLAMA_HOST` | Ollama service URL |
| Model | `deepseek-ocr` | — | OCR model name |
| PDF DPI | `200` | — | Render resolution for page images |
| PDF mode | `dual_layer` | — | Default output mode |
| Web port | `8080` | — | Web server port |
| Max upload | `200 MB` | — | Maximum PDF upload size |

## Deployment (systemd)

To run as a system service on Linux:

```bash
sudo tee /etc/systemd/system/deepseek-ocr.service << 'EOF'
[Unit]
Description=DeepSeek-OCR Web Service
After=network.target ollama.service

[Service]
Type=simple
User=your-user
WorkingDirectory=/path/to/deepseek-ocr
ExecStart=/path/to/deepseek-ocr/.venv/bin/deepseek-ocr serve --host 0.0.0.0 --port 8080
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now deepseek-ocr
```

## Project Structure

```
src/deepseek_ocr/
├── config.py              # Global configuration + PDFOutputMode enum
├── core/
│   ├── pdf_reader.py      # PDF -> page images (PNG)
│   ├── ocr_engine.py      # Ollama model inference
│   ├── output_parser.py   # Parse OCR output -> TextBlocks with coordinates
│   ├── pdf_writer.py      # Generate PDF (dual-layer / rewrite with LaTeX)
│   ├── markdown_writer.py # Generate Markdown output
│   ├── ocr_cache.py       # Per-page OCR result persistence
│   └── pipeline.py        # Orchestrates the full conversion flow
├── cli/
│   └── main.py            # Click CLI commands (convert/check/serve)
├── web/
│   ├── app.py             # FastAPI application factory
│   ├── routes.py          # API route handlers + SSE progress
│   └── static/            # Frontend (HTML/CSS/JS)
└── utils/
    └── logger.py          # Logging utilities
```

## License

MIT
