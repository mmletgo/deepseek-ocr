# -*- coding: utf-8 -*-
"""
Business Logic:
    持久化存储每页的 OCR 结果，避免重复对同一 PDF 进行 OCR。
    以 PDF 的 MD5 哈希为 key，每页结果单独存为 JSON 文件。

Code Logic:
    缓存目录结构：{cache_dir}/{pdf_md5}/page_{n:04d}.json
    JSON 内容：{"page_index": N, "raw_text": "..."}
"""

import json
import hashlib
from pathlib import Path

from deepseek_ocr.core.ocr_engine import OCRResult
from deepseek_ocr.utils.logger import logger

# 单页 OCR 文本超过此长度视为模型输出异常（重复/损坏），丢弃缓存重新 OCR
_MAX_RAW_TEXT_LENGTH: int = 20_000


class OCRCache:
    """按 PDF MD5 缓存每页 OCR 结果，支持断点续传"""

    def __init__(self, cache_dir: str | Path) -> None:
        """
        Business Logic:
            初始化 OCR 缓存，指定缓存根目录。

        Code Logic:
            将 cache_dir 转换为 Path 对象保存，不创建目录（延迟到实际写入时创建）。
        """
        self.cache_dir = Path(cache_dir)

    @staticmethod
    def compute_md5(file_path: str | Path) -> str:
        """
        Business Logic:
            计算文件的 MD5 哈希值，作为缓存的唯一标识符。
            同一 PDF 文件内容不变则哈希不变，可用于判断是否已缓存。

        Code Logic:
            以 64KB 分块读取文件并更新 MD5，返回十六进制哈希字符串。
        """
        md5 = hashlib.md5()
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                md5.update(chunk)
        return md5.hexdigest()

    def _page_path(self, pdf_md5: str, page_index: int) -> Path:
        """返回指定页的缓存文件路径"""
        return self.cache_dir / pdf_md5 / f"page_{page_index:04d}.json"

    def is_page_cached(self, pdf_md5: str, page_index: int) -> bool:
        """
        Business Logic:
            检查指定页是否已有缓存，用于决定是否跳过 OCR。

        Code Logic:
            检查对应 JSON 文件是否存在。
        """
        return self._page_path(pdf_md5, page_index).exists()

    def count_cached_pages(self, pdf_md5: str) -> int:
        """
        Business Logic:
            统计已缓存的页数，用于向用户展示断点续传进度。

        Code Logic:
            枚举缓存目录下所有 page_*.json 文件数量。
        """
        d = self.cache_dir / pdf_md5
        if not d.exists():
            return 0
        return len(list(d.glob("page_*.json")))

    def load_page(self, pdf_md5: str, page_index: int) -> OCRResult | None:
        """
        Business Logic:
            从缓存中加载指定页的 OCR 结果。
            缓存不存在时返回 None，调用方需处理。

        Code Logic:
            读取 JSON 文件，反序列化为 OCRResult 对象（success=True）。
        """
        path = self._page_path(pdf_md5, page_index)
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        raw_text: str = data.get("raw_text", "")
        # 检测异常 OCR 输出：文本超长通常是模型输出重复/损坏
        if len(raw_text) > _MAX_RAW_TEXT_LENGTH:
            logger.warning(
                f"页 {page_index}: OCR 缓存文本异常 ({len(raw_text)} 字符 > {_MAX_RAW_TEXT_LENGTH})，"
                f"丢弃缓存将重新 OCR"
            )
            path.unlink(missing_ok=True)
            return None
        return OCRResult(
            page_index=data["page_index"],
            raw_text=raw_text,
            success=True,
        )

    def save_page(self, pdf_md5: str, page_index: int, result: OCRResult) -> None:
        """
        Business Logic:
            将 OCR 结果保存到缓存，供下次断点续传使用。

        Code Logic:
            创建父目录（若不存在），序列化 OCRResult 关键字段写入 JSON 文件。
            使用 ensure_ascii=False 支持多语言内容。
        """
        # 不缓存异常 OCR 输出（文本超长，模型输出重复/损坏）
        if len(result.raw_text) > _MAX_RAW_TEXT_LENGTH:
            logger.warning(
                f"页 {page_index}: OCR 输出异常 ({len(result.raw_text)} 字符)，不写入缓存"
            )
            return
        path = self._page_path(pdf_md5, page_index)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {"page_index": page_index, "raw_text": result.raw_text},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
