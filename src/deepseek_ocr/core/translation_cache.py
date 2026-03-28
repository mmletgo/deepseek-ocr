# -*- coding: utf-8 -*-
"""
Business Logic:
    持久化存储每页的翻译结果，避免重复翻译同一 PDF。
    以 PDF 的 MD5 哈希 + 源/目标语言组合为 key，每页翻译结果单独存为 JSON 文件。

Code Logic:
    缓存目录结构：{cache_dir}/{pdf_md5}/{src_lang}_{tgt_lang}/page_{n:04d}.json
    JSON 内容：每个 translated block 的 text, label, bbox
"""

import json
from pathlib import Path
from typing import Any

from deepseek_ocr.core.output_parser import ParsedPage, TextBlock
from deepseek_ocr.core.translator import TranslatedPage
from deepseek_ocr.utils.logger import logger


_SKIP_LABELS: frozenset[str] = frozenset({"formula", "equation", "image", "table"})


class TranslationCache:
    """按 PDF MD5 + 语言对缓存每页翻译结果，支持断点续传"""

    def __init__(self, cache_dir: str | Path) -> None:
        self.cache_dir = Path(cache_dir)

    def _lang_key(self, source_lang: str, target_lang: str) -> str:
        """生成语言对缓存目录名"""
        src: str = source_lang.lower().replace(" ", "_")
        tgt: str = target_lang.lower().replace(" ", "_")
        return f"{src}_{tgt}"

    def _page_path(self, pdf_md5: str, source_lang: str, target_lang: str, page_index: int) -> Path:
        lang_key: str = self._lang_key(source_lang, target_lang)
        return self.cache_dir / pdf_md5 / lang_key / f"page_{page_index:04d}.json"

    def is_page_cached(self, pdf_md5: str, source_lang: str, target_lang: str, page_index: int) -> bool:
        return self._page_path(pdf_md5, source_lang, target_lang, page_index).exists()

    def count_cached_pages(self, pdf_md5: str, source_lang: str, target_lang: str) -> int:
        d: Path = self.cache_dir / pdf_md5 / self._lang_key(source_lang, target_lang)
        if not d.exists():
            return 0
        return len(list(d.glob("page_*.json")))

    def load_page(
        self, pdf_md5: str, source_lang: str, target_lang: str,
        page_index: int, original_page: ParsedPage,
    ) -> TranslatedPage | None:
        path: Path = self._page_path(pdf_md5, source_lang, target_lang, page_index)
        if not path.exists():
            return None
        data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        # 显式标记为失败的缓存 → 视为未命中，触发重新翻译
        if not data.get("success", True):
            logger.info(f"页 {page_index}: 缓存中翻译失败（success=False），将重新翻译")
            return None
        # 旧格式缓存（无 success 字段）：通过内容检测翻译是否实际完成
        # 如果所有可翻译块的文本与原文完全相同，视为翻译失败
        if "success" not in data:
            orig_blocks: list[TextBlock] = original_page.blocks
            cached_blocks: list[dict[str, Any]] = data.get("translated_blocks", [])
            if len(orig_blocks) == len(cached_blocks):
                has_translatable: bool = False
                all_unchanged: bool = True
                for orig, cached in zip(orig_blocks, cached_blocks):
                    if orig.label in _SKIP_LABELS or not orig.text.strip():
                        continue
                    has_translatable = True
                    if orig.text != cached.get("text", ""):
                        all_unchanged = False
                        break
                if has_translatable and all_unchanged:
                    logger.info(
                        f"页 {page_index}: 旧缓存中翻译内容与原文一致，视为失败，将重新翻译"
                    )
                    return None
        translated_blocks: list[TextBlock] = [
            TextBlock(text=b["text"], label=b["label"], bbox=b["bbox"])
            for b in data["translated_blocks"]
        ]
        return TranslatedPage(
            original=original_page,
            translated_blocks=translated_blocks,
            page_index=data["page_index"],
            success=True,
        )

    def save_page(
        self, pdf_md5: str, source_lang: str, target_lang: str,
        page_index: int, translated_page: TranslatedPage,
    ) -> None:
        path: Path = self._page_path(pdf_md5, source_lang, target_lang, page_index)
        path.parent.mkdir(parents=True, exist_ok=True)
        data: dict[str, Any] = {
            "page_index": page_index,
            "success": translated_page.success,
            "translated_blocks": [
                {"text": b.text, "label": b.label, "bbox": list(b.bbox)}
                for b in translated_page.translated_blocks
            ],
        }
        path.write_text(
            json.dumps(data, ensure_ascii=False),
            encoding="utf-8",
        )
