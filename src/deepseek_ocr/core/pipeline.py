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

        Code Logic:
            根据AppConfig初始化PDFReader(使用pdf配置)、OCREngine(使用ollama配置)、
            OutputParser、DualLayerPDFWriter和MarkdownWriter。
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
    ) -> ConversionResult:
        """
        Business Logic:
            执行完整的PDF转换流程，将扫描PDF转换为可搜索PDF和/或Markdown。
            这是同步版本，适用于CLI命令行工具。

        Code Logic:
            按顺序执行5个步骤:
            1. PDFReader: PDF -> 逐页图片
            2. OCREngine: 逐页OCR识别(带进度回调)
            3. OutputParser: 解析每页OCR结果
            4. DualLayerPDFWriter: 生成双层PDF(可选)
            5. MarkdownWriter: 生成Markdown(可选)
            捕获所有异常并封装到ConversionResult中。
        """
        input_pdf = Path(input_pdf)
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        start_time: float = time.time()
        stem: str = input_pdf.stem

        try:
            # 步骤1: 读取PDF，渲染为图片
            self._report_progress(0, 1, "正在读取PDF...")
            page_images: list[PageImage] = self.pdf_reader.read_pdf(input_pdf)
            total_pages: int = len(page_images)
            logger.info(f"PDF读取完成, 共 {total_pages} 页")

            # 步骤2: 逐页OCR
            ocr_results: list[OCRResult] = []
            for i, page_img in enumerate(page_images):
                self._report_progress(i + 1, total_pages, f"正在识别第 {i + 1} 页...")
                result: OCRResult = self.ocr_engine.ocr_single_image(
                    image_data=page_img.image_bytes,
                    page_index=page_img.page_index,
                )
                ocr_results.append(result)
                if not result.success:
                    logger.warning(f"页 {i}: OCR失败: {result.error_msg}")

            # 步骤3: 解析OCR结果
            self._report_progress(total_pages, total_pages, "正在解析OCR结果...")
            parsed_pages: list[ParsedPage] = []
            for ocr_result in ocr_results:
                parsed: ParsedPage = self.parser.parse(
                    raw_text=ocr_result.raw_text,
                    page_index=ocr_result.page_index,
                )
                parsed_pages.append(parsed)

            # 步骤4: 生成PDF
            output_pdf_path: Path | None = None
            if generate_pdf:
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

            elapsed: float = time.time() - start_time
            self._report_progress(total_pages, total_pages, f"转换完成! 耗时 {elapsed:.1f}s")

            return ConversionResult(
                source_pdf=input_pdf,
                output_pdf=output_pdf_path,
                output_markdown=output_md_path,
                page_count=total_pages,
                success=True,
                elapsed_seconds=elapsed,
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
    ) -> ConversionResult:
        """
        Business Logic:
            异步版本的PDF转换流程，用于Web界面等需要非阻塞处理的场景。
            OCR步骤使用异步接口，不阻塞事件循环。

        Code Logic:
            流程与同步版本相同，但OCR步骤使用ocr_single_image_async。
            PDF读取、解析、写入等CPU密集操作仍为同步（它们执行很快）。
        """
        input_pdf = Path(input_pdf)
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        start_time: float = time.time()
        stem: str = input_pdf.stem

        try:
            # 步骤1: 读取PDF，渲染为图片（CPU密集但很快，同步即可）
            self._report_progress(0, 1, "正在读取PDF...")
            page_images: list[PageImage] = self.pdf_reader.read_pdf(input_pdf)
            total_pages: int = len(page_images)
            logger.info(f"PDF读取完成, 共 {total_pages} 页")

            # 步骤2: 逐页异步OCR
            ocr_results: list[OCRResult] = []
            for i, page_img in enumerate(page_images):
                self._report_progress(i + 1, total_pages, f"正在识别第 {i + 1} 页...")
                result: OCRResult = await self.ocr_engine.ocr_single_image_async(
                    image_data=page_img.image_bytes,
                    page_index=page_img.page_index,
                )
                ocr_results.append(result)
                if not result.success:
                    logger.warning(f"页 {i}: OCR失败: {result.error_msg}")

            # 步骤3: 解析OCR结果
            self._report_progress(total_pages, total_pages, "正在解析OCR结果...")
            parsed_pages: list[ParsedPage] = []
            for ocr_result in ocr_results:
                parsed: ParsedPage = self.parser.parse(
                    raw_text=ocr_result.raw_text,
                    page_index=ocr_result.page_index,
                )
                parsed_pages.append(parsed)

            # 步骤4: 生成PDF
            output_pdf_path: Path | None = None
            if generate_pdf:
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

            elapsed: float = time.time() - start_time
            self._report_progress(total_pages, total_pages, f"转换完成! 耗时 {elapsed:.1f}s")

            return ConversionResult(
                source_pdf=input_pdf,
                output_pdf=output_pdf_path,
                output_markdown=output_md_path,
                page_count=total_pages,
                success=True,
                elapsed_seconds=elapsed,
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
