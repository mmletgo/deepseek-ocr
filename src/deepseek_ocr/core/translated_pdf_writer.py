# -*- coding: utf-8 -*-
"""
Business Logic:
    生成翻译版PDF，支持两种模式：
    - 目标语言PDF：在原始扫描页上用白色遮盖文字区域，重新渲染翻译后的文字
    - 双语对照PDF：左半页为原始扫描，右半页为翻译文字

    公式/图表/图片区域保持原始扫描效果不做翻译。
    CJK语言使用pymupdf内置CJK字体，支持逐字符断行。
    通过ProcessPoolExecutor(forkserver)多进程并行渲染各页。

Code Logic:
    模块级worker函数负责单页渲染（支持pickle序列化到子进程）。
    TextBlock数据序列化为dict跨进程传递。
    两个worker：_render_translated_page_worker（目标语言）、_render_bilingual_page_worker（双语对照）。
    TranslatedPDFWriter类负责多进程编排和最终PDF合并。
"""

from __future__ import annotations

import concurrent.futures
import multiprocessing
import os
from pathlib import Path
from typing import TYPE_CHECKING

from deepseek_ocr.core.pdf_reader import PageImage
from deepseek_ocr.core.pdf_writer import (
    _wrap_line,
    _contains_latex,
    _clean_markdown,
    _render_text_image,
    _strip_latex,
)
from deepseek_ocr.utils.logger import logger

if TYPE_CHECKING:
    from deepseek_ocr.core.output_parser import ParsedPage
    from deepseek_ocr.core.translator import TranslatedPage

# ---------------------------------------------------------------------------
# 模块级常量
# ---------------------------------------------------------------------------

_LANG_FONT_ORDERING: dict[str, int] = {
    "Simplified Chinese": 0,
    "Traditional Chinese": 1,
    "Japanese": 2,
    "Korean": 3,
}

_SKIP_LABELS: frozenset[str] = frozenset({"formula", "equation", "image", "table"})


# ---------------------------------------------------------------------------
# 模块级辅助函数
# ---------------------------------------------------------------------------


def _is_cjk_lang(target_lang: str) -> bool:
    """判断目标语言是否为CJK语言。"""
    return target_lang in _LANG_FONT_ORDERING


def _get_font_for_lang(target_lang: str) -> object:
    """
    Business Logic:
        根据目标语言返回合适的字体。CJK语言使用pymupdf内置CJK字体，其他使用helv。

    Code Logic:
        查询 _LANG_FONT_ORDERING 获取 ordering 参数，存在则用 pymupdf.Font(ordering=N)，
        否则返回 pymupdf.Font("helv")。
    """
    import pymupdf as _fitz

    ordering: int | None = _LANG_FONT_ORDERING.get(target_lang)
    if ordering is not None:
        return _fitz.Font(ordering=ordering)
    return _fitz.Font("helv")


def _is_cjk_char(c: str) -> bool:
    """判断单个字符是否为CJK字符或全角标点。"""
    cp: int = ord(c)
    return (
        0x4E00 <= cp <= 0x9FFF       # CJK统一汉字
        or 0x3400 <= cp <= 0x4DBF    # CJK统一汉字扩展A
        or 0xF900 <= cp <= 0xFAFF    # CJK兼容汉字
        or 0x3000 <= cp <= 0x303F    # CJK符号和标点
        or 0xFF00 <= cp <= 0xFFEF    # 全角ASCII、全角标点
        or 0x3040 <= cp <= 0x309F    # 平假名
        or 0x30A0 <= cp <= 0x30FF    # 片假名
        or 0xAC00 <= cp <= 0xD7AF    # 韩文音节
        or 0x2000 <= cp <= 0x206F    # 通用标点（含中文引号等）
    )


def _wrap_line_cjk(
    line: str,
    font: object,
    fontsize: float,
    max_width: float,
) -> list[str]:
    """
    Business Logic:
        支持CJK字符的换行。CJK字符逐字符断行，非CJK部分按单词边界断行。

    Code Logic:
        逐字符遍历，累计宽度超过 max_width 时断行。
        对于连续的非CJK字符（英文单词），尽量不在单词中间断开。
    """
    if not line.strip():
        return [line]
    if font.text_length(line, fontsize=fontsize) <= max_width:  # type: ignore[union-attr]
        return [line]

    wrapped: list[str] = []
    current: str = ""
    i: int = 0
    length: int = len(line)

    while i < length:
        c: str = line[i]

        if _is_cjk_char(c):
            # CJK字符：逐字符处理
            test: str = current + c
            if font.text_length(test, fontsize=fontsize) > max_width:  # type: ignore[union-attr]
                if current:
                    wrapped.append(current)
                current = c
            else:
                current = test
            i += 1
        else:
            # 非CJK字符：收集整个英文单词
            word: str = ""
            while i < length and not _is_cjk_char(line[i]) and line[i] != " ":
                word += line[i]
                i += 1
            # 包含紧随的空格
            if i < length and line[i] == " ":
                word += " "
                i += 1

            test = current + word
            if font.text_length(test, fontsize=fontsize) > max_width:  # type: ignore[union-attr]
                if current:
                    wrapped.append(current)
                current = word
            else:
                current = test

    if current:
        wrapped.append(current)
    return wrapped if wrapped else [line]


# ---------------------------------------------------------------------------
# 多进程 worker 函数
# ---------------------------------------------------------------------------


def _render_translated_page_worker(args: tuple) -> bytes:  # type: ignore[type-arg]
    """
    Business Logic:
        多进程worker：渲染单页翻译PDF。
        在原始扫描页上用白色遮盖文字区域，重新渲染翻译后的文字。
        公式/图表/图片区域保持原始扫描效果。

    Code Logic:
        子进程内独立创建pymupdf对象，避免跨进程共享C层对象。
        创建单页文档，插入图像底层，遮盖文字区域后渲染翻译文字。
        同时写入不可见搜索层（原文，fontsize=3）供搜索使用。
    """
    import pymupdf as _fitz

    page_img: PageImage
    translated_blocks: list[dict[str, object]]
    target_lang: str
    page_img, translated_blocks, target_lang = args

    # 字体准备
    cjk_font: object = _get_font_for_lang(target_lang)
    helv_font: object = _fitz.Font("helv")
    use_cjk: bool = _is_cjk_lang(target_lang)

    doc: _fitz.Document = _fitz.open()
    try:
        page: _fitz.Page = doc.new_page(
            width=page_img.original_width,
            height=page_img.original_height,
        )
        # 底层：原始扫描图像
        page.insert_image(page.rect, stream=page_img.image_bytes, overlay=False)

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
            pdf_x1: float = bbox[0] / 999.0 * page.rect.width
            pdf_y1: float = bbox[1] / 999.0 * page.rect.height
            pdf_x2: float = bbox[2] / 999.0 * page.rect.width
            pdf_y2: float = bbox[3] / 999.0 * page.rect.height

            if pdf_x2 <= pdf_x1 or pdf_y2 <= pdf_y1:
                continue

            bbox_width: float = pdf_x2 - pdf_x1
            bbox_height: float = pdf_y2 - pdf_y1
            block_rect: _fitz.Rect = _fitz.Rect(pdf_x1, pdf_y1, pdf_x2, pdf_y2)

            # 白色遮盖原始扫描的文字区域
            page.draw_rect(block_rect, color=None, fill=(1, 1, 1), overlay=True)

            # 3路径渲染翻译文字
            # CJK 语言跳过 matplotlib 路径：matplotlib 不支持 CJK 文字换行，
            # 长文本会被压缩成极小字体。剥离 LaTeX 定界符后走纯文本 CJK 渲染。
            if _contains_latex(text) and not use_cjk:
                # 含内联 LaTeX → matplotlib 渲染（仅非 CJK 语言）
                try:
                    png_bytes: bytes = _render_text_image(text, label, bbox_width, bbox_height)
                    page.insert_image(block_rect, stream=png_bytes, overlay=True)
                    # 不可见搜索层
                    try:
                        tw_search.append(
                            (pdf_x1, pdf_y1 + 10), text[:200],
                            font=helv_font, fontsize=3,
                        )
                    except Exception:
                        pass
                    continue  # 跳过后续的纯文本渲染
                except Exception:
                    pass  # 回退到纯文本渲染

            # 纯文本渲染（或 LaTeX 渲染失败的回退）
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

            # 字号自适应：缩小字号直到文字能放入bbox
            for _ in range(20):
                if len(wrapped) * fontsize_txt * 1.2 <= bbox_height or fontsize_txt <= 3.0:
                    break
                fontsize_txt *= 0.9
                wrapped = []
                for ln in lines:
                    if use_cjk:
                        wrapped.extend(
                            _wrap_line_cjk(ln, render_font, fontsize_txt, bbox_width)
                        )
                    else:
                        wrapped.extend(
                            _wrap_line(ln, render_font, fontsize_txt, bbox_width)
                        )

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

            # 不可见搜索层：写入原文（helv字体，fontsize=3）
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


def _render_bilingual_page_worker(args: tuple) -> bytes:  # type: ignore[type-arg]
    """
    Business Logic:
        多进程worker：渲染单页双语对照PDF。
        左半页为原始扫描图像，右半页为翻译文字（白色背景）。
        中间用淡灰色分隔线分割。

    Code Logic:
        创建双倍宽度页面，左侧插入原始扫描图，右侧白色背景上渲染翻译文字。
        翻译文字坐标在x轴上偏移original_width，y轴保持与原始扫描对应位置对齐。
    """
    import pymupdf as _fitz

    page_img: PageImage
    original_blocks: list[dict[str, object]]
    translated_blocks: list[dict[str, object]]
    target_lang: str
    page_img, original_blocks, translated_blocks, target_lang = args

    # 字体准备
    cjk_font: object = _get_font_for_lang(target_lang)
    helv_font: object = _fitz.Font("helv")
    use_cjk: bool = _is_cjk_lang(target_lang)

    orig_w: float = page_img.original_width
    orig_h: float = page_img.original_height

    doc: _fitz.Document = _fitz.open()
    try:
        page: _fitz.Page = doc.new_page(width=orig_w * 2, height=orig_h)

        # 左半页：插入原始扫描图像
        left_rect: _fitz.Rect = _fitz.Rect(0, 0, orig_w, orig_h)
        page.insert_image(left_rect, stream=page_img.image_bytes, overlay=False)

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

        # 创建 Pixmap 用于复制原始扫描中的公式/图表/图片到右半页
        full_pix: _fitz.Pixmap | None = None
        try:
            full_pix = _fitz.Pixmap(page_img.image_bytes)
        except Exception:
            pass

        # 右半页渲染翻译文字
        tw_right: _fitz.TextWriter = _fitz.TextWriter(page.rect)

        for block in translated_blocks:
            text: str = str(block.get("text", ""))
            label: str = str(block.get("label", ""))
            bbox: list[int] = list(block.get("bbox", [0, 0, 999, 999]))  # type: ignore[arg-type]

            # 公式/图表/图片：复制原始扫描对应区域到右半页
            if label in _SKIP_LABELS:
                if full_pix is not None:
                    r_x1: float = bbox[0] / 999.0 * orig_w + orig_w
                    r_y1: float = bbox[1] / 999.0 * orig_h
                    r_x2: float = bbox[2] / 999.0 * orig_w + orig_w
                    r_y2: float = bbox[3] / 999.0 * orig_h
                    if r_x2 > r_x1 and r_y2 > r_y1:
                        try:
                            pix_x1: int = max(0, int(bbox[0] / 999 * full_pix.width))
                            pix_y1: int = max(0, int(bbox[1] / 999 * full_pix.height))
                            pix_x2: int = min(full_pix.width, int(bbox[2] / 999 * full_pix.width))
                            pix_y2: int = min(full_pix.height, int(bbox[3] / 999 * full_pix.height))
                            if pix_x2 > pix_x1 and pix_y2 > pix_y1:
                                clip: _fitz.IRect = _fitz.IRect(pix_x1, pix_y1, pix_x2, pix_y2)
                                cropped: _fitz.Pixmap = _fitz.Pixmap(
                                    full_pix.colorspace, clip, full_pix.alpha,
                                )
                                cropped.copy(full_pix, clip)
                                cropped.set_origin(0, 0)
                                dest: _fitz.Rect = _fitz.Rect(r_x1, r_y1, r_x2, r_y2)
                                page.insert_image(dest, pixmap=cropped, overlay=True)
                        except Exception:
                            pass
                continue

            if not text.strip():
                continue

            # 坐标转换到右半页：x轴偏移original_width
            pdf_x1: float = bbox[0] / 999.0 * orig_w + orig_w
            pdf_y1: float = bbox[1] / 999.0 * orig_h
            pdf_x2: float = bbox[2] / 999.0 * orig_w + orig_w
            pdf_y2: float = bbox[3] / 999.0 * orig_h

            if pdf_x2 <= pdf_x1 or pdf_y2 <= pdf_y1:
                continue

            bbox_width: float = pdf_x2 - pdf_x1
            bbox_height: float = pdf_y2 - pdf_y1

            # 3路径渲染翻译文字
            # CJK 语言跳过 matplotlib 路径（不支持 CJK 换行）
            if _contains_latex(text) and not use_cjk:
                try:
                    png_bytes: bytes = _render_text_image(text, label, bbox_width, bbox_height)
                    img_rect: _fitz.Rect = _fitz.Rect(pdf_x1, pdf_y1, pdf_x2, pdf_y2)
                    page.insert_image(img_rect, stream=png_bytes, overlay=True)
                    continue
                except Exception:
                    pass  # 回退到纯文本

            # 纯文本渲染（或 LaTeX 渲染失败的回退）
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
                        wrapped.extend(
                            _wrap_line_cjk(ln, render_font, fontsize_txt, bbox_width)
                        )
                    else:
                        wrapped.extend(
                            _wrap_line(ln, render_font, fontsize_txt, bbox_width)
                        )

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


# ---------------------------------------------------------------------------
# TranslatedPDFWriter 类
# ---------------------------------------------------------------------------


class TranslatedPDFWriter:
    """翻译版PDF生成器，支持目标语言PDF和双语对照PDF"""

    def __init__(self) -> None:
        """
        Business Logic:
            初始化翻译PDF写入器。

        Code Logic:
            仅记录日志，实际字体创建在子进程worker中完成。
        """
        logger.info("TranslatedPDFWriter初始化完成")

    def create_translated_pdf(
        self,
        page_images: list[PageImage],
        translated_pages: list["TranslatedPage"],
        output_path: str | Path,
        target_lang: str = "Simplified Chinese",
    ) -> Path:
        """
        Business Logic:
            生成目标语言PDF。在原始扫描页上用白色遮盖文字区域，重新渲染翻译后的文字。
            公式/图表/图片保持原始扫描效果。

        Code Logic:
            使用ProcessPoolExecutor(forkserver)并行渲染各页。
            将TranslatedPage.translated_blocks序列化为dict列表传入worker。
            并行完成后按原始顺序insert_pdf合并，最终deflate=False轻量保存。
        """
        import pymupdf

        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        total: int = len(page_images)
        logger.info(f"开始生成翻译PDF: {output_path}, 共 {total} 页, 目标语言: {target_lang}")

        # 序列化 translated_blocks 为 dict 列表
        worker_args: list[tuple[PageImage, list[dict[str, object]], str]] = []
        for page_img, tp in zip(page_images, translated_pages):
            blocks_dicts: list[dict[str, object]] = [
                {"text": b.text, "label": b.label, "bbox": b.bbox}
                for b in tp.translated_blocks
            ]
            worker_args.append((page_img, blocks_dicts, target_lang))

        # 多进程并行渲染
        cpu_count: int = os.cpu_count() or 4
        max_workers: int = max(1, cpu_count - 2)
        mp_ctx = multiprocessing.get_context("forkserver")

        with concurrent.futures.ProcessPoolExecutor(
            max_workers=max_workers,
            mp_context=mp_ctx,
        ) as executor:
            futures = [
                executor.submit(_render_translated_page_worker, args)
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
            logger.info(f"翻译PDF生成完成: {output_path}")
        finally:
            final_doc.close()

        return output_path

    def create_bilingual_pdf(
        self,
        page_images: list[PageImage],
        original_pages: list["ParsedPage"],
        translated_pages: list["TranslatedPage"],
        output_path: str | Path,
        target_lang: str = "Simplified Chinese",
    ) -> Path:
        """
        Business Logic:
            生成双语对照PDF。左半页为原始扫描，右半页为翻译文字。
            公式/图表/图片在右半页留白（左侧已有原图）。

        Code Logic:
            使用ProcessPoolExecutor(forkserver)并行渲染各页。
            将original_pages和translated_pages的blocks序列化为dict列表传入worker。
            并行完成后按原始顺序insert_pdf合并。
        """
        import pymupdf

        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        total: int = len(page_images)
        logger.info(f"开始生成双语对照PDF: {output_path}, 共 {total} 页, 目标语言: {target_lang}")

        # 序列化 blocks 为 dict 列表
        worker_args: list[
            tuple[PageImage, list[dict[str, object]], list[dict[str, object]], str]
        ] = []
        for page_img, op, tp in zip(page_images, original_pages, translated_pages):
            orig_dicts: list[dict[str, object]] = [
                {"text": b.text, "label": b.label, "bbox": b.bbox}
                for b in op.blocks
            ]
            trans_dicts: list[dict[str, object]] = [
                {"text": b.text, "label": b.label, "bbox": b.bbox}
                for b in tp.translated_blocks
            ]
            worker_args.append((page_img, orig_dicts, trans_dicts, target_lang))

        # 多进程并行渲染
        cpu_count: int = os.cpu_count() or 4
        max_workers: int = max(1, cpu_count - 2)
        mp_ctx = multiprocessing.get_context("forkserver")

        with concurrent.futures.ProcessPoolExecutor(
            max_workers=max_workers,
            mp_context=mp_ctx,
        ) as executor:
            futures = [
                executor.submit(_render_bilingual_page_worker, args)
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
            logger.info(f"双语对照PDF生成完成: {output_path}")
        finally:
            final_doc.close()

        return output_path
