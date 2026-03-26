# core/ - 核心引擎模块

## 模块职责

| 文件 | 职责 |
|------|------|
| `pdf_reader.py` | 将扫描PDF每页渲染为PNG图片 (PDFReader, PageImage) |
| `ocr_engine.py` | 通过Ollama调用DeepSeek-OCR模型 (OCREngine, OCRResult, PromptMode) |
| `output_parser.py` | 解析ref/det坐标标签 (OutputParser, TextBlock, ParsedPage) |
| `pdf_writer.py` | 生成双层PDF: 图像底层+透明文字层 (DualLayerPDFWriter) |
| `markdown_writer.py` | 输出Markdown文件 (MarkdownWriter) |
| `pipeline.py` | 端到端编排所有模块 (ConversionPipeline, ConversionResult) |

## 关键数据结构
- `PageImage`: PDF单页的PNG图片数据 + 尺寸元信息
- `OCRResult`: 单页OCR结果 (raw_text含ref/det标签)
- `TextBlock`: 单个文本区域 (text + label + bbox归一化坐标)
- `ParsedPage`: 单页解析结果 (blocks列表 + 清理后的文本)
- `ConversionResult`: 最终转换结果 (输出路径 + 状态)

## 双层PDF技术要点
- `page.insert_image(rect, stream=bytes, overlay=False)` 插入底层图像
- `TextWriter.fill_textbox(rect, text, font, fontsize)` 填充文字
- `tw.write_text(page, render_mode=3)` 写入不可见文字层
- 坐标转换: `pdf_coord = bbox[i] / 999.0 * page_dimension`

## 降级策略
OCR输出无坐标标签时 → 整页文本作为单个TextBlock(bbox=[0,0,999,999])
