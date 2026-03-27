# core/ - 核心引擎模块

## 模块职责

| 文件 | 职责 |
|------|------|
| `pdf_reader.py` | 将扫描PDF每页渲染为PNG图片 (PDFReader, PageImage) |
| `ocr_engine.py` | 通过Ollama调用DeepSeek-OCR模型 (OCREngine, OCRResult, PromptMode) |
| `output_parser.py` | 解析ref/det坐标标签 (OutputParser, TextBlock, ParsedPage) |
| `pdf_writer.py` | 生成PDF: 支持dual_layer(透明文字层)和rewrite(矢量重绘)两种模式 (DualLayerPDFWriter) |
| `markdown_writer.py` | 输出Markdown文件 (MarkdownWriter) |
| `pipeline.py` | 端到端编排所有模块 (ConversionPipeline, ConversionResult) |

## 关键数据结构
- `PageImage`: PDF单页的PNG图片数据 + 尺寸元信息
- `OCRResult`: 单页OCR结果 (raw_text含ref/det标签)
- `TextBlock`: 单个文本区域 (text + label + bbox归一化坐标)
- `ParsedPage`: 单页解析结果 (blocks列表 + 清理后的文本)
- `ConversionResult`: 最终转换结果 (输出路径 + 状态)

## PDF生成技术要点
- `page.insert_image(rect, stream=bytes, overlay=False)` 插入底层图像
- `tw.append((x, y), text, font, fontsize)` 逐行放置文字（避免fill_textbox无限循环bug）
- `tw.write_text(page, render_mode=3)` 不可见文字层(dual_layer) / `render_mode=0` 可见(rewrite)
- 坐标转换: `pdf_coord = bbox[i] / 999.0 * page_dimension`
- 页面渲染通过 ProcessPoolExecutor(forkserver) 多进程并行
- `_KEEP_ORIGINAL_LABELS = {"image", "table", "formula"}`: rewrite模式下保持原始扫描效果的标签
- rewrite模式：`page.draw_rect(rect, fill=(1,1,1))` 白色遮盖文字区域后重绘矢量文字

## 降级策略
OCR输出无坐标标签时 → 整页文本作为单个TextBlock(bbox=[0,0,999,999])
