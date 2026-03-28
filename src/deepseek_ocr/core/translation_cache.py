# -*- coding: utf-8 -*-
"""
Business Logic:
    持久化存储每页的翻译结果，避免重复对同一 PDF 进行翻译。
    以 PDF 的 MD5 哈希 + 源语言 + 目标语言为 key，每页结果单独存为 JSON 文件。

Code Logic:
    缓存目录结构：{cache_dir}/{pdf_md5}_{src_lang}_{tgt_lang}/page_{n:04d}.json
    JSON 内容：{"page_index": N, "translated_blocks": [{"text": "...", "label": "...", "bbox": [x1,y1,x2,y2]}, ...]}
"""

import json
from pathlib import Path


class TranslationCache:
    """按 PDF MD5 + 语言对缓存每页翻译结果，支持断点续传"""

    def __init__(self, cache_dir: str | Path) -> None:
        """
        Business Logic:
            初始化翻译缓存，指定缓存根目录。

        Code Logic:
            将 cache_dir 转换为 Path 对象保存，不创建目录（延迟到实际写入时创建）。
        """
        self.cache_dir = Path(cache_dir)

    def _cache_key(self, pdf_md5: str, source_lang: str, target_lang: str) -> str:
        """
        Business Logic:
            生成缓存目录名，由 PDF 哈希和语言对组合而成。

        Code Logic:
            将语言名转为小写、空格替换为下划线、截取前10字符，
            拼接为 {pdf_md5}_{src_lang}_{tgt_lang} 格式。
        """
        src: str = source_lang.lower().replace(" ", "_")[:10]
        tgt: str = target_lang.lower().replace(" ", "_")[:10]
        return f"{pdf_md5}_{src}_{tgt}"

    def _page_path(self, pdf_md5: str, source_lang: str, target_lang: str, page_index: int) -> Path:
        """返回指定页的缓存文件路径"""
        key: str = self._cache_key(pdf_md5, source_lang, target_lang)
        return self.cache_dir / key / f"page_{page_index:04d}.json"

    def is_page_cached(self, pdf_md5: str, source_lang: str, target_lang: str, page_index: int) -> bool:
        """
        Business Logic:
            检查指定页是否已有翻译缓存，用于决定是否跳过翻译。

        Code Logic:
            检查对应 JSON 文件是否存在。
        """
        return self._page_path(pdf_md5, source_lang, target_lang, page_index).exists()

    def load_page(self, pdf_md5: str, source_lang: str, target_lang: str, page_index: int) -> list[dict] | None:
        """
        Business Logic:
            从缓存中加载指定页的翻译结果。
            缓存不存在时返回 None，调用方需处理。

        Code Logic:
            读取 JSON 文件，返回 translated_blocks 列表。
        """
        path: Path = self._page_path(pdf_md5, source_lang, target_lang, page_index)
        if not path.exists():
            return None
        data: dict = json.loads(path.read_text(encoding="utf-8"))
        return data["translated_blocks"]

    def save_page(
        self,
        pdf_md5: str,
        source_lang: str,
        target_lang: str,
        page_index: int,
        translated_blocks: list[dict],
    ) -> None:
        """
        Business Logic:
            将翻译结果保存到缓存，供下次断点续传使用。

        Code Logic:
            创建父目录（若不存在），序列化翻译结果写入 JSON 文件。
            使用 ensure_ascii=False 支持多语言内容。
        """
        path: Path = self._page_path(pdf_md5, source_lang, target_lang, page_index)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {"page_index": page_index, "translated_blocks": translated_blocks},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    def count_cached_pages(self, pdf_md5: str, source_lang: str, target_lang: str) -> int:
        """
        Business Logic:
            统计已缓存的翻译页数，用于向用户展示断点续传进度。

        Code Logic:
            枚举缓存目录下所有 page_*.json 文件数量。
        """
        key: str = self._cache_key(pdf_md5, source_lang, target_lang)
        d: Path = self.cache_dir / key
        if not d.exists():
            return 0
        return len(list(d.glob("page_*.json")))
