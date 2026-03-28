# core/ - 核心引擎模块

## 模块职责

| 文件 | 职责 |
|------|------|
| `pdf_reader.py` | 将扫描PDF每页渲染为PNG图片 (PDFReader, PageImage) |
| `ocr_engine.py` | 通过Ollama调用DeepSeek-OCR模型 (OCREngine, OCRResult, PromptMode) |
| `output_parser.py` | 解析ref/det坐标标签 (OutputParser, TextBlock, ParsedPage) |
| `pdf_writer.py` | 生成PDF: 支持dual_layer(透明文字层)和rewrite(矢量重绘)两种模式 (DualLayerPDFWriter) |
| `markdown_writer.py` | 输出Markdown文件 (MarkdownWriter) |
| `translator.py` | 通过 OpenAI 兼容接口调用 LLM 翻译文本块 (Translator, TranslatedPage) |
| `translated_pdf_writer.py` | 生成翻译版PDF: 目标语言PDF + 双语对照PDF (TranslatedPDFWriter) |
| `translation_cache.py` | 翻译结果持久化缓存，按 PDF MD5 + 语言对存储 (TranslationCache) |
| `pipeline.py` | 端到端编排所有模块 (ConversionPipeline, ConversionResult) |

## 关键数据结构
- `PageImage`: PDF单页的PNG图片数据 + 尺寸元信息
- `OCRResult`: 单页OCR结果 (raw_text含ref/det标签)
- `TextBlock`: 单个文本区域 (text + label + bbox归一化坐标)
- `ParsedPage`: 单页解析结果 (blocks列表 + 清理后的文本)
- `TranslatedPage`: 单页翻译结果 (original ParsedPage + translated_blocks + success状态)
- `ConversionResult`: 最终转换结果 (输出路径 + 状态)

## PDF生成技术要点
- `page.insert_image(rect, stream=bytes, overlay=False)` 插入底层图像
- `tw.append((x, y), text, font, fontsize)` 逐行放置文字（避免fill_textbox无限循环bug）
- `tw.write_text(page, render_mode=3)` 不可见文字层(dual_layer) / `render_mode=0` 可见(rewrite)
- 坐标转换: `pdf_coord = bbox[i] / 999.0 * page_dimension`
- 页面渲染通过 ProcessPoolExecutor(forkserver) 多进程并行
- `_KEEP_ORIGINAL_LABELS = {"image", "table"}`: rewrite模式下保持原始扫描效果的标签

## rewrite 模式三路径渲染
- `equation`/`formula` 块 → matplotlib mathtext 渲染 LaTeX → PNG 嵌入 + 不可见搜索层
- 含内联 LaTeX(`\(...\)`) 的文本 → matplotlib 混合渲染 → PNG 嵌入 + 不可见搜索层
- 纯文本 → `tw.append()` 矢量文字 + `_wrap_line()` 自动换行
- 两个 TextWriter：`tw_visible`(render_mode=0) + `tw_search`(render_mode=3)
- matplotlib 使用 Agg 后端，子进程安全
- CJK 字体支持：`_render_text_image()` 检测 CJK 字符时通过 `FontProperties(fname=...)` 加载系统 CJK 字体（`_find_cjk_font_path()` 通过 fontconfig 或回退路径查找），避免 matplotlib 默认字体不支持 CJK 导致乱码

## 翻译引擎技术要点
- 批量翻译：将可翻译 blocks 按 [N] 编号组装到单个 prompt，一次 API 调用完成
- 跳过标签：`_SKIP_LABELS = {"formula", "equation", "image", "table"}` 不翻译
- 回退策略：编号解析数量不匹配时，逐 block 单独调用 API 翻译
- 重试机制：指数退避（2s, 4s, 8s...），最多 max_retries 次
- 同步/异步双接口：`translate_page` / `translate_page_async`
- 错误隔离：单个 block 翻译失败保留原文，不影响其他 block

## 翻译PDF生成技术要点
- 两种输出模式：目标语言PDF（白色遮盖+翻译文字重绘）、双语对照PDF（左原图右翻译）
- CJK字体：`pymupdf.Font(ordering=N)` — 简中0/繁中1/日语2/韩语3，其他语言用helv
- CJK换行：`_wrap_line_cjk()` 逐字符断行，非CJK部分按单词边界断行
- 跳过标签：`_SKIP_LABELS = {"formula", "equation", "image", "table"}` 保持原始扫描
- 搜索层：翻译PDF写入原文到不可见搜索层（helv, fontsize=3, render_mode=3）
- 双语对照：页面宽度翻倍，左半页原图，右半页白色背景+翻译文字，中间灰色分隔线
- 双语对照公式/图表保留：skip-label 块从原始扫描 Pixmap 裁切对应区域，复制到右半页对应位置（`Pixmap.copy()` + `set_origin(0,0)` + `insert_image()`）
- 多进程并行：ProcessPoolExecutor(forkserver)，worker函数为模块级函数
- 数据序列化：TextBlock → dict 跨进程传递（避免pickle问题）
- 三路径渲染（复用 pdf_writer.py 的 `_contains_latex`, `_clean_markdown`, `_render_text_image`, `_strip_latex`）：
  - 非 CJK 语言 + 含内联 LaTeX → `_render_text_image()` matplotlib 渲染为 PNG 嵌入，失败静默回退纯文本
  - CJK 语言 + 含内联 LaTeX → 跳过 matplotlib（不支持 CJK 换行），`_strip_latex()` 剥离定界符后走纯文本渲染
  - 纯文本 → `_clean_markdown()` 清理 Markdown 标记（如 `####` 标题）后矢量文字渲染
  - 目标语言PDF：LaTeX 成功时写入不可见搜索层；双语对照PDF：LaTeX 渲染 PNG 嵌入右半页

## 降级策略
OCR输出无坐标标签时 → 整页文本作为单个TextBlock(bbox=[0,0,999,999])
