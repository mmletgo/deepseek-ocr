# -*- coding: utf-8 -*-
"""
Business Logic:
    从文本PDF中提取文本和坐标信息，生成与OCR解析结果相同的 ParsedPage 列表。
    适用于已包含嵌入文本的PDF（非扫描版），无需调用OCR模型即可提取结构化数据。

Code Logic:
    使用 PyMuPDF 的 page.get_text("dict") 提取文本块和图片块，
    根据字号中位数判断标题/正文，将PDF坐标归一化为 0-999 范围，
    输出与 OutputParser 相同的 TextBlock/ParsedPage 数据结构。
"""

from pathlib import Path
from statistics import median

import pymupdf

from deepseek_ocr.core.output_parser import TextBlock, ParsedPage
from deepseek_ocr.utils.logger import logger


class TextPDFExtractor:
    """从文本PDF中提取文本块和坐标，生成 ParsedPage 列表"""

    def extract_all_pages(self, pdf_path: str | Path) -> list[ParsedPage]:
        """
        Business Logic:
            打开文本PDF，逐页提取文本块，返回所有页面的解析结果。

        Code Logic:
            使用 pymupdf.open 打开PDF，遍历每页调用 _extract_page，
            最终关闭文档并返回 ParsedPage 列表。
        """
        pdf_path = Path(pdf_path)
        logger.info(f"开始提取文本PDF: {pdf_path.name}")

        doc: pymupdf.Document = pymupdf.open(str(pdf_path))
        pages: list[ParsedPage] = []

        for page_index in range(doc.page_count):
            page: pymupdf.Page = doc[page_index]
            parsed_page: ParsedPage = self._extract_page(page, page_index)
            pages.append(parsed_page)
            logger.debug(
                f"页 {page_index}: 提取 {len(parsed_page.blocks)} 个文本块"
            )

        doc.close()
        logger.info(
            f"文本PDF提取完成: {pdf_path.name}, 共 {len(pages)} 页"
        )
        return pages

    def _extract_page(self, page: pymupdf.Page, page_index: int) -> ParsedPage:
        """
        Business Logic:
            从单个PDF页面提取所有文本块和图片块，判断标签类型。

        Code Logic:
            1. 调用 page.get_text("dict") 获取页面结构化数据
            2. 遍历 blocks: type==0 为文本块，type==1 为图片块
            3. 文本块: 聚合 lines→spans→text，收集字号用于 label 判断
            4. 图片块: 创建 label="image" 的空文本块
            5. 根据所有文本块的字号中位数判断 title/text
        """
        page_dict: dict = page.get_text("dict")  # type: ignore[assignment]
        page_width: float = page_dict["width"]
        page_height: float = page_dict["height"]
        raw_blocks: list[dict] = page_dict["blocks"]  # type: ignore[assignment]

        # 第一轮: 收集所有文本块的数据和字号信息
        text_block_data: list[tuple[str, list[int], float]] = []
        # (aggregated_text, normalized_bbox, avg_font_size)
        image_blocks: list[TextBlock] = []
        all_span_sizes: list[float] = []

        for block in raw_blocks:
            block_type: int = block["type"]
            bbox_raw: tuple[float, float, float, float] = (
                block["bbox"][0],
                block["bbox"][1],
                block["bbox"][2],
                block["bbox"][3],
            )

            if block_type == 0:
                # 文本块: 聚合 lines → spans → text
                lines: list[dict] = block.get("lines", [])
                block_texts: list[str] = []
                block_font_sizes: list[float] = []

                for line in lines:
                    spans: list[dict] = line.get("spans", [])
                    for span in spans:
                        span_text: str = span.get("text", "")
                        span_size: float = span.get("size", 0.0)
                        if span_text.strip():
                            block_texts.append(span_text)
                            block_font_sizes.append(span_size)
                            all_span_sizes.append(span_size)

                aggregated_text: str = "".join(block_texts).strip()
                if not aggregated_text:
                    continue

                avg_size: float = (
                    sum(block_font_sizes) / len(block_font_sizes)
                    if block_font_sizes
                    else 0.0
                )
                norm_bbox: list[int] = self._normalize_bbox(
                    bbox_raw, page_width, page_height
                )
                text_block_data.append((aggregated_text, norm_bbox, avg_size))

            elif block_type == 1:
                # 图片块
                norm_bbox = self._normalize_bbox(
                    bbox_raw, page_width, page_height
                )
                image_blocks.append(
                    TextBlock(text="", label="image", bbox=norm_bbox)
                )

        # 第二轮: 根据字号中位数判断 label
        median_size: float = median(all_span_sizes) if all_span_sizes else 0.0
        title_threshold: float = median_size * 1.5

        blocks: list[TextBlock] = []
        for aggregated_text, norm_bbox, avg_size in text_block_data:
            label: str = (
                "title"
                if median_size > 0 and avg_size >= title_threshold
                else "text"
            )
            blocks.append(
                TextBlock(text=aggregated_text, label=label, bbox=norm_bbox)
            )

        # 合并文本块和图片块（图片块附加在后面）
        blocks.extend(image_blocks)

        # 生成 markdown_text 和 plain_text
        markdown_text: str = self._generate_markdown(blocks)
        plain_text: str = self._generate_plain_text(blocks)

        return ParsedPage(
            blocks=blocks,
            plain_text=plain_text,
            markdown_text=markdown_text,
            page_index=page_index,
        )

    @staticmethod
    def _normalize_bbox(
        bbox: tuple[float, float, float, float],
        page_width: float,
        page_height: float,
    ) -> list[int]:
        """
        Business Logic:
            将PDF坐标转换为归一化的 0-999 坐标范围。

        Code Logic:
            norm = clamp(int(pdf_coord / page_dim * 999), 0, 999)
            分别对 x1, y1, x2, y2 进行转换。
        """

        def _clamp(value: float, min_val: int, max_val: int) -> int:
            return max(min_val, min(max_val, int(value)))

        x1: int = _clamp(bbox[0] / page_width * 999, 0, 999) if page_width > 0 else 0
        y1: int = _clamp(bbox[1] / page_height * 999, 0, 999) if page_height > 0 else 0
        x2: int = _clamp(bbox[2] / page_width * 999, 0, 999) if page_width > 0 else 999
        y2: int = _clamp(bbox[3] / page_height * 999, 0, 999) if page_height > 0 else 999

        return [x1, y1, x2, y2]

    @staticmethod
    def _generate_markdown(blocks: list[TextBlock]) -> str:
        """
        Business Logic:
            从文本块列表生成 Markdown 格式文本。

        Code Logic:
            - title 块: 加 "## " 前缀
            - text 块: 原样输出
            - image 块: 跳过
            - 各块用 "\\n\\n" 连接
        """
        parts: list[str] = []
        for block in blocks:
            if block.label == "title":
                parts.append(f"## {block.text}")
            elif block.label == "text":
                parts.append(block.text)
            # image 块跳过
        return "\n\n".join(parts)

    @staticmethod
    def _generate_plain_text(blocks: list[TextBlock]) -> str:
        """
        Business Logic:
            从文本块列表生成纯文本。

        Code Logic:
            - title/text 块的 text 用 "\\n" 连接
            - image 块跳过
        """
        parts: list[str] = []
        for block in blocks:
            if block.label in ("title", "text"):
                parts.append(block.text)
        return "\n".join(parts)
