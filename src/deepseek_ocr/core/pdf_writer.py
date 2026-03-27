# -*- coding: utf-8 -*-
"""
Business Logic:
    生成双层PDF：底层是扫描图像(保持原始外观)，上层是透明文字层(支持搜索和复制)。
    用户需要一个既能保持原始扫描外观、又能搜索和复制文字的PDF文件。

Code Logic:
    使用PyMuPDF创建新PDF文档，逐页插入:
    1. 底层：原始扫描图像(overlay=False)
    2. 上层：使用TextWriter写入透明文字(render_mode=3 = 不可见)
    坐标从归一化(0-999)转换为PDF坐标(pt)。
    使用线程池并行处理每页，充分利用多核CPU加速生成。
"""

import concurrent.futures
import os
import pymupdf
from pathlib import Path

from deepseek_ocr.core.pdf_reader import PageImage
from deepseek_ocr.core.output_parser import ParsedPage, TextBlock
from deepseek_ocr.utils.logger import logger


class DualLayerPDFWriter:
    """双层PDF生成器，底层扫描图像 + 上层透明文字层"""

    def __init__(self) -> None:
        """
        Business Logic:
            初始化PDF写入器，准备用于文字层的字体。

        Code Logic:
            加载Helvetica内置字体，用于写入不可见文字层。
        """
        self.font: pymupdf.Font = pymupdf.Font("helv")
        logger.info("DualLayerPDFWriter初始化完成")

    def create_dual_layer_pdf(
        self,
        page_images: list[PageImage],
        parsed_pages: list[ParsedPage],
        output_path: str | Path,
    ) -> Path:
        """
        Business Logic:
            将扫描图像和OCR文本合并为双层PDF。
            生成的PDF外观与原始扫描PDF一致，但文字可搜索和复制。
            使用线程池并行处理每页，充分利用多核CPU加速生成。

        Code Logic:
            用ThreadPoolExecutor并行调用_render_page_to_bytes处理每页，
            各线程独立创建Font和Document对象（线程安全），
            并行处理完成后按原始顺序合并成最终PDF文档保存。
            中间页字节流不压缩，最终合并时统一压缩。
        """
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        logger.info(f"开始生成双层PDF: {output_path}, 共 {len(page_images)} 页")

        cpu_count = os.cpu_count() or 4
        max_workers = max(1, min(cpu_count, len(page_images)))
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [
                executor.submit(self._render_page_to_bytes, page_img, parsed)
                for page_img, parsed in zip(page_images, parsed_pages)
            ]
            page_bytes_list: list[bytes] = [f.result() for f in futures]

        logger.info(f"所有页面并行处理完成，开始合并...")
        final_doc: pymupdf.Document = pymupdf.open()
        try:
            for page_bytes in page_bytes_list:
                src: pymupdf.Document = pymupdf.open("pdf", page_bytes)
                final_doc.insert_pdf(src)
                src.close()
            # 各页已预压缩，直接保存无需再次 deflate/garbage
            final_doc.save(str(output_path), deflate=False, garbage=1)
            logger.info(f"双层PDF生成完成: {output_path}")
        finally:
            final_doc.close()

        return output_path

    def _add_page(
        self,
        doc: pymupdf.Document,
        page_img: PageImage,
        parsed: ParsedPage,
    ) -> None:
        """
        Business Logic:
            在PDF文档中添加一页，包含底层扫描图像和上层透明文字。
            确保文字层与图像层的位置精确对齐。

        Code Logic:
            1. 创建新页面，尺寸与原始PDF一致
            2. 插入扫描图像作为底层(overlay=False)
            3. 使用TextWriter在对应坐标位置写入透明文字
            4. 坐标转换：归一化(0-999) -> PDF坐标(pt)
            5. render_mode=3表示不可见文字(可搜索但不显示)
        """
        # 1. 创建新页面，尺寸与原始PDF一致
        page: pymupdf.Page = doc.new_page(
            width=page_img.original_width,
            height=page_img.original_height,
        )

        # 2. 插入扫描图像作为底层
        page.insert_image(
            page.rect,
            stream=page_img.image_bytes,
            overlay=False,
        )

        # 3. 用TextWriter写入透明文字层
        tw: pymupdf.TextWriter = pymupdf.TextWriter(page.rect)

        for block in parsed.blocks:
            if not block.text.strip():
                continue

            # 坐标转换：归一化(0-999) -> PDF坐标
            pdf_x1: float = block.bbox[0] / 999.0 * page.rect.width
            pdf_y1: float = block.bbox[1] / 999.0 * page.rect.height
            pdf_x2: float = block.bbox[2] / 999.0 * page.rect.width
            pdf_y2: float = block.bbox[3] / 999.0 * page.rect.height

            rect: pymupdf.Rect = pymupdf.Rect(pdf_x1, pdf_y1, pdf_x2, pdf_y2)

            # 确保rect有效（宽高大于0）
            if rect.width <= 0 or rect.height <= 0:
                logger.debug(f"页 {page_img.page_index}: 跳过无效区域 {block.bbox}")
                continue

            # 估算字号：根据区域高度和行数计算
            lines: list[str] = block.text.strip().split('\n')
            line_count: int = max(len(lines), 1)
            fontsize: float = max(min(rect.height / line_count * 0.75, 36.0), 3.0)

            try:
                # fill_textbox自动换行填充文本到指定区域
                tw.fill_textbox(
                    rect,
                    block.text,
                    font=self.font,
                    fontsize=fontsize,
                )
            except Exception as e:
                # fill_textbox可能因文字过多超出rect而抛异常
                logger.debug(
                    f"页 {page_img.page_index}: fill_textbox异常 (block label={block.label}): {e}, "
                    f"尝试缩小字号"
                )
                # 缩小字号重试
                try:
                    smaller_fontsize: float = max(fontsize * 0.5, 3.0)
                    tw.fill_textbox(
                        rect,
                        block.text,
                        font=self.font,
                        fontsize=smaller_fontsize,
                    )
                except Exception as e2:
                    logger.warning(
                        f"页 {page_img.page_index}: fill_textbox再次失败: {e2}, 跳过此block"
                    )

        # 4. render_mode=3 -> 不可见文字（可搜索但不显示）
        tw.write_text(page, render_mode=3)

    @staticmethod
    def _render_page_to_bytes(page_img: PageImage, parsed: ParsedPage) -> bytes:
        """
        Business Logic:
            线程安全的单页处理函数，用于并行生成双层PDF的每一页。
            每次调用独立创建Font和Document对象，不共享任何状态。

        Code Logic:
            在独立线程中创建单页PDF文档，嵌入图像和透明文字层，
            返回未压缩的PDF字节流供后续合并。
        """
        font: pymupdf.Font = pymupdf.Font("helv")
        single_doc: pymupdf.Document = pymupdf.open()
        try:
            page: pymupdf.Page = single_doc.new_page(
                width=page_img.original_width,
                height=page_img.original_height,
            )
            page.insert_image(page.rect, stream=page_img.image_bytes, overlay=False)
            tw: pymupdf.TextWriter = pymupdf.TextWriter(page.rect)
            for block in parsed.blocks:
                if not block.text.strip():
                    continue
                pdf_x1: float = block.bbox[0] / 999.0 * page.rect.width
                pdf_y1: float = block.bbox[1] / 999.0 * page.rect.height
                pdf_x2: float = block.bbox[2] / 999.0 * page.rect.width
                pdf_y2: float = block.bbox[3] / 999.0 * page.rect.height
                rect: pymupdf.Rect = pymupdf.Rect(pdf_x1, pdf_y1, pdf_x2, pdf_y2)
                if rect.width <= 0 or rect.height <= 0:
                    continue
                lines: list[str] = block.text.strip().split('\n')
                fontsize: float = max(min(rect.height / max(len(lines), 1) * 0.75, 36.0), 3.0)
                try:
                    tw.fill_textbox(rect, block.text, font=font, fontsize=fontsize)
                except Exception:
                    try:
                        tw.fill_textbox(rect, block.text, font=font,
                                        fontsize=max(fontsize * 0.5, 3.0))
                    except Exception:
                        pass
            tw.write_text(page, render_mode=3)
            # 每页单独压缩（并行执行，效率高），最终合并时无需重压缩
            return single_doc.tobytes(deflate=True, garbage=1)
        finally:
            single_doc.close()
