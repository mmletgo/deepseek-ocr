# DeepSeek-OCR 项目

使用本地 DeepSeek-OCR 模型将英文扫描 PDF 转化为可搜索双层 PDF 和 Markdown 文件的工具。

## 技术栈
- **模型推理**: Ollama + deepseek-ocr (Apple Silicon Metal 加速)
- **PDF处理**: PyMuPDF (pymupdf)
- **CLI**: Click + Rich
- **Web**: FastAPI + SSE + 原生前端
- **Python**: 3.12+, 严格类型定义

## 项目结构

```
src/deepseek_ocr/
├── config.py          # 全局配置 (AppConfig, OllamaConfig, PDFConfig, WebConfig, PDFOutputMode)
├── core/              # 核心引擎 → 见 core/CLAUDE.md
├── cli/               # CLI命令行 → 见 cli/CLAUDE.md
├── web/               # Web界面 → 见 web/CLAUDE.md
└── utils/             # 工具模块 (logger)
```

## 数据流
```
扫描PDF → PDFReader(逐页PNG) → OCREngine(Ollama) → OutputParser(TextBlock+坐标)
                                                        ├→ DualLayerPDFWriter → 双层PDF
                                                        └→ MarkdownWriter → .md文件
```

## 核心概念
- **PDF输出模式** (`PDFOutputMode` 枚举):
  - `dual_layer`: 底层原始扫描图像 + 上层透明文字层(render_mode=3)，视觉不变但可搜索
  - `rewrite`: 文字区域白色遮盖 + 矢量字体重绘(render_mode=0)，图表/公式保持原始扫描
- **归一化坐标**: DeepSeek-OCR输出坐标范围0-999，需转换为PDF坐标: `pdf_coord = model_coord / 999 * page_dimension`
- **grounding模式**: 使用 `<|grounding|>` 前缀触发带坐标的OCR输出

## 使用方式
```bash
# CLI
deepseek-ocr check              # 检查环境
deepseek-ocr convert input.pdf  # 转换PDF
deepseek-ocr serve              # 启动Web服务

# Web: 浏览器访问 http://localhost:8080
```

## 依赖
- ollama (SDK) + Ollama 服务 + deepseek-ocr 模型
- PyMuPDF, Click, Rich, FastAPI, uvicorn, sse-starlette, python-multipart
