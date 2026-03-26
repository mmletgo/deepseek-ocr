# -*- coding: utf-8 -*-
"""核心模块：PDF读取、OCR识别、文本解析、PDF生成、Markdown生成、流水线编排"""

from deepseek_ocr.core.pdf_reader import PDFReader, PageImage
from deepseek_ocr.core.ocr_engine import OCREngine, OCRResult, PromptMode
from deepseek_ocr.core.output_parser import OutputParser, TextBlock, ParsedPage
from deepseek_ocr.core.pdf_writer import DualLayerPDFWriter
from deepseek_ocr.core.markdown_writer import MarkdownWriter
from deepseek_ocr.core.pipeline import ConversionPipeline, ConversionResult

__all__ = [
    "PDFReader",
    "PageImage",
    "OCREngine",
    "OCRResult",
    "PromptMode",
    "OutputParser",
    "TextBlock",
    "ParsedPage",
    "DualLayerPDFWriter",
    "MarkdownWriter",
    "ConversionPipeline",
    "ConversionResult",
]
