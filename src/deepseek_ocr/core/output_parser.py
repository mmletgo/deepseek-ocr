# -*- coding: utf-8 -*-
"""
Business Logic:
    解析DeepSeek-OCR模型输出的带坐标标签文本，提取文本块及其位置信息。
    OCR模型返回的原始文本包含特殊标签标注每个文本区域的类型和坐标，
    需要解析后才能用于生成双层PDF和结构化Markdown。

Code Logic:
    使用正则表达式匹配 <|ref|>label<|/ref|><|det|>[[coords]]<|/det|> 格式的标签，
    将标签与紧跟其后的文本内容配对成TextBlock。
    支持坐标归一化转换(0-999范围转像素坐标)。
    解析失败时降级为纯文本模式。
"""

import re
import ast
from dataclasses import dataclass, field

from deepseek_ocr.utils.logger import logger


@dataclass
class TextBlock:
    """单个文本区域块，包含文本内容、标签类型和归一化坐标"""
    text: str                # 文本内容
    label: str               # 标签类型: title, text, image, table, formula
    bbox: list[int] = field(default_factory=list)  # 归一化坐标 [x1, y1, x2, y2], 范围 0-999


@dataclass
class ParsedPage:
    """单页的解析结果，包含文本块列表和清理后的文本"""
    blocks: list[TextBlock]  # 文本块列表
    plain_text: str          # 纯文本(无标签)
    markdown_text: str       # 清理后的Markdown
    page_index: int          # 页码(从0开始)


class OutputParser:
    """DeepSeek-OCR输出解析器，将带坐标标签的原始文本转换为结构化数据"""

    # 匹配 <|ref|>label<|/ref|><|det|>[[x1,y1,x2,y2]]<|/det|> 格式
    TAG_PATTERN: re.Pattern[str] = re.compile(
        r'<\|ref\|>(.*?)<\|/ref\|><\|det\|>(.*?)<\|/det\|>',
        re.DOTALL,
    )

    # 匹配所有ref/det标签（用于清理）
    CLEAN_PATTERN: re.Pattern[str] = re.compile(
        r'<\|ref\|>.*?<\|/ref\|><\|det\|>.*?<\|/det\|>',
        re.DOTALL,
    )

    def parse(self, raw_text: str, page_index: int) -> ParsedPage:
        """
        Business Logic:
            解析OCR模型的原始输出，提取每个文本区域的内容、类型和位置。
            如果解析失败（没有找到任何标签），降级为纯文本模式。

        Code Logic:
            1. 使用正则表达式查找所有标签及其位置
            2. 根据标签在原文中的位置，截取标签后到下一个标签前的文本作为该块的内容
            3. 解析坐标字符串为整数列表
            4. 如果无标签匹配，创建一个覆盖全页的TextBlock作为降级处理
        """
        blocks: list[TextBlock] = []
        matches: list[re.Match[str]] = list(self.TAG_PATTERN.finditer(raw_text))

        if not matches:
            # 降级：无标签时，整个文本作为一个block
            logger.warning(f"页 {page_index}: 未找到坐标标签，降级为纯文本模式")
            clean_text: str = raw_text.strip()
            if clean_text:
                blocks.append(TextBlock(
                    text=clean_text,
                    label="text",
                    bbox=[0, 0, 999, 999],
                ))
            return ParsedPage(
                blocks=blocks,
                plain_text=self.extract_plain_text(raw_text),
                markdown_text=self.extract_clean_markdown(raw_text),
                page_index=page_index,
            )

        # 解析每个标签及其对应的文本内容
        for i, match in enumerate(matches):
            label: str = match.group(1).strip()
            coords_str: str = match.group(2).strip()

            # 解析坐标
            bbox: list[int] = self._parse_coords(coords_str)

            # 提取该标签后面的文本内容
            # 文本范围：当前标签结束位置 到 下一个标签开始位置（或文本末尾）
            text_start: int = match.end()
            if i + 1 < len(matches):
                text_end: int = matches[i + 1].start()
            else:
                text_end = len(raw_text)

            text_content: str = raw_text[text_start:text_end].strip()

            blocks.append(TextBlock(
                text=text_content,
                label=label,
                bbox=bbox,
            ))

        return ParsedPage(
            blocks=blocks,
            plain_text=self.extract_plain_text(raw_text),
            markdown_text=self.extract_clean_markdown(raw_text),
            page_index=page_index,
        )

    def _parse_coords(self, coords_str: str) -> list[int]:
        """
        Business Logic:
            将坐标字符串解析为整数列表，容错处理各种格式异常。

        Code Logic:
            尝试用ast.literal_eval解析坐标字符串，
            支持 [[x1,y1,x2,y2]] 和 [x1,y1,x2,y2] 两种格式。
            解析失败时返回全页坐标[0,0,999,999]。
        """
        try:
            parsed = ast.literal_eval(coords_str)
            # 处理嵌套列表 [[x1,y1,x2,y2]]
            if isinstance(parsed, list) and len(parsed) > 0:
                if isinstance(parsed[0], list):
                    coords: list[int] = [int(c) for c in parsed[0]]
                else:
                    coords = [int(c) for c in parsed]
                if len(coords) == 4:
                    return coords
            logger.warning(f"坐标格式异常: {coords_str}, 使用全页坐标")
            return [0, 0, 999, 999]
        except (ValueError, SyntaxError) as e:
            logger.warning(f"坐标解析失败: {coords_str}, 错误: {e}, 使用全页坐标")
            return [0, 0, 999, 999]

    def normalize_to_pixel(
        self,
        bbox: list[int],
        image_width: int,
        image_height: int,
    ) -> tuple[float, float, float, float]:
        """
        Business Logic:
            将归一化坐标(0-999)转换为实际像素坐标，
            用于在图片上定位文本区域。

        Code Logic:
            将0-999范围的坐标按比例映射到实际图片尺寸。
        """
        x1: float = bbox[0] / 999.0 * image_width
        y1: float = bbox[1] / 999.0 * image_height
        x2: float = bbox[2] / 999.0 * image_width
        y2: float = bbox[3] / 999.0 * image_height
        return (x1, y1, x2, y2)

    def extract_clean_markdown(self, raw_text: str) -> str:
        """
        Business Logic:
            从OCR输出中去除所有坐标标签，保留纯净的Markdown文本。
            用于生成最终的Markdown输出文件。

        Code Logic:
            使用正则表达式移除所有 <|ref|>...<|/ref|><|det|>...<|/det|> 标签，
            清理多余的空行。
        """
        cleaned: str = self.CLEAN_PATTERN.sub("", raw_text)
        # 清理多余空行
        cleaned = re.sub(r'\n{3,}', '\n\n', cleaned)
        return cleaned.strip()

    def extract_plain_text(self, raw_text: str) -> str:
        """
        Business Logic:
            从OCR输出中提取纯文本，去除所有标签和Markdown格式标记。
            用于全文搜索和纯文本导出场景。

        Code Logic:
            先移除坐标标签，再移除常见的Markdown格式标记(#、*、-等)。
        """
        # 先移除坐标标签
        text: str = self.CLEAN_PATTERN.sub("", raw_text)
        # 移除Markdown格式标记
        text = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)
        text = re.sub(r'\*{1,2}(.*?)\*{1,2}', r'\1', text)
        text = re.sub(r'`{1,3}(.*?)`{1,3}', r'\1', text, flags=re.DOTALL)
        # 清理多余空行
        text = re.sub(r'\n{3,}', '\n\n', text)
        return text.strip()
