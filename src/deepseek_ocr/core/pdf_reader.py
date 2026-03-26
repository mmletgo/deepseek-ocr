# -*- coding: utf-8 -*-
"""
Business Logic:
    将扫描PDF的每一页渲染为PNG图片，供后续OCR引擎识别。
    用户上传的扫描PDF需要先转换为高质量图片才能进行文字识别。

Code Logic:
    使用PyMuPDF(pymupdf)库打开PDF，按指定DPI渲染每页为PNG图片，
    如果渲染后的图片尺寸超过max_dimension，则等比缩放。
    返回包含图片字节、尺寸信息和原始PDF页面尺寸的PageImage列表。
"""

import pymupdf
from dataclasses import dataclass
from pathlib import Path

from deepseek_ocr.utils.logger import logger


@dataclass
class PageImage:
    """单页PDF渲染后的图片数据，包含图片字节和尺寸元信息"""
    image_bytes: bytes      # PNG图片字节
    width: int              # 像素宽度
    height: int             # 像素高度
    page_index: int         # 页码(从0开始)
    original_width: float   # 原始PDF页面宽度(pt)
    original_height: float  # 原始PDF页面高度(pt)


class PDFReader:
    """PDF文件读取器，将扫描PDF的每一页渲染为PNG图片"""

    def __init__(self, dpi: int = 200, max_dimension: int = 1920) -> None:
        """
        Business Logic:
            初始化PDF读取器，配置渲染精度和最大尺寸限制。
            DPI越高图片越清晰但OCR耗时越长，需要平衡。

        Code Logic:
            保存dpi和max_dimension参数，计算基础缩放矩阵比例(dpi/72)。
        """
        self.dpi: int = dpi
        self.max_dimension: int = max_dimension
        self._scale: float = dpi / 72.0
        logger.info(f"PDFReader初始化: dpi={dpi}, max_dimension={max_dimension}")

    def read_pdf(self, pdf_path: str | Path) -> list[PageImage]:
        """
        Business Logic:
            读取整个PDF文件，将每一页渲染为PNG图片返回。
            这是OCR流水线的第一步。

        Code Logic:
            用pymupdf打开PDF，遍历每一页调用render_page渲染，
            收集所有PageImage到列表中返回。
        """
        pdf_path = Path(pdf_path)
        if not pdf_path.exists():
            raise FileNotFoundError(f"PDF文件不存在: {pdf_path}")

        logger.info(f"开始读取PDF: {pdf_path}")
        doc: pymupdf.Document = pymupdf.open(str(pdf_path))
        page_images: list[PageImage] = []

        try:
            for page_index in range(len(doc)):
                page: pymupdf.Page = doc[page_index]
                page_image: PageImage = self.render_page(page, page_index)
                page_images.append(page_image)
                logger.debug(f"已渲染第 {page_index + 1}/{len(doc)} 页, "
                             f"尺寸: {page_image.width}x{page_image.height}")
        finally:
            doc.close()

        logger.info(f"PDF读取完成, 共 {len(page_images)} 页")
        return page_images

    def get_page_count(self, pdf_path: str | Path) -> int:
        """
        Business Logic:
            获取PDF的总页数，用于进度显示和预估处理时间。

        Code Logic:
            用pymupdf打开PDF，返回page_count属性值后关闭文档。
        """
        pdf_path = Path(pdf_path)
        if not pdf_path.exists():
            raise FileNotFoundError(f"PDF文件不存在: {pdf_path}")

        doc: pymupdf.Document = pymupdf.open(str(pdf_path))
        count: int = len(doc)
        doc.close()
        return count

    def render_page(self, page: pymupdf.Page, page_index: int) -> PageImage:
        """
        Business Logic:
            将PDF的单页渲染为PNG图片，确保图片尺寸不超过限制。
            过大的图片会导致OCR模型处理缓慢或内存不足。

        Code Logic:
            1. 使用Matrix(dpi/72, dpi/72)进行高分辨率渲染
            2. 检查渲染后的pixmap尺寸是否超过max_dimension
            3. 如果超出，计算等比缩放比例，重新渲染
            4. 将pixmap转换为PNG字节，封装为PageImage返回
        """
        original_width: float = page.rect.width
        original_height: float = page.rect.height

        # 使用DPI对应的缩放矩阵渲染
        matrix: pymupdf.Matrix = pymupdf.Matrix(self._scale, self._scale)
        pixmap: pymupdf.Pixmap = page.get_pixmap(matrix=matrix, alpha=False)

        # 检查是否需要缩放
        current_max: int = max(pixmap.width, pixmap.height)
        if current_max > self.max_dimension:
            scale_factor: float = self.max_dimension / current_max
            adjusted_scale: float = self._scale * scale_factor
            matrix = pymupdf.Matrix(adjusted_scale, adjusted_scale)
            pixmap = page.get_pixmap(matrix=matrix, alpha=False)
            logger.debug(f"页 {page_index}: 图片超出限制，缩放至 {pixmap.width}x{pixmap.height}")

        image_bytes: bytes = pixmap.tobytes("png")

        return PageImage(
            image_bytes=image_bytes,
            width=pixmap.width,
            height=pixmap.height,
            page_index=page_index,
            original_width=original_width,
            original_height=original_height,
        )
