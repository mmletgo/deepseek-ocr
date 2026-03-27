# -*- coding: utf-8 -*-
"""
Business Logic:
    生成双层PDF：底层是扫描图像(保持原始外观)，上层是透明文字层(支持搜索和复制)。
    支持两种模式：
    - dual_layer: 透明文字层(render_mode=3)，保持原始扫描外观
    - rewrite: 白色遮盖文字区域 + 矢量字体重绘(render_mode=0)，图表/公式保持原始扫描

Code Logic:
    使用PyMuPDF创建新PDF文档，逐页插入:
    1. 底层：原始扫描图像(overlay=False)
    2. rewrite模式下：白色矩形遮盖文字区域
    3. 上层：使用TextWriter写入文字(render_mode由模式决定)
    坐标从归一化(0-999)转换为PDF坐标(pt)。
    页面渲染通过 ProcessPoolExecutor 多进程并行（每进程独立 GIL，真正多核）。
"""

import concurrent.futures
import multiprocessing
import os
import pymupdf
from pathlib import Path

from deepseek_ocr.core.pdf_reader import PageImage
from deepseek_ocr.core.output_parser import ParsedPage
from deepseek_ocr.utils.logger import logger

_KEEP_ORIGINAL_LABELS: frozenset[str] = frozenset({"image", "table", "formula"})


def _render_page_worker(args: tuple[PageImage, ParsedPage, str]) -> bytes:
    """
    Business Logic:
        多进程 worker：渲染单页双层 PDF 字节流。
        作为模块级顶层函数以支持 pickle 序列化传递到子进程。

    Code Logic:
        子进程内独立创建 pymupdf.Font，避免跨进程共享 C 层对象。
        创建单页文档，插入图像底层和透明文字层，返回压缩 PDF 字节。
        rewrite模式下额外绘制白色遮盖矩形，使用 render_mode=0 渲染可见文字。
    """
    import pymupdf as _fitz  # 子进程内导入，避免 fork 后状态污染

    page_img, parsed, mode = args
    font = _fitz.Font("helv")
    doc = _fitz.open()
    try:
        page = doc.new_page(
            width=page_img.original_width,
            height=page_img.original_height,
        )
        page.insert_image(page.rect, stream=page_img.image_bytes, overlay=False)

        # rewrite 模式：白色矩形遮盖文字区域
        if mode == "rewrite":
            for block in parsed.blocks:
                if not block.text.strip():
                    continue
                if block.label in _KEEP_ORIGINAL_LABELS:
                    continue
                r_x1: float = block.bbox[0] / 999.0 * page.rect.width
                r_y1: float = block.bbox[1] / 999.0 * page.rect.height
                r_x2: float = block.bbox[2] / 999.0 * page.rect.width
                r_y2: float = block.bbox[3] / 999.0 * page.rect.height
                if r_x2 <= r_x1 or r_y2 <= r_y1:
                    continue
                page.draw_rect(
                    _fitz.Rect(r_x1, r_y1, r_x2, r_y2),
                    color=None, fill=(1, 1, 1), overlay=True,
                )

        tw = _fitz.TextWriter(page.rect)
        for block in parsed.blocks:
            if not block.text.strip():
                continue
            if mode == "rewrite" and block.label in _KEEP_ORIGINAL_LABELS:
                continue
            pdf_x1: float = block.bbox[0] / 999.0 * page.rect.width
            pdf_y1: float = block.bbox[1] / 999.0 * page.rect.height
            pdf_x2: float = block.bbox[2] / 999.0 * page.rect.width
            pdf_y2: float = block.bbox[3] / 999.0 * page.rect.height
            if pdf_x2 <= pdf_x1 or pdf_y2 <= pdf_y1:
                continue
            lines: list[str] = block.text.strip().split('\n')
            fontsize: float = max(min((pdf_y2 - pdf_y1) / max(len(lines), 1) * 0.75, 36.0), 3.0)
            # 逐行用 tw.append() 放置，完全避免 fill_textbox 的无限循环 bug
            line_height: float = fontsize * 1.2
            y: float = pdf_y1 + fontsize * 0.85  # 首行基线
            for line in lines:
                if y > pdf_y2:
                    break
                if line.strip():
                    try:
                        tw.append((pdf_x1, y), line, font=font, fontsize=fontsize)
                    except Exception:
                        pass
                y += line_height
        # dual_layer: render_mode=3(不可见), rewrite: render_mode=0(可见)
        render_mode: int = 0 if mode == "rewrite" else 3
        tw.write_text(page, render_mode=render_mode)
        # deflate=True：压缩图像流，避免PNG像素流以未压缩形式输出（否则单页~7MB）
        return doc.tobytes(deflate=True, garbage=0)
    finally:
        doc.close()


class DualLayerPDFWriter:
    """双层PDF生成器，底层扫描图像 + 上层透明文字层"""

    def __init__(self) -> None:
        """
        Business Logic:
            初始化PDF写入器，准备用于文字层的字体（CLI串行路径使用）。

        Code Logic:
            加载Helvetica内置字体，用于写入不可见文字层。
        """
        self.font: pymupdf.Font = pymupdf.Font("helv")
        logger.info("DualLayerPDFWriter初始化完成")

    def create_dual_layer_pdf(
        self,
        page_images: list[PageImage],
        parsed_pages: list[ParsedPage],
        output_path: str | Path,
        mode: str = "dual_layer",
    ) -> Path:
        """
        Business Logic:
            将扫描图像和OCR文本合并为PDF。
            dual_layer模式：外观与原始扫描PDF一致，文字不可见但可搜索。
            rewrite模式：文字区域用白色覆盖后重新渲染可见矢量文字，图表/公式保持原始扫描。

        Code Logic:
            使用 ProcessPoolExecutor（forkserver）并行渲染各页：
            - 每个子进程独立 GIL，真正多核并行
            - forkserver 上下文：fork 发生在干净的服务进程中
            - max_workers = cpu_count - 2（留 2 核给 asyncio 事件循环和 Ollama）
            并行完成后按原始顺序 insert_pdf 合并，最终 deflate=False 轻量保存。
        """
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        total: int = len(page_images)
        logger.info(f"开始生成PDF ({mode}模式): {output_path}, 共 {total} 页")

        cpu_count: int = os.cpu_count() or 4
        max_workers: int = max(1, cpu_count - 2)

        # forkserver：在干净子进程中 fork，避免多线程主进程的锁状态被继承
        mp_ctx = multiprocessing.get_context("forkserver")
        with concurrent.futures.ProcessPoolExecutor(
            max_workers=max_workers,
            mp_context=mp_ctx,
        ) as executor:
            futures = [
                executor.submit(_render_page_worker, (pi, pp, mode))
                for pi, pp in zip(page_images, parsed_pages)
            ]
            page_bytes_list: list[bytes] = [f.result() for f in futures]

        logger.info(f"所有页面并行渲染完成，开始合并 {total} 页...")
        final_doc: pymupdf.Document = pymupdf.open()
        try:
            for page_bytes in page_bytes_list:
                src: pymupdf.Document = pymupdf.open("pdf", page_bytes)
                final_doc.insert_pdf(src)
                src.close()
            final_doc.save(str(output_path), deflate=False, garbage=1)
            logger.info(f"PDF生成完成: {output_path}")
        finally:
            final_doc.close()

        return output_path

    def _add_page(
        self,
        doc: pymupdf.Document,
        page_img: PageImage,
        parsed: ParsedPage,
        mode: str = "dual_layer",
    ) -> None:
        """
        Business Logic:
            在PDF文档中添加一页，包含底层扫描图像和上层文字。
            dual_layer模式：文字不可见(render_mode=3)，仅用于搜索。
            rewrite模式：白色矩形遮盖文字区域后重新渲染可见文字(render_mode=0)，
            图表/图片/公式区域保持原始扫描效果。

        Code Logic:
            1. 创建新页面，尺寸与原始PDF一致
            2. 插入扫描图像作为底层(overlay=False)
            3. rewrite模式下，用白色矩形遮盖文字区域
            4. 使用TextWriter逐行append文字
            5. 坐标转换：归一化(0-999) -> PDF坐标(pt)
        """
        # 1. 创建新页面，尺寸与原始PDF一致
        page: pymupdf.Page = doc.new_page(
            width=page_img.original_width,
            height=page_img.original_height,
        )

        # 2. 插入扫描图像作为底层
        page.insert_image(
            page.rect,
            stream=page_img.image_bytes,
            overlay=False,
        )

        # 3. rewrite模式：白色矩形遮盖文字区域
        if mode == "rewrite":
            for block in parsed.blocks:
                if not block.text.strip():
                    continue
                if block.label in _KEEP_ORIGINAL_LABELS:
                    continue
                r_x1: float = block.bbox[0] / 999.0 * page.rect.width
                r_y1: float = block.bbox[1] / 999.0 * page.rect.height
                r_x2: float = block.bbox[2] / 999.0 * page.rect.width
                r_y2: float = block.bbox[3] / 999.0 * page.rect.height
                if r_x2 <= r_x1 or r_y2 <= r_y1:
                    continue
                page.draw_rect(
                    pymupdf.Rect(r_x1, r_y1, r_x2, r_y2),
                    color=None, fill=(1, 1, 1), overlay=True,
                )

        # 4. 用TextWriter写入文字层
        tw: pymupdf.TextWriter = pymupdf.TextWriter(page.rect)

        for block in parsed.blocks:
            if not block.text.strip():
                continue

            # rewrite模式下，图表/图片/公式保持原始扫描效果
            if mode == "rewrite" and block.label in _KEEP_ORIGINAL_LABELS:
                continue

            # 坐标转换：归一化(0-999) -> PDF坐标
            pdf_x1: float = block.bbox[0] / 999.0 * page.rect.width
            pdf_y1: float = block.bbox[1] / 999.0 * page.rect.height
            pdf_x2: float = block.bbox[2] / 999.0 * page.rect.width
            pdf_y2: float = block.bbox[3] / 999.0 * page.rect.height

            rect: pymupdf.Rect = pymupdf.Rect(pdf_x1, pdf_y1, pdf_x2, pdf_y2)

            if rect.width <= 0 or rect.height <= 0:
                logger.debug(f"页 {page_img.page_index}: 跳过无效区域 {block.bbox}")
                continue

            # 估算字号：根据区域高度和行数计算
            lines: list[str] = block.text.strip().split('\n')
            line_count: int = max(len(lines), 1)
            fontsize: float = max(min(rect.height / line_count * 0.75, 36.0), 3.0)

            # 逐行用 tw.append() 放置，完全避免 fill_textbox 的无限循环 bug
            line_height: float = fontsize * 1.2
            y: float = pdf_y1 + fontsize * 0.85  # 首行基线
            for line in lines:
                if y > pdf_y2:
                    break
                if line.strip():
                    try:
                        tw.append((pdf_x1, y), line, font=self.font, fontsize=fontsize)
                    except Exception:
                        pass
                y += line_height

        # 5. dual_layer: render_mode=3(不可见), rewrite: render_mode=0(可见)
        render_mode: int = 0 if mode == "rewrite" else 3
        tw.write_text(page, render_mode=render_mode)
