# -*- coding: utf-8 -*-
"""
为文本类型PDF生成翻译版PDF和双语对照PDF。
与 TranslatedPDFWriter（扫描PDF用）不同，使用 show_pdf_page() 保留原始PDF的矢量内容，
而非渲染为扫描PNG图片作为底层。
通过 ProcessPoolExecutor(forkserver) 多进程并行渲染各页。
"""

from __future__ import annotations

import concurrent.futures
import multiprocessing
import os
from pathlib import Path
from typing import TYPE_CHECKING

from deepseek_ocr.core.pdf_writer import (
    _wrap_line,
    _contains_latex,
    _clean_markdown,
    _render_text_image,
    _strip_latex,
)
from deepseek_ocr.core.translated_pdf_writer import (
    _SKIP_LABELS,
    _get_font_for_lang,
    _is_cjk_lang,
    _wrap_line_cjk,
)
from deepseek_ocr.utils.logger import logger

if TYPE_CHECKING:
    from deepseek_ocr.core.output_parser import ParsedPage
    from deepseek_ocr.core.translator import TranslatedPage


# ---------------------------------------------------------------------------
# 多进程 worker 函数（模块级，支持 pickle 序列化）
# ---------------------------------------------------------------------------


def _render_text_translated_page_worker(args: tuple) -> bytes:  # type: ignore[type-arg]
    """
    多进程 worker：为文本PDF渲染单页翻译版PDF。
    使用 show_pdf_page() 复制原始页面内容，白色遮盖文字区域后渲染翻译文字。
    """
    import pymupdf as _fitz

    source_pdf_path: str
    page_index: int
    page_width: float
    page_height: float
    translated_blocks: list[dict[str, object]]
    target_lang: str
    source_pdf_path, page_index, page_width, page_height, translated_blocks, target_lang = args

    # 字体准备
    cjk_font: object = _get_font_for_lang(target_lang)
    helv_font: object = _fitz.Font("helv")
    use_cjk: bool = _is_cjk_lang(target_lang)

    # 打开源PDF（每个 worker 独立打开）
    src_doc: _fitz.Document = _fitz.open(source_pdf_path)

    doc: _fitz.Document = _fitz.open()
    try:
        page: _fitz.Page = doc.new_page(width=page_width, height=page_height)

        # 底层：复制原始PDF页面（保留矢量内容）
        page.show_pdf_page(page.rect, src_doc, pno=page_index)

        # 文字层
        tw_visible: _fitz.TextWriter = _fitz.TextWriter(page.rect)
        tw_search: _fitz.TextWriter = _fitz.TextWriter(page.rect)

        for block in translated_blocks:
            text: str = str(block.get("text", ""))
            label: str = str(block.get("label", ""))
            bbox: list[int] = list(block.get("bbox", [0, 0, 999, 999]))  # type: ignore[arg-type]

            if not text.strip():
                continue
            if label in _SKIP_LABELS:
                continue

            # 坐标转换：归一化(0-999) → PDF坐标(pt)
            pdf_x1: float = bbox[0] / 999.0 * page_width
            pdf_y1: float = bbox[1] / 999.0 * page_height
            pdf_x2: float = bbox[2] / 999.0 * page_width
            pdf_y2: float = bbox[3] / 999.0 * page_height

            if pdf_x2 <= pdf_x1 or pdf_y2 <= pdf_y1:
                continue

            bbox_width: float = pdf_x2 - pdf_x1
            bbox_height: float = pdf_y2 - pdf_y1
            block_rect: _fitz.Rect = _fitz.Rect(pdf_x1, pdf_y1, pdf_x2, pdf_y2)

            # 白色遮盖原始文字区域
            page.draw_rect(block_rect, color=None, fill=(1, 1, 1), overlay=True)

            # 3路径渲染翻译文字
            if _contains_latex(text) and not use_cjk:
                try:
                    png_bytes: bytes = _render_text_image(text, label, bbox_width, bbox_height)
                    page.insert_image(block_rect, stream=png_bytes, overlay=True)
                    try:
                        tw_search.append(
                            (pdf_x1, pdf_y1 + 10), text[:200],
                            font=helv_font, fontsize=3,
                        )
                    except Exception:
                        pass
                    continue
                except Exception:
                    pass  # 回退到纯文本渲染

            # 纯文本渲染
            stripped_text: str = _strip_latex(text.strip()) if use_cjk else text.strip()
            clean_text: str = _clean_markdown(stripped_text)
            render_font: object = cjk_font if use_cjk else helv_font
            lines: list[str] = clean_text.split("\n")
            fontsize_txt: float = max(
                min(bbox_height / max(len(lines), 1) * 0.75, 14.0), 3.0
            )

            # 换行
            if use_cjk:
                wrapped: list[str] = []
                for ln in lines:
                    wrapped.extend(_wrap_line_cjk(ln, render_font, fontsize_txt, bbox_width))
            else:
                wrapped = []
                for ln in lines:
                    wrapped.extend(_wrap_line(ln, render_font, fontsize_txt, bbox_width))

            # 字号自适应
            for _ in range(20):
                if len(wrapped) * fontsize_txt * 1.2 <= bbox_height or fontsize_txt <= 3.0:
                    break
                fontsize_txt *= 0.9
                wrapped = []
                for ln in lines:
                    if use_cjk:
                        wrapped.extend(_wrap_line_cjk(ln, render_font, fontsize_txt, bbox_width))
                    else:
                        wrapped.extend(_wrap_line(ln, render_font, fontsize_txt, bbox_width))

            fontsize_txt = max(fontsize_txt, 3.0)
            line_height: float = fontsize_txt * 1.2
            y: float = pdf_y1 + fontsize_txt * 0.85

            for ln in wrapped:
                if y > pdf_y2:
                    break
                if ln.strip():
                    try:
                        tw_visible.append(
                            (pdf_x1, y), ln, font=render_font, fontsize=fontsize_txt
                        )
                    except Exception:
                        pass
                y += line_height

            # 不可见搜索层：写入原文
            try:
                tw_search.append(
                    (pdf_x1, pdf_y1 + 10),
                    text[:200],
                    font=helv_font,
                    fontsize=3,
                )
            except Exception:
                pass

        tw_visible.write_text(page, render_mode=0)
        tw_search.write_text(page, render_mode=3)

        return doc.tobytes(deflate=True, garbage=0)
    finally:
        doc.close()
        src_doc.close()


def _render_text_bilingual_page_worker(args: tuple) -> bytes:  # type: ignore[type-arg]
    """
    多进程 worker：为文本PDF渲染单页双语对照PDF。
    左半页为原始PDF页面(show_pdf_page)，右半页为白色背景+翻译文字。
    """
    import pymupdf as _fitz

    source_pdf_path: str
    page_index: int
    page_width: float
    page_height: float
    translated_blocks: list[dict[str, object]]
    target_lang: str
    source_pdf_path, page_index, page_width, page_height, translated_blocks, target_lang = args

    # 字体准备
    cjk_font: object = _get_font_for_lang(target_lang)
    helv_font: object = _fitz.Font("helv")
    use_cjk: bool = _is_cjk_lang(target_lang)

    orig_w: float = page_width
    orig_h: float = page_height

    # 打开源PDF
    src_doc: _fitz.Document = _fitz.open(source_pdf_path)

    doc: _fitz.Document = _fitz.open()
    try:
        page: _fitz.Page = doc.new_page(width=orig_w * 2, height=orig_h)

        # 左半页：复制原始PDF页面
        left_rect: _fitz.Rect = _fitz.Rect(0, 0, orig_w, orig_h)
        page.show_pdf_page(left_rect, src_doc, pno=page_index)

        # 右半页：白色背景
        right_bg: _fitz.Rect = _fitz.Rect(orig_w, 0, orig_w * 2, orig_h)
        page.draw_rect(right_bg, color=None, fill=(1, 1, 1), overlay=True)

        # 中间分隔线
        page.draw_line(
            _fitz.Point(orig_w, 0),
            _fitz.Point(orig_w, orig_h),
            color=(0.85, 0.85, 0.85),
            width=0.5,
        )

        # 右半页渲染翻译文字
        tw_right: _fitz.TextWriter = _fitz.TextWriter(page.rect)

        for block in translated_blocks:
            text: str = str(block.get("text", ""))
            label: str = str(block.get("label", ""))
            bbox: list[int] = list(block.get("bbox", [0, 0, 999, 999]))  # type: ignore[arg-type]

            # 跳过标签：不在右半页渲染（左半页原始内容已包含）
            if label in _SKIP_LABELS:
                continue

            if not text.strip():
                continue

            # 坐标转换到右半页：x轴偏移 orig_w
            pdf_x1: float = bbox[0] / 999.0 * orig_w + orig_w
            pdf_y1: float = bbox[1] / 999.0 * orig_h
            pdf_x2: float = bbox[2] / 999.0 * orig_w + orig_w
            pdf_y2: float = bbox[3] / 999.0 * orig_h

            if pdf_x2 <= pdf_x1 or pdf_y2 <= pdf_y1:
                continue

            bbox_width: float = pdf_x2 - pdf_x1
            bbox_height: float = pdf_y2 - pdf_y1

            # 3路径渲染
            if _contains_latex(text) and not use_cjk:
                try:
                    png_bytes: bytes = _render_text_image(text, label, bbox_width, bbox_height)
                    img_rect: _fitz.Rect = _fitz.Rect(pdf_x1, pdf_y1, pdf_x2, pdf_y2)
                    page.insert_image(img_rect, stream=png_bytes, overlay=True)
                    continue
                except Exception:
                    pass

            # 纯文本渲染
            stripped_text: str = _strip_latex(text.strip()) if use_cjk else text.strip()
            clean_text: str = _clean_markdown(stripped_text)
            render_font: object = cjk_font if use_cjk else helv_font
            lines: list[str] = clean_text.split("\n")
            fontsize_txt: float = max(
                min(bbox_height / max(len(lines), 1) * 0.75, 14.0), 3.0
            )

            # 换行
            if use_cjk:
                wrapped: list[str] = []
                for ln in lines:
                    wrapped.extend(_wrap_line_cjk(ln, render_font, fontsize_txt, bbox_width))
            else:
                wrapped = []
                for ln in lines:
                    wrapped.extend(_wrap_line(ln, render_font, fontsize_txt, bbox_width))

            # 字号自适应
            for _ in range(20):
                if len(wrapped) * fontsize_txt * 1.2 <= bbox_height or fontsize_txt <= 3.0:
                    break
                fontsize_txt *= 0.9
                wrapped = []
                for ln in lines:
                    if use_cjk:
                        wrapped.extend(_wrap_line_cjk(ln, render_font, fontsize_txt, bbox_width))
                    else:
                        wrapped.extend(_wrap_line(ln, render_font, fontsize_txt, bbox_width))

            fontsize_txt = max(fontsize_txt, 3.0)
            line_height: float = fontsize_txt * 1.2
            y: float = pdf_y1 + fontsize_txt * 0.85

            for ln in wrapped:
                if y > pdf_y2:
                    break
                if ln.strip():
                    try:
                        tw_right.append(
                            (pdf_x1, y), ln, font=render_font, fontsize=fontsize_txt
                        )
                    except Exception:
                        pass
                y += line_height

        tw_right.write_text(page, render_mode=0)

        return doc.tobytes(deflate=True, garbage=0)
    finally:
        doc.close()
        src_doc.close()


# ---------------------------------------------------------------------------
# TextPDFTranslatedWriter 类
# ---------------------------------------------------------------------------


class TextPDFTranslatedWriter:
    """文本PDF翻译版生成器，使用 show_pdf_page() 保留原始矢量内容"""

    def __init__(self) -> None:
        logger.info("TextPDFTranslatedWriter初始化完成")

    def create_translated_pdf(
        self,
        source_pdf_path: str | Path,
        translated_pages: list["TranslatedPage"],
        output_path: str | Path,
        target_lang: str = "Simplified Chinese",
    ) -> Path:
        """
        生成目标语言PDF。
        使用 show_pdf_page() 复制原始页面，白色遮盖文字区域，重新渲染翻译文字。
        """
        import pymupdf

        source_pdf_path = Path(source_pdf_path)
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        # 获取每页尺寸
        src_doc: pymupdf.Document = pymupdf.open(str(source_pdf_path))
        page_sizes: list[tuple[float, float]] = [
            (src_doc[i].rect.width, src_doc[i].rect.height)
            for i in range(src_doc.page_count)
        ]
        src_doc.close()

        total: int = len(translated_pages)
        logger.info(f"开始生成文本PDF翻译版: {output_path}, 共 {total} 页, 目标语言: {target_lang}")

        # 序列化参数
        worker_args: list[tuple[str, int, float, float, list[dict[str, object]], str]] = []
        for i, tp in enumerate(translated_pages):
            pw, ph = page_sizes[i]
            blocks_dicts: list[dict[str, object]] = [
                {"text": b.text, "label": b.label, "bbox": b.bbox}
                for b in tp.translated_blocks
            ]
            worker_args.append((str(source_pdf_path), i, pw, ph, blocks_dicts, target_lang))

        # 多进程并行渲染
        cpu_count: int = os.cpu_count() or 4
        max_workers: int = max(1, cpu_count - 2)
        mp_ctx = multiprocessing.get_context("forkserver")

        with concurrent.futures.ProcessPoolExecutor(
            max_workers=max_workers,
            mp_context=mp_ctx,
        ) as executor:
            futures = [
                executor.submit(_render_text_translated_page_worker, args)
                for args in worker_args
            ]
            page_bytes_list: list[bytes] = [f.result() for f in futures]

        logger.info(f"所有页面并行渲染完成，开始合并 {total} 页...")

        # 合并所有单页PDF
        final_doc: pymupdf.Document = pymupdf.open()
        try:
            for page_bytes in page_bytes_list:
                src: pymupdf.Document = pymupdf.open("pdf", page_bytes)
                final_doc.insert_pdf(src)
                src.close()
            final_doc.save(str(output_path), deflate=False, garbage=1)
            logger.info(f"文本PDF翻译版生成完成: {output_path}")
        finally:
            final_doc.close()

        return output_path

    def create_bilingual_pdf(
        self,
        source_pdf_path: str | Path,
        original_pages: list["ParsedPage"],
        translated_pages: list["TranslatedPage"],
        output_path: str | Path,
        target_lang: str = "Simplified Chinese",
    ) -> Path:
        """
        生成双语对照PDF。
        左半页为原始PDF页面(show_pdf_page)，右半页为翻译文字。
        """
        import pymupdf

        source_pdf_path = Path(source_pdf_path)
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        # 获取每页尺寸
        src_doc: pymupdf.Document = pymupdf.open(str(source_pdf_path))
        page_sizes: list[tuple[float, float]] = [
            (src_doc[i].rect.width, src_doc[i].rect.height)
            for i in range(src_doc.page_count)
        ]
        src_doc.close()

        total: int = len(translated_pages)
        logger.info(f"开始生成文本PDF双语版: {output_path}, 共 {total} 页, 目标语言: {target_lang}")

        # 序列化参数（双语版只传 translated_blocks，skip_labels 块直接跳过）
        worker_args: list[tuple[str, int, float, float, list[dict[str, object]], str]] = []
        for i, tp in enumerate(translated_pages):
            pw, ph = page_sizes[i]
            trans_dicts: list[dict[str, object]] = [
                {"text": b.text, "label": b.label, "bbox": b.bbox}
                for b in tp.translated_blocks
            ]
            worker_args.append((str(source_pdf_path), i, pw, ph, trans_dicts, target_lang))

        # 多进程并行渲染
        cpu_count: int = os.cpu_count() or 4
        max_workers: int = max(1, cpu_count - 2)
        mp_ctx = multiprocessing.get_context("forkserver")

        with concurrent.futures.ProcessPoolExecutor(
            max_workers=max_workers,
            mp_context=mp_ctx,
        ) as executor:
            futures = [
                executor.submit(_render_text_bilingual_page_worker, args)
                for args in worker_args
            ]
            page_bytes_list: list[bytes] = [f.result() for f in futures]

        logger.info(f"所有页面并行渲染完成，开始合并 {total} 页...")

        # 合并所有单页PDF
        final_doc: pymupdf.Document = pymupdf.open()
        try:
            for page_bytes in page_bytes_list:
                src: pymupdf.Document = pymupdf.open("pdf", page_bytes)
                final_doc.insert_pdf(src)
                src.close()
            final_doc.save(str(output_path), deflate=False, garbage=1)
            logger.info(f"文本PDF双语版生成完成: {output_path}")
        finally:
            final_doc.close()

        return output_path
