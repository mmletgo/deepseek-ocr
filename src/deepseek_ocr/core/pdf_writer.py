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
"""

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

        Code Logic:
            创建空PDF文档，遍历每页图像和解析结果，
            调用_add_page逐页添加双层内容，最后保存到指定路径。
            使用deflate压缩和garbage=4优化文件大小。
        """
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        logger.info(f"开始生成双层PDF: {output_path}, 共 {len(page_images)} 页")
        doc: pymupdf.Document = pymupdf.open()

        try:
            for page_img, parsed in zip(page_images, parsed_pages):
                self._add_page(doc, page_img, parsed)

            doc.save(str(output_path), deflate=True, garbage=4)
            logger.info(f"双层PDF生成完成: {output_path}")
        finally:
            doc.close()

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
