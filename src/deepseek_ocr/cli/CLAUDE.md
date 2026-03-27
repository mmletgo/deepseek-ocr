# cli/ - 命令行界面

## 文件
- `main.py`: Click命令组，包含3个子命令

## 命令

| 命令 | 功能 |
|------|------|
| `convert <path>` | 转换PDF文件或目录，支持 --output/-o, --dpi, --no-pdf, --no-markdown, --model, --ollama-host, --pdf-mode |
| `check` | 检查Ollama服务和模型状态 |
| `serve` | 启动FastAPI Web服务，支持 --host, --port |

## 入口点
`pyproject.toml` 中定义: `deepseek-ocr = "deepseek_ocr.cli.main:cli"`

## 依赖
- `ConversionPipeline` (core/pipeline.py) 执行转换
- `OCREngine.check_health()` 检查环境
- Rich Progress/Table/Panel 美化输出
