# DeepSeek-OCR 项目

使用 DeepSeek-OCR-2 模型 (vLLM推理) 将英文扫描 PDF 转化为可搜索双层 PDF 和 Markdown 文件的工具。支持通过 OpenAI 兼容 LLM 接口将 OCR 结果翻译为目标语言，生成翻译版 PDF 和双语对照 PDF。同时支持文本类型PDF（非扫描版），自动检测PDF类型，跳过OCR直接提取文本进行翻译。

## 技术栈
- **模型推理**: vLLM + DeepSeek-OCR-2 (NVIDIA CUDA, 进程内嵌引擎)
- **翻译**: OpenAI 兼容 API (openai SDK)
- **PDF处理**: PyMuPDF (pymupdf)
- **CLI**: Click + Rich
- **Web**: FastAPI + SSE + 原生前端
- **Python**: 3.12+, 严格类型定义
- **平台**: Linux (Ubuntu) + NVIDIA GPU

## 项目结构

```
src/deepseek_ocr/
├── config.py          # 全局配置 (AppConfig, VLLMConfig, PDFConfig, WebConfig, TranslationConfig, PDFOutputMode)
├── core/              # 核心引擎 → 见 core/CLAUDE.md
├── cli/               # CLI命令行 → 见 cli/CLAUDE.md
├── web/               # Web界面 → 见 web/CLAUDE.md
└── utils/             # 工具模块 (logger)
.env.template          # 环境变量配置模板（复制为 .env 使用）
```

## 数据流
```
PDF → PDFTypeDetector(自动检测类型)
  ├─ [扫描PDF] → PDFReader(逐页PNG) → OCREngine(vLLM+DeepSeek-OCR-2) → OutputParser(TextBlock+坐标)
  │                                                          ├→ DualLayerPDFWriter → 双层PDF
  │                                                          ├→ MarkdownWriter → .md文件
  │                                                          └→ Translator → TranslatedPDFWriter
  │                                                                ├→ 目标语言PDF ({stem}_{lang}.pdf)
  │                                                                └→ 双语对照PDF ({stem}_bilingual.pdf)
  └─ [文本PDF] → TextPDFExtractor(PyMuPDF直接提取) → ParsedPage
                                                       ├→ MarkdownWriter → .md文件
                                                       └→ Translator → TextPDFTranslatedWriter(show_pdf_page保留矢量)
                                                             ├→ 目标语言PDF ({stem}_{lang}.pdf)
                                                             └→ 双语对照PDF ({stem}_bilingual.pdf)
```

## 核心概念
- **PDF输出模式** (`PDFOutputMode` 枚举):
  - `dual_layer`: 底层原始扫描图像 + 上层透明文字层(render_mode=3)，视觉不变但可搜索
  - `rewrite`: 文字区域白色遮盖 + 矢量字体重绘(render_mode=0)，图表/公式保持原始扫描
- **归一化坐标**: DeepSeek-OCR-2输出坐标范围0-999，需转换为PDF坐标: `pdf_coord = model_coord / 999 * page_dimension`
- **vLLM推理引擎**: 进程内嵌vLLM引擎，CLI用同步`LLM`，Web用`AsyncLLMEngine`全局单例
  - 自定义模型注册: `DeepseekOCR2ForCausalLM` → vLLM ModelRegistry
  - 图像预处理: `DeepseekOCR2Processor.tokenize_with_images()` 动态裁剪+编码
  - 重复抑制: `NoRepeatNGramLogitsProcessor` 防止n-gram重复
  - 环境变量: `VLLM_USE_V1=0` (必须使用v0引擎)
- **PDF类型自动检测**: `PDFTypeDetector` 采样前5页统计文本字符数，>100字符/页判定为文本PDF
  - 文本PDF：跳过OCR，用 `TextPDFExtractor` 通过 PyMuPDF `get_text("dict")` 直接提取文本+坐标
  - 扫描PDF：走原有OCR流程
  - 两条路径在 `ParsedPage` 层面汇合，翻译/Markdown/缓存完全复用
- **文本PDF翻译输出**: `TextPDFTranslatedWriter` 使用 `show_pdf_page()` 保留原始矢量内容（非扫描PNG）
- **grounding模式**: 使用 `<image>\n<|grounding|>` 前缀触发带坐标的OCR输出
- **翻译功能**:
  - 通过 OpenAI 兼容 API 逐页翻译 OCR 文本
  - 整页编号化提交（保持上下文），解析失败回退逐 block 翻译
  - 跳过 formula/equation/image/table 标签（保持原文不翻译）
  - 目标语言 PDF：底层扫描图 + 白色遮盖 + CJK字体翻译文字
  - 双语对照 PDF：左半页原始扫描，右半页翻译文字（双倍宽度页面）
  - CJK 字体：PyMuPDF `ordering` 参数（简中=0, 繁中=1, 日语=2, 韩语=3）
  - 翻译配置：base_url / api_key / model（支持环境变量 + CLI参数覆盖）
- **配置优先级**: CLI 参数 > .env 文件 / 环境变量 > 代码默认值
  - 通过 python-dotenv 在 config.py 模块加载时读取项目根目录 `.env` 文件
  - 所有 dataclass 字段均使用 `field(default_factory=lambda: os.getenv(...))` 模式
  - CLI 参数默认值为 None，非 None 时覆盖 .env 配置
  - 环境变量命名: VLLM_*, PDF_*, WEB_*, TRANSLATION_*, OUTPUT_DIR

## 使用方式
```bash
# CLI
deepseek-ocr check              # 检查环境
deepseek-ocr convert input.pdf  # 转换PDF
deepseek-ocr serve              # 启动Web服务
deepseek-ocr convert input.pdf --translate --translation-api-key sk-xxx  # OCR+翻译
deepseek-ocr translate input.pdf --translation-api-key sk-xxx            # 翻译（等效）

# Web: 浏览器访问 http://localhost:8080，勾选 "Translate after OCR"
```

## 依赖
- vllm==0.8.5 + torch>=2.6.0 + DeepSeek-OCR-2 模型 (HuggingFace)
- DeepSeek-OCR-2 推理模块 (从GitHub仓库获取: deepseek_ocr2, process.image_process, process.ngram_norepeat)
- flash-attn>=2.7.3 (可选，性能优化)
- openai (SDK) — LLM翻译（OpenAI兼容接口）
- python-dotenv — .env 文件加载
- PyMuPDF, Click, Rich, FastAPI, uvicorn, sse-starlette, python-multipart
- transformers>=4.46.3, huggingface_hub
