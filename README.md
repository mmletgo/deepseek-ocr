# DeepSeek-OCR

A local-first tool that converts scanned English PDFs into **searchable dual-layer PDFs** and **Markdown** files, powered by the [DeepSeek-OCR](https://ollama.com/library/deepseek-ocr) model running on [Ollama](https://ollama.com).

All processing happens locally on your machine — no cloud API calls, no data leaves your device.

## Features

- **Dual-layer PDF output** — Original scanned images preserved with an invisible, searchable text layer on top. Visually identical to the original, but fully searchable and selectable.
- **Markdown output** — Clean Markdown extraction of document text.
- **Accurate text positioning** — Uses DeepSeek-OCR's grounding mode to get precise bounding-box coordinates for each text block, so the searchable text layer aligns with the original scan.
- **Apple Silicon accelerated** — Leverages Metal GPU acceleration through Ollama on macOS.
- **CLI + Web interface** — Use the command line for scripting/automation, or the drag-and-drop web UI for a visual workflow.
- **Real-time progress** — Web UI shows live OCR progress via Server-Sent Events (SSE).

## Screenshots

**Web Interface**

Upload a scanned PDF via drag-and-drop, watch real-time progress, and download results:

```
┌─────────────────────────────────────┐
│         DeepSeek-OCR                │
│                                     │
│   ┌─────────────────────────────┐   │
│   │  Drag and drop PDF here     │   │
│   │  or click to select a file  │   │
│   └─────────────────────────────┘   │
│                                     │
│   Converting... ████████░░░░ 67%    │
│   Processing page 4 of 6           │
│                                     │
│   [Download PDF]  [Download MD]     │
└─────────────────────────────────────┘
```

## Requirements

- **Python** 3.12+
- **Ollama** installed and running
- **deepseek-ocr** model pulled (~6.7 GB)

## Installation

### 1. Set up Ollama and the model

```bash
# Quick setup (macOS/Linux)
bash scripts/setup_ollama.sh

# Or manually:
brew install ollama        # macOS
ollama serve               # Start the service
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

# Start the web server
deepseek-ocr serve
```

### Web Interface

```bash
deepseek-ocr serve
```

Then open http://localhost:8080 in your browser. Drag and drop a PDF file, and the tool will:

1. Upload and process each page through the OCR model
2. Show real-time progress
3. Provide download links for the searchable PDF and Markdown output

### API

The web server exposes a REST API:

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/api/upload` | Upload a PDF file, returns `task_id` |
| `GET` | `/api/progress/{task_id}` | SSE stream of conversion progress |
| `GET` | `/api/download/{task_id}/pdf` | Download the searchable PDF result |
| `GET` | `/api/download/{task_id}/markdown` | Download the Markdown result |
| `GET` | `/api/health` | Service health check |

## How It Works

```
Scanned PDF
    │
    ▼
PDFReader ─── Renders each page as a PNG image
    │
    ▼
OCREngine ─── Sends image to DeepSeek-OCR via Ollama (grounding mode)
    │
    ▼
OutputParser ─── Extracts text blocks with normalized coordinates (0–999)
    │
    ├──▶ DualLayerPDFWriter ─── Overlays invisible text on original images → Searchable PDF
    │
    └──▶ MarkdownWriter ─── Formats extracted text → Markdown file
```

**Dual-layer PDF explained**: The output PDF contains the original scanned image as the visible layer, with an invisible text layer (using PDF render mode 3) positioned on top. This means the PDF looks exactly like the original scan, but you can search, select, and copy text from it.

## Configuration

Configuration is managed through `src/deepseek_ocr/config.py` with sensible defaults:

| Config | Default | Env Variable | Description |
|--------|---------|-------------|-------------|
| Ollama host | `http://localhost:11434` | `OLLAMA_HOST` | Ollama service URL |
| Model | `deepseek-ocr` | — | OCR model name |
| PDF DPI | `200` | — | Render resolution for page images |
| Web port | `8080` | — | Web server port |
| Max upload | `200 MB` | — | Maximum PDF upload size |

## Project Structure

```
src/deepseek_ocr/
├── config.py              # Global configuration
├── core/
│   ├── pdf_reader.py      # PDF → page images
│   ├── ocr_engine.py      # Ollama model inference
│   ├── output_parser.py   # Parse OCR output → text blocks
│   ├── pdf_writer.py      # Generate dual-layer searchable PDF
│   ├── markdown_writer.py # Generate Markdown output
│   └── pipeline.py        # Orchestrates the full conversion flow
├── cli/
│   └── main.py            # Click CLI commands
├── web/
│   ├── app.py             # FastAPI application
│   ├── routes.py          # API route handlers
│   └── static/            # Frontend (HTML/CSS/JS)
└── utils/
    └── logger.py          # Logging utilities
```

## License

MIT
