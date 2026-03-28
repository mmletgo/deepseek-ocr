# cli/ - 命令行界面

## 文件
- `main.py`: Click命令组，包含4个子命令

## 命令

| 命令 | 功能 |
|------|------|
| `convert <path>` | 转换PDF文件或目录，支持 --output/-o, --dpi, --no-pdf, --no-markdown, --model, --ollama-host, --pdf-mode, --translate, --source-lang, --target-lang, --translation-model, --translation-base-url, --translation-api-key |
| `translate <path>` | 翻译扫描PDF（等效于 convert --translate），支持 --output/-o, --source-lang, --target-lang, --dpi, --model, --ollama-host, --translation-model, --translation-base-url, --translation-api-key |
| `check` | 检查Ollama服务和模型状态 |
| `serve` | 启动FastAPI Web服务，支持 --host, --port |

## 内部函数
- `_collect_pdf_files(input_path)`: 收集单文件或目录中的PDF列表
- `_run_convert(...)`: 公共转换逻辑，供 convert 和 translate 子命令复用；包含构建 AppConfig/TranslationConfig、逐文件调用 ConversionPipeline、显示进度和结果摘要

## 配置优先级
1. CLI参数（所有可覆盖参数默认值为 None，非 None 时生效）
2. .env 文件 / 环境变量（通过 AppConfig 的 dataclass default_factory 读取）
3. 代码默认值

## 入口点
`pyproject.toml` 中定义: `deepseek-ocr = "deepseek_ocr.cli.main:cli"`

## 依赖
- `ConversionPipeline` (core/pipeline.py) 执行转换
- `OCREngine.check_health()` 检查环境
- `TranslationConfig` (config.py) 翻译配置
- Rich Progress/Table/Panel 美化输出
