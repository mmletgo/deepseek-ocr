# -*- coding: utf-8 -*-
"""
检测PDF类型：文本版(已有嵌入文本)还是扫描版(图片为主)。
采样页面并统计文本字符数来判断。
"""

from dataclasses import dataclass
from pathlib import Path

import pymupdf

from deepseek_ocr.utils.logger import logger


@dataclass
class PDFTypeInfo:
    """PDF类型检测结果"""
    pdf_type: str  # "text" | "scanned"
    total_pages: int
    avg_chars_per_page: float


class PDFTypeDetector:
    """PDF类型检测器"""

    TEXT_THRESHOLD: int = 100  # 每页平均非空白字符数阈值
    SAMPLE_PAGES: int = 5     # 最大采样页数

    def detect(self, pdf_path: str | Path) -> PDFTypeInfo:
        """
        检测PDF类型。

        策略：采样前 SAMPLE_PAGES 页(或全部页，取较少者)，
        用 page.get_text("text") 获取文本，计数非空白字符。
        如果平均每页字符数 > TEXT_THRESHOLD，则判定为文本PDF。
        """
        pdf_path = Path(pdf_path)
        doc: pymupdf.Document = pymupdf.open(str(pdf_path))
        total_pages: int = doc.page_count

        sample_count: int = min(total_pages, self.SAMPLE_PAGES)
        total_chars: int = 0

        for i in range(sample_count):
            page: pymupdf.Page = doc[i]
            text: str = str(page.get_text("text"))
            # 计数非空白字符
            non_whitespace: int = sum(1 for c in text if not c.isspace())
            total_chars += non_whitespace

        doc.close()

        avg_chars: float = total_chars / sample_count if sample_count > 0 else 0.0
        pdf_type: str = "text" if avg_chars > self.TEXT_THRESHOLD else "scanned"

        logger.info(
            f"PDF类型检测: {pdf_path.name} → {pdf_type} "
            f"(采样{sample_count}页, 平均{avg_chars:.0f}字符/页, 阈值{self.TEXT_THRESHOLD})"
        )

        return PDFTypeInfo(
            pdf_type=pdf_type,
            total_pages=total_pages,
            avg_chars_per_page=avg_chars,
        )
