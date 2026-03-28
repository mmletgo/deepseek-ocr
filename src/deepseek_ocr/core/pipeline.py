# -*- coding: utf-8 -*-
"""
Business Logic:
    端到端编排所有OCR处理模块，提供一键式PDF转换功能。
    用户只需指定输入PDF和输出目录，pipeline自动完成所有步骤：
    PDF读取 -> OCR识别 -> 文本解析 -> 双层PDF生成 -> Markdown生成。

Code Logic:
    初始化所有子模块(PDFReader, OCREngine, OutputParser, DualLayerPDFWriter, MarkdownWriter)，
    按顺序调用各模块处理，支持进度回调和同步/异步两种接口。
"""

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from deepseek_ocr.config import AppConfig
from deepseek_ocr.core.pdf_reader import PDFReader, PageImage
from deepseek_ocr.core.ocr_engine import OCREngine, OCRResult, PromptMode
from deepseek_ocr.core.output_parser import OutputParser, ParsedPage
from deepseek_ocr.core.pdf_writer import DualLayerPDFWriter
from deepseek_ocr.core.markdown_writer import MarkdownWriter
from deepseek_ocr.core.translator import Translator, TranslatedPage
from deepseek_ocr.core.translated_pdf_writer import TranslatedPDFWriter
from deepseek_ocr.core.pdf_type_detector import PDFTypeDetector
from deepseek_ocr.core.text_pdf_extractor import TextPDFExtractor
from deepseek_ocr.core.text_pdf_translated_writer import TextPDFTranslatedWriter
from deepseek_ocr.utils.logger import logger


@dataclass
class ConversionResult:
    """PDF转换的最终结果"""
    source_pdf: Path          # 源PDF路径
    output_pdf: Path | None   # 输出双层PDF路径(未生成时为None)
    output_markdown: Path | None  # 输出Markdown路径(未生成时为None)
    page_count: int           # 处理的页数
    success: bool             # 转换是否成功
    error_msg: str | None = None  # 错误信息(成功时为None)
    elapsed_seconds: float = 0.0  # 总耗时(秒)
    output_translated_pdf: Path | None = None    # 翻译后PDF路径
    output_bilingual_pdf: Path | None = None     # 双语对照PDF路径
    translation_error: str | None = None         # 翻译错误（不影响OCR结果）
    pdf_type: str = "scanned"  # "text" | "scanned"


class ConversionPipeline:
    """端到端PDF转换流水线，编排所有OCR处理模块"""

    def __init__(
        self,
        config: AppConfig,
        progress_callback: Callable[[int, int, str], None] | None = None,
    ) -> None:
        """
        Business Logic:
            初始化转换流水线，创建所有子模块实例。
            progress_callback用于向上层(CLI/Web)报告处理进度。
            如果配置了翻译API密钥，还会初始化Translator和TranslatedPDFWriter。

        Code Logic:
            根据AppConfig初始化PDFReader(使用pdf配置)、OCREngine(使用ollama配置)、
            OutputParser、DualLayerPDFWriter和MarkdownWriter。
            如果translation.api_key非空，初始化Translator和TranslatedPDFWriter。
            保存进度回调函数。
        """
        self.config: AppConfig = config
        self.progress_callback: Callable[[int, int, str], None] | None = progress_callback
        self.pdf_mode: str = config.pdf.output_mode.value

        self.pdf_reader: PDFReader = PDFReader(
            dpi=config.pdf.dpi,
            max_dimension=config.pdf.max_dimension,
        )
        self.ocr_engine: OCREngine = OCREngine(config.ollama)
        self.parser: OutputParser = OutputParser()
        self.pdf_writer: DualLayerPDFWriter = DualLayerPDFWriter()
        self.markdown_writer: MarkdownWriter = MarkdownWriter()

        if config.translation.api_key:
            self.translator: Translator | None = Translator(config.translation)
            self.translated_pdf_writer: TranslatedPDFWriter | None = TranslatedPDFWriter()
        else:
            self.translator = None
            self.translated_pdf_writer = None

        logger.info("ConversionPipeline初始化完成")

    def _report_progress(self, current: int, total: int, message: str) -> None:
        """
        Business Logic:
            向上层报告处理进度，用于在CLI或Web界面显示进度条。

        Code Logic:
            如果设置了progress_callback，调用它传递当前页/总页数/消息。
        """
        logger.info(f"进度: [{current}/{total}] {message}")
        if self.progress_callback is not None:
            self.progress_callback(current, total, message)

    def convert(
        self,
        input_pdf: str | Path,
        output_dir: str | Path,
        generate_pdf: bool = True,
        generate_markdown: bool = True,
        translate: bool = False,
        source_lang: str | None = None,
        target_lang: str | None = None,
    ) -> ConversionResult:
        """
        Business Logic:
            执行完整的PDF转换流程，将扫描PDF转换为可搜索PDF和/或Markdown。
            可选地将OCR结果翻译为目标语言并生成翻译PDF和双语对照PDF。
            这是同步版本，适用于CLI命令行工具。

        Code Logic:
            按顺序执行最多7个步骤:
            1. PDFReader: PDF -> 逐页图片
            2. OCREngine: 逐页OCR识别(带进度回调)
            3. OutputParser: 解析每页OCR结果
            4. DualLayerPDFWriter: 生成双层PDF(可选)
            5. MarkdownWriter: 生成Markdown(可选)
            6. Translator: 逐页翻译(可选，translate=True时)
            7. TranslatedPDFWriter: 生成翻译PDF和双语对照PDF(可选)
            翻译失败不影响OCR结果，错误记录在translation_error中。
            捕获所有异常并封装到ConversionResult中。
        """
        input_pdf = Path(input_pdf)
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        start_time: float = time.time()
        stem: str = input_pdf.stem

        try:
            # 步骤0: 检测PDF类型
            self._report_progress(0, 1, "正在检测PDF类型...")
            detector: PDFTypeDetector = PDFTypeDetector()
            from deepseek_ocr.core.pdf_type_detector import PDFTypeInfo
            pdf_type_info: PDFTypeInfo = detector.detect(input_pdf)
            is_text_pdf: bool = pdf_type_info.pdf_type == "text"

            if is_text_pdf:
                # 文本PDF路径：直接提取文本，跳过OCR
                self._report_progress(0, 1, "文本PDF，正在提取文本...")
                extractor: TextPDFExtractor = TextPDFExtractor()
                parsed_pages: list[ParsedPage] = extractor.extract_all_pages(input_pdf)
                page_images: list[PageImage] | None = None
                total_pages: int = len(parsed_pages)
                logger.info(f"文本PDF提取完成, 共 {total_pages} 页")
            else:
                # 扫描PDF路径：现有流程不变
                # 步骤1: 读取PDF，渲染为图片
                self._report_progress(0, 1, "正在读取PDF...")
                page_images = self.pdf_reader.read_pdf(input_pdf)
                total_pages = len(page_images)
                logger.info(f"PDF读取完成, 共 {total_pages} 页")

                # 步骤2: 逐页OCR
                from deepseek_ocr.core.ocr_cache import _MAX_RAW_TEXT_LENGTH
                ocr_results: list[OCRResult] = []
                for i, page_img in enumerate(page_images):
                    self._report_progress(i + 1, total_pages, f"正在识别第 {i + 1} 页...")
                    result: OCRResult = self.ocr_engine.ocr_single_image(
                        image_data=page_img.image_bytes,
                        page_index=page_img.page_index,
                    )
                    for _ocr_retry in range(2):
                        if len(result.raw_text) <= _MAX_RAW_TEXT_LENGTH:
                            break
                        logger.warning(f"页 {i}: OCR输出异常 ({len(result.raw_text)} 字符)，第 {_ocr_retry + 1} 次重试")
                        result = self.ocr_engine.ocr_single_image(
                            image_data=page_img.image_bytes,
                            page_index=page_img.page_index,
                        )
                    ocr_results.append(result)
                    if not result.success:
                        logger.warning(f"页 {i}: OCR失败: {result.error_msg}")

                # 步骤3: 解析OCR结果
                self._report_progress(total_pages, total_pages, "正在解析OCR结果...")
                parsed_pages = []
                for ocr_result in ocr_results:
                    parsed: ParsedPage = self.parser.parse(
                        raw_text=ocr_result.raw_text,
                        page_index=ocr_result.page_index,
                    )
                    parsed_pages.append(parsed)

            # 步骤4: 生成PDF（文本PDF跳过，已有文字层）
            output_pdf_path: Path | None = None
            if generate_pdf and not is_text_pdf and page_images is not None:
                self._report_progress(total_pages, total_pages, f"正在生成PDF ({self.pdf_mode}模式)...")
                output_pdf_path = output_dir / f"{stem}_ocr.pdf"
                self.pdf_writer.create_dual_layer_pdf(
                    page_images=page_images,
                    parsed_pages=parsed_pages,
                    output_path=output_pdf_path,
                    mode=self.pdf_mode,
                )

            # 步骤5: 生成Markdown
            output_md_path: Path | None = None
            if generate_markdown:
                self._report_progress(total_pages, total_pages, "正在生成Markdown...")
                output_md_path = output_dir / f"{stem}.md"
                self.markdown_writer.write(
                    parsed_pages=parsed_pages,
                    output_path=output_md_path,
                )

            # 步骤6: 翻译（可选）
            output_translated_path: Path | None = None
            output_bilingual_path: Path | None = None
            translation_error: str | None = None

            if translate and self.translator is not None:
                try:
                    src_lang: str = source_lang or "English"
                    tgt_lang: str = target_lang or "Simplified Chinese"

                    translated_pages: list[TranslatedPage] = []
                    for i, parsed in enumerate(parsed_pages):
                        self._report_progress(i + 1, total_pages, f"正在翻译第 {i + 1} 页...")
                        tp: TranslatedPage = self.translator.translate_page(parsed, src_lang, tgt_lang)
                        translated_pages.append(tp)

                    # 重试翻译失败的页面（最多2轮）
                    failed_indices: list[int] = [
                        i for i, tp in enumerate(translated_pages) if not tp.success
                    ]
                    for retry_round in range(2):
                        if not failed_indices:
                            break
                        logger.warning(
                            f"第 {retry_round + 1} 轮重试: {len(failed_indices)} 页翻译失败"
                        )
                        still_failed: list[int] = []
                        for idx in failed_indices:
                            tp = self.translator.translate_page(
                                parsed_pages[idx], src_lang, tgt_lang
                            )
                            translated_pages[idx] = tp
                            if not tp.success:
                                still_failed.append(idx)
                        failed_indices = still_failed
                    if failed_indices:
                        logger.warning(f"重试后仍有 {len(failed_indices)} 页翻译失败")

                    # 步骤7: 生成翻译PDF
                    self._report_progress(total_pages, total_pages, "正在生成翻译PDF...")
                    lang_suffix: str = tgt_lang.split()[0][:2].lower() if tgt_lang else "tr"
                    output_translated_path = output_dir / f"{stem}_{lang_suffix}.pdf"
                    output_bilingual_path = output_dir / f"{stem}_bilingual.pdf"

                    if is_text_pdf:
                        # 文本PDF: 使用 TextPDFTranslatedWriter
                        text_translated_writer: TextPDFTranslatedWriter = TextPDFTranslatedWriter()
                        text_translated_writer.create_translated_pdf(
                            source_pdf_path=input_pdf,
                            translated_pages=translated_pages,
                            output_path=output_translated_path,
                            target_lang=tgt_lang,
                        )
                        text_translated_writer.create_bilingual_pdf(
                            source_pdf_path=input_pdf,
                            original_pages=parsed_pages,
                            translated_pages=translated_pages,
                            output_path=output_bilingual_path,
                            target_lang=tgt_lang,
                        )
                    elif self.translated_pdf_writer is not None and page_images is not None:
                        # 扫描PDF: 使用现有 TranslatedPDFWriter
                        self.translated_pdf_writer.create_translated_pdf(
                            page_images=page_images,
                            translated_pages=translated_pages,
                            output_path=output_translated_path,
                            target_lang=tgt_lang,
                        )
                        self.translated_pdf_writer.create_bilingual_pdf(
                            page_images=page_images,
                            original_pages=parsed_pages,
                            translated_pages=translated_pages,
                            output_path=output_bilingual_path,
                            target_lang=tgt_lang,
                        )
                except Exception as e:
                    translation_error = str(e)
                    logger.error(f"翻译失败: {e}", exc_info=True)

            elapsed: float = time.time() - start_time
            self._report_progress(total_pages, total_pages, f"转换完成! 耗时 {elapsed:.1f}s")

            return ConversionResult(
                source_pdf=input_pdf,
                output_pdf=output_pdf_path,
                output_markdown=output_md_path,
                output_translated_pdf=output_translated_path,
                output_bilingual_pdf=output_bilingual_path,
                page_count=total_pages,
                success=True,
                translation_error=translation_error,
                elapsed_seconds=elapsed,
                pdf_type=pdf_type_info.pdf_type,
            )

        except Exception as e:
            elapsed = time.time() - start_time
            logger.error(f"转换失败: {e}", exc_info=True)
            return ConversionResult(
                source_pdf=input_pdf,
                output_pdf=None,
                output_markdown=None,
                page_count=0,
                success=False,
                error_msg=str(e),
                elapsed_seconds=elapsed,
            )

    async def convert_async(
        self,
        input_pdf: str | Path,
        output_dir: str | Path,
        generate_pdf: bool = True,
        generate_markdown: bool = True,
        translate: bool = False,
        source_lang: str | None = None,
        target_lang: str | None = None,
    ) -> ConversionResult:
        """
        Business Logic:
            异步版本的PDF转换流程，用于Web界面等需要非阻塞处理的场景。
            OCR步骤使用异步接口，不阻塞事件循环。
            可选地将OCR结果翻译为目标语言并生成翻译PDF和双语对照PDF。

        Code Logic:
            流程与同步版本相同，但OCR步骤使用ocr_single_image_async，
            翻译步骤使用translate_page_async，
            PDF生成通过run_in_executor放到线程池避免阻塞事件循环。
            翻译失败不影响OCR结果。
        """
        input_pdf = Path(input_pdf)
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        start_time: float = time.time()
        stem: str = input_pdf.stem

        try:
            # 步骤0: 检测PDF类型
            self._report_progress(0, 1, "正在检测PDF类型...")
            detector: PDFTypeDetector = PDFTypeDetector()
            from deepseek_ocr.core.pdf_type_detector import PDFTypeInfo
            pdf_type_info: PDFTypeInfo = detector.detect(input_pdf)
            is_text_pdf: bool = pdf_type_info.pdf_type == "text"

            if is_text_pdf:
                # 文本PDF路径：直接提取文本，跳过OCR
                self._report_progress(0, 1, "文本PDF，正在提取文本...")
                extractor: TextPDFExtractor = TextPDFExtractor()
                parsed_pages: list[ParsedPage] = extractor.extract_all_pages(input_pdf)
                page_images: list[PageImage] | None = None
                total_pages: int = len(parsed_pages)
                logger.info(f"文本PDF提取完成, 共 {total_pages} 页")
            else:
                # 扫描PDF路径：现有流程不变
                # 步骤1: 读取PDF，渲染为图片（CPU密集但很快，同步即可）
                self._report_progress(0, 1, "正在读取PDF...")
                page_images = self.pdf_reader.read_pdf(input_pdf)
                total_pages = len(page_images)
                logger.info(f"PDF读取完成, 共 {total_pages} 页")

                # 步骤2: 逐页异步OCR
                from deepseek_ocr.core.ocr_cache import _MAX_RAW_TEXT_LENGTH
                ocr_results: list[OCRResult] = []
                for i, page_img in enumerate(page_images):
                    self._report_progress(i + 1, total_pages, f"正在识别第 {i + 1} 页...")
                    result: OCRResult = await self.ocr_engine.ocr_single_image_async(
                        image_data=page_img.image_bytes,
                        page_index=page_img.page_index,
                    )
                    for _ocr_retry in range(2):
                        if len(result.raw_text) <= _MAX_RAW_TEXT_LENGTH:
                            break
                        logger.warning(f"页 {i}: OCR输出异常 ({len(result.raw_text)} 字符)，第 {_ocr_retry + 1} 次重试")
                        result = await self.ocr_engine.ocr_single_image_async(
                            image_data=page_img.image_bytes,
                            page_index=page_img.page_index,
                        )
                    ocr_results.append(result)
                    if not result.success:
                        logger.warning(f"页 {i}: OCR失败: {result.error_msg}")

                # 步骤3: 解析OCR结果
                self._report_progress(total_pages, total_pages, "正在解析OCR结果...")
                parsed_pages = []
                for ocr_result in ocr_results:
                    parsed: ParsedPage = self.parser.parse(
                        raw_text=ocr_result.raw_text,
                        page_index=ocr_result.page_index,
                    )
                    parsed_pages.append(parsed)

            # 步骤4: 生成PDF（文本PDF跳过，已有文字层）
            output_pdf_path: Path | None = None
            if generate_pdf and not is_text_pdf and page_images is not None:
                self._report_progress(total_pages, total_pages, f"正在生成PDF ({self.pdf_mode}模式)...")
                output_pdf_path = output_dir / f"{stem}_ocr.pdf"
                self.pdf_writer.create_dual_layer_pdf(
                    page_images=page_images,
                    parsed_pages=parsed_pages,
                    output_path=output_pdf_path,
                    mode=self.pdf_mode,
                )

            # 步骤5: 生成Markdown
            output_md_path: Path | None = None
            if generate_markdown:
                self._report_progress(total_pages, total_pages, "正在生成Markdown...")
                output_md_path = output_dir / f"{stem}.md"
                self.markdown_writer.write(
                    parsed_pages=parsed_pages,
                    output_path=output_md_path,
                )

            # 步骤6: 翻译（可选）
            output_translated_path: Path | None = None
            output_bilingual_path: Path | None = None
            translation_error: str | None = None

            if translate and self.translator is not None:
                try:
                    src_lang: str = source_lang or "English"
                    tgt_lang: str = target_lang or "Simplified Chinese"

                    translated_pages: list[TranslatedPage] = []
                    for i, parsed in enumerate(parsed_pages):
                        self._report_progress(i + 1, total_pages, f"正在翻译第 {i + 1} 页...")
                        tp: TranslatedPage = await self.translator.translate_page_async(parsed, src_lang, tgt_lang)
                        translated_pages.append(tp)

                    # 重试翻译失败的页面（最多2轮）
                    failed_indices: list[int] = [
                        i for i, tp in enumerate(translated_pages) if not tp.success
                    ]
                    for retry_round in range(2):
                        if not failed_indices:
                            break
                        logger.warning(
                            f"第 {retry_round + 1} 轮重试: {len(failed_indices)} 页翻译失败"
                        )
                        still_failed: list[int] = []
                        for idx in failed_indices:
                            tp = await self.translator.translate_page_async(
                                parsed_pages[idx], src_lang, tgt_lang
                            )
                            translated_pages[idx] = tp
                            if not tp.success:
                                still_failed.append(idx)
                        failed_indices = still_failed
                    if failed_indices:
                        logger.warning(f"重试后仍有 {len(failed_indices)} 页翻译失败")

                    # 步骤7: 生成翻译PDF（CPU密集，放到线程池）
                    self._report_progress(total_pages, total_pages, "正在生成翻译PDF...")
                    lang_suffix: str = tgt_lang.split()[0][:2].lower() if tgt_lang else "tr"
                    output_translated_path = output_dir / f"{stem}_{lang_suffix}.pdf"
                    output_bilingual_path = output_dir / f"{stem}_bilingual.pdf"

                    loop: asyncio.AbstractEventLoop = asyncio.get_running_loop()

                    if is_text_pdf:
                        # 文本PDF: 使用 TextPDFTranslatedWriter
                        text_translated_writer: TextPDFTranslatedWriter = TextPDFTranslatedWriter()
                        await loop.run_in_executor(
                            None,
                            text_translated_writer.create_translated_pdf,
                            input_pdf,
                            translated_pages,
                            output_translated_path,
                            tgt_lang,
                        )
                        await loop.run_in_executor(
                            None,
                            text_translated_writer.create_bilingual_pdf,
                            input_pdf,
                            parsed_pages,
                            translated_pages,
                            output_bilingual_path,
                            tgt_lang,
                        )
                    elif self.translated_pdf_writer is not None and page_images is not None:
                        # 扫描PDF: 使用现有 TranslatedPDFWriter
                        await loop.run_in_executor(
                            None,
                            self.translated_pdf_writer.create_translated_pdf,
                            page_images,
                            translated_pages,
                            output_translated_path,
                            tgt_lang,
                        )
                        await loop.run_in_executor(
                            None,
                            self.translated_pdf_writer.create_bilingual_pdf,
                            page_images,
                            parsed_pages,
                            translated_pages,
                            output_bilingual_path,
                            tgt_lang,
                        )
                except Exception as e:
                    translation_error = str(e)
                    logger.error(f"翻译失败: {e}", exc_info=True)

            elapsed: float = time.time() - start_time
            self._report_progress(total_pages, total_pages, f"转换完成! 耗时 {elapsed:.1f}s")

            return ConversionResult(
                source_pdf=input_pdf,
                output_pdf=output_pdf_path,
                output_markdown=output_md_path,
                output_translated_pdf=output_translated_path,
                output_bilingual_pdf=output_bilingual_path,
                page_count=total_pages,
                success=True,
                translation_error=translation_error,
                elapsed_seconds=elapsed,
                pdf_type=pdf_type_info.pdf_type,
            )

        except Exception as e:
            elapsed = time.time() - start_time
            logger.error(f"异步转换失败: {e}", exc_info=True)
            return ConversionResult(
                source_pdf=input_pdf,
                output_pdf=None,
                output_markdown=None,
                page_count=0,
                success=False,
                error_msg=str(e),
                elapsed_seconds=elapsed,
            )
