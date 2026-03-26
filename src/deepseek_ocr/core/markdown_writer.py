# -*- coding: utf-8 -*-
"""
Business Logic:
    将OCR解析结果输出为Markdown文件。
    用户需要一个结构化的文本格式便于后续编辑和使用。

Code Logic:
    收集每页的markdown_text，用分隔符连接，清理多余空白后写入文件。
"""

import re
from pathlib import Path

from deepseek_ocr.core.output_parser import ParsedPage
from deepseek_ocr.utils.logger import logger


class MarkdownWriter:
    """Markdown文件写入器，将OCR解析结果输出为Markdown格式"""

    def write(
        self,
        parsed_pages: list[ParsedPage],
        output_path: str | Path,
        page_separator: str = "\n\n---\n\n",
    ) -> Path:
        """
        Business Logic:
            将所有页面的Markdown文本合并写入一个文件。
            多页之间用分隔线隔开，便于阅读和定位。

        Code Logic:
            遍历parsed_pages提取每页的markdown_text，
            用page_separator连接后清理多余空白，写入到输出路径。
        """
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        logger.info(f"开始生成Markdown: {output_path}, 共 {len(parsed_pages)} 页")

        page_texts: list[str] = []
        for parsed in parsed_pages:
            cleaned: str = self._clean_whitespace(parsed.markdown_text)
            if cleaned:
                page_texts.append(cleaned)

        content: str = page_separator.join(page_texts)
        content = self._clean_whitespace(content)

        output_path.write_text(content, encoding="utf-8")
        logger.info(f"Markdown生成完成: {output_path}")

        return output_path

    def _clean_whitespace(self, text: str) -> str:
        """
        Business Logic:
            清理文本中多余的空行，提升Markdown文件的可读性。

        Code Logic:
            将连续3个及以上的换行符替换为2个换行符，
            去除首尾空白。
        """
        # 清理连续多个空行为最多两个换行
        cleaned: str = re.sub(r'\n{3,}', '\n\n', text)
        # 去除行尾多余空格
        cleaned = re.sub(r'[ \t]+$', '', cleaned, flags=re.MULTILINE)
        return cleaned.strip()
