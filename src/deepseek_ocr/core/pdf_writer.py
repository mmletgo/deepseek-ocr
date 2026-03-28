# -*- coding: utf-8 -*-
"""
Business Logic:
    生成双层PDF：底层是扫描图像(保持原始外观)，上层是透明文字层(支持搜索和复制)。
    支持两种模式：
    - dual_layer: 透明文字层(render_mode=3)，保持原始扫描外观
    - rewrite: 白色遮盖文字区域 + 重新渲染，图表/图片保持原始扫描
      - 纯文本 → 矢量字体重绘(render_mode=0)
      - equation/formula 块 → matplotlib 渲染 LaTeX 公式为 PNG 嵌入
      - 含内联 LaTeX(\(...\), \[...\])的文本 → matplotlib 混合渲染为 PNG 嵌入
      - 所有 matplotlib 渲染块同时写入不可见搜索层(render_mode=3)

Code Logic:
    使用PyMuPDF创建新PDF文档，逐页插入:
    1. 底层：原始扫描图像(overlay=False)
    2. rewrite模式下：白色矩形遮盖非 image/table 区域
    3. 上层：三路径渲染(公式图片/内联LaTeX图片/矢量文字)
    坐标从归一化(0-999)转换为PDF坐标(pt)。
    页面渲染通过 ProcessPoolExecutor 多进程并行（每进程独立 GIL，真正多核）。
    LaTeX 渲染使用 matplotlib mathtext 引擎(Agg后端)，支持子进程安全调用。
"""

import concurrent.futures
import functools
import multiprocessing
import os
import subprocess
import pymupdf
from pathlib import Path

from deepseek_ocr.core.pdf_reader import PageImage
from deepseek_ocr.core.output_parser import ParsedPage
from deepseek_ocr.utils.logger import logger

_KEEP_ORIGINAL_LABELS: frozenset[str] = frozenset({"image", "table"})


@functools.lru_cache(maxsize=1)
def _find_cjk_font_path() -> str | None:
    """查找系统 CJK 字体文件路径，用于 matplotlib 渲染含 CJK 字符的文本。"""
    # 优先通过 fontconfig 查找
    try:
        result: subprocess.CompletedProcess[str] = subprocess.run(
            ["fc-match", "-f", "%{file}", "Noto Sans CJK SC"],
            capture_output=True, text=True, timeout=5,
        )
        path: str = result.stdout.strip()
        if path and os.path.exists(path):
            return path
    except Exception:
        pass
    # 回退到常见路径
    for p in (
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
        "/System/Library/Fonts/PingFang.ttc",
    ):
        if os.path.exists(p):
            return p
    return None


def _has_cjk_chars(text: str) -> bool:
    """检测文本中是否包含 CJK 字符。"""
    for c in text:
        cp: int = ord(c)
        if (
            0x4E00 <= cp <= 0x9FFF
            or 0x3400 <= cp <= 0x4DBF
            or 0x3000 <= cp <= 0x303F
            or 0xFF00 <= cp <= 0xFFEF
            or 0x3040 <= cp <= 0x30FF
            or 0xAC00 <= cp <= 0xD7AF
        ):
            return True
    return False


def _wrap_line(line: str, font: object, fontsize: float, max_width: float) -> list[str]:
    """将单行文本按 bbox 宽度折行（按单词边界），返回多个子行。"""
    if not line.strip():
        return [line]
    if font.text_length(line, fontsize=fontsize) <= max_width:  # type: ignore[union-attr]
        return [line]
    words: list[str] = line.split()
    wrapped: list[str] = []
    current: str = ""
    for word in words:
        test: str = (current + " " + word).strip()
        if font.text_length(test, fontsize=fontsize) <= max_width:  # type: ignore[union-attr]
            current = test
        else:
            if current:
                wrapped.append(current)
            current = word
    if current:
        wrapped.append(current)
    return wrapped if wrapped else [line]


def _contains_latex(text: str) -> bool:
    """检测文本中是否包含 LaTeX 数学公式标记。"""
    return r'\(' in text or r'\)' in text or r'\[' in text or r'\]' in text


import re as _re

_MD_HEADING_RE = _re.compile(r'^#{1,6}\s+')


def _clean_markdown(text: str) -> str:
    """清理 Markdown 格式标记，返回适合矢量文字渲染的纯文本。"""
    lines: list[str] = text.split('\n')
    cleaned: list[str] = []
    for line in lines:
        # 去掉 Markdown 标题标记 (####)
        line = _MD_HEADING_RE.sub('', line)
        cleaned.append(line)
    return '\n'.join(cleaned)


def _strip_latex(text: str) -> str:
    """去掉 LaTeX 分隔符，返回可读纯文本（matplotlib 回退用）。"""
    result: str = text
    result = result.replace(r'\[', '').replace(r'\]', '')
    result = result.replace(r'\(', '').replace(r'\)', '')
    return result


_ARRAY_ENV_RE = _re.compile(
    r'\\left\s*[\[\(]?\s*\\begin\{array\}\{[^}]*\}(.*?)\\end\{array\}\s*\\right\s*[\]\).]?'
    r'|\\begin\{array\}\{[^}]*\}(.*?)\\end\{array\}',
    _re.DOTALL,
)
_CASES_ENV_RE = _re.compile(
    r'\\left\s*[\\\{]?\s*\\begin\{cases\}(.*?)\\end\{cases\}\s*\\right\s*[\\\}.]?'
    r'|\\begin\{cases\}(.*?)\\end\{cases\}',
    _re.DOTALL,
)


def _sanitize_latex(text: str) -> str:
    """预处理 LaTeX，替换 mathtext 不支持的命令。"""
    s: str = text
    # \left[\begin{array}{cc}...\end{array}\right] → 保留内容
    s = _ARRAY_ENV_RE.sub(lambda m: m.group(1) or m.group(2) or '', s)
    s = _CASES_ENV_RE.sub(lambda m: m.group(1) or m.group(2) or '', s)
    # 清理遗留的 \begin{array/bmatrix/cases}
    s = _re.sub(r'\\begin\{(?:array|bmatrix|pmatrix|vmatrix)\}\{[^}]*\}', '', s)
    s = _re.sub(r'\\begin\{(?:array|bmatrix|pmatrix|vmatrix|cases)\}', '', s)
    s = _re.sub(r'\\end\{(?:array|bmatrix|pmatrix|vmatrix|cases)\}', '', s)
    # 清理 array/matrix 残留: & → 空格, \\ → 空格
    s = s.replace('&', ' ')
    s = s.replace(r'\\', ' ')
    # \Big \bigg \Bigg → 移除
    s = _re.sub(r'\\[Bb]ig{1,2}[lr]?\b', '', s)
    # \left\{ → \{, \right. → 移除, \left\ → 移除
    s = _re.sub(r'\\left\s*\\{', r'\\{', s)
    s = _re.sub(r'\\left\s*\\(?=[^a-zA-Z{])', '', s)
    s = _re.sub(r'\\right\s*\\.?', '', s)
    # \operatorname * → \operatorname (带或不带空格)
    s = _re.sub(r'\\operatorname\s*\*', r'\\operatorname', s)
    # \text{} → \mathrm{}
    s = s.replace(r'\text{', r'\mathrm{')
    # \mathbf 无花括号 → 移除
    s = _re.sub(r'\\mathbf\s*(?=\s|=|[^{])', '', s)
    # \scriptstyle \displaystyle \textstyle → 移除
    for cmd in (r'\scriptstyle', r'\displaystyle', r'\textstyle'):
        s = s.replace(cmd, '')
    # \mod → \bmod
    s = s.replace(r'\mod', r'\bmod')
    # \hfill → 空格
    s = s.replace(r'\hfill', ' ')
    # 空公式块
    s = s.replace(r'\[\]', '')
    # 自动补全未闭合的 {} (OCR 截断修复)
    open_count: int = s.count('{') - s.count('}')
    if open_count > 0:
        s += '}' * open_count
    # 清理多余空格
    s = _re.sub(r'\s+', ' ', s)
    return s


def _escape_literal_dollars(text: str) -> str:
    """转义文本中非 LaTeX 分隔符的 $ 字符（如货币符号 $100）。"""
    # 找到不在 \(...\) 或 \[...\] 内部的孤立 $ 字符
    # 策略：先标记所有 \( \) \[ \] 的位置，然后转义其余 $
    result: list[str] = []
    i: int = 0
    while i < len(text):
        if text[i:i+2] in (r'\(', r'\)', r'\[', r'\]'):
            result.append(text[i:i+2])
            i += 2
        elif text[i] == '$':
            result.append(r'\$')
            i += 1
        else:
            result.append(text[i])
            i += 1
    return ''.join(result)


def _render_latex_image(latex: str, width_pt: float, height_pt: float, dpi: int = 200) -> bytes:
    """渲染 LaTeX 公式为 PNG 图片（用于 equation/formula 块）。"""
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from io import BytesIO

    # 预处理 + 转换分隔符
    text: str = _sanitize_latex(latex.strip())
    # 先转义原有的 $ 货币符号，避免与 LaTeX 分隔符冲突
    text = _escape_literal_dollars(text)
    text = text.replace(r'\[', '$').replace(r'\]', '$')
    text = text.replace(r'\(', '$').replace(r'\)', '$')
    if '$' not in text or text.replace('$', '').strip() == '':
        raise ValueError("Empty LaTeX after sanitization")

    fig_w: float = max(width_pt / 72.0, 0.5)
    fig_h: float = max(height_pt / 72.0, 0.3)
    fig = plt.figure(figsize=(fig_w, fig_h))
    try:
        fig.text(0.5, 0.5, text, fontsize=11, ha='center', va='center',
                 math_fontfamily='cm')
        buf: BytesIO = BytesIO()
        fig.savefig(buf, format='png', dpi=dpi, facecolor='white',
                    bbox_inches='tight', pad_inches=0.05)
        return buf.getvalue()
    finally:
        plt.close(fig)


def _render_text_image(text: str, label: str, width_pt: float, height_pt: float, dpi: int = 200) -> bytes:
    """渲染含内联 LaTeX 的文本块为 PNG 图片。"""
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from io import BytesIO

    # 预处理 + 将 LaTeX 分隔符转为 matplotlib 格式
    converted: str = _sanitize_latex(text)
    # 先转义原有的 $ 货币符号，避免与 LaTeX 分隔符冲突
    converted = _escape_literal_dollars(converted)
    converted = converted.replace(r'\(', '$').replace(r'\)', '$')
    converted = converted.replace(r'\[', '$').replace(r'\]', '$')

    fig_w: float = max(width_pt / 72.0, 0.5)
    fig_h: float = max(height_pt / 72.0, 0.3)
    fig = plt.figure(figsize=(fig_w, fig_h))

    is_title: bool = label in ("title", "sub_title")
    fontsize: int = 14 if is_title else 10
    fontweight: str = 'bold' if is_title else 'normal'

    # CJK 字体支持：检测文本是否包含 CJK 字符
    text_kwargs: dict[str, object] = {
        "va": "top", "ha": "left", "wrap": True, "math_fontfamily": "cm",
    }
    if _has_cjk_chars(text):
        cjk_path: str | None = _find_cjk_font_path()
        if cjk_path:
            from matplotlib.font_manager import FontProperties
            text_kwargs["fontproperties"] = FontProperties(
                fname=cjk_path, size=fontsize, weight=fontweight,
            )
    if "fontproperties" not in text_kwargs:
        text_kwargs["fontsize"] = fontsize
        text_kwargs["fontweight"] = fontweight

    try:
        fig.text(0.02, 0.98, converted, **text_kwargs)  # type: ignore[arg-type]
        buf: BytesIO = BytesIO()
        fig.savefig(buf, format='png', dpi=dpi, facecolor='white',
                    bbox_inches='tight', pad_inches=0.05)
        return buf.getvalue()
    finally:
        plt.close(fig)


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

        # rewrite 模式：按块决定渲染方式（不预先全部白色遮盖）
        if mode == "rewrite":
            tw_visible = _fitz.TextWriter(page.rect)
            tw_search = _fitz.TextWriter(page.rect)

            for block in parsed.blocks:
                if not block.text.strip():
                    continue
                if block.label in _KEEP_ORIGINAL_LABELS:
                    continue
                pdf_x1: float = block.bbox[0] / 999.0 * page.rect.width
                pdf_y1: float = block.bbox[1] / 999.0 * page.rect.height
                pdf_x2: float = block.bbox[2] / 999.0 * page.rect.width
                pdf_y2: float = block.bbox[3] / 999.0 * page.rect.height
                if pdf_x2 <= pdf_x1 or pdf_y2 <= pdf_y1:
                    continue
                bbox_width: float = pdf_x2 - pdf_x1
                bbox_height: float = pdf_y2 - pdf_y1
                block_rect = _fitz.Rect(pdf_x1, pdf_y1, pdf_x2, pdf_y2)

                if block.label in ("equation", "formula"):
                    # 独立公式 → matplotlib 渲染，失败则保留原始扫描
                    try:
                        png_bytes: bytes = _render_latex_image(block.text, bbox_width, bbox_height)
                        page.draw_rect(block_rect, color=None, fill=(1, 1, 1), overlay=True)
                        page.insert_image(block_rect, stream=png_bytes, overlay=True)
                    except Exception:
                        pass  # 不遮盖，保留原始扫描
                    try:
                        tw_search.append((pdf_x1, pdf_y1 + 10), block.text[:200],
                                         font=font, fontsize=3)
                    except Exception:
                        pass
                elif _contains_latex(block.text):
                    # 含内联 LaTeX → matplotlib 渲染，失败则白色遮盖 + OCR 原文回退
                    try:
                        png_bytes = _render_text_image(block.text, block.label,
                                                       bbox_width, bbox_height)
                        page.draw_rect(block_rect, color=None, fill=(1, 1, 1), overlay=True)
                        page.insert_image(block_rect, stream=png_bytes, overlay=True)
                        try:
                            tw_search.append((pdf_x1, pdf_y1 + 10), block.text[:200],
                                             font=font, fontsize=3)
                        except Exception:
                            pass
                    except Exception:
                        # 回退：白色遮盖 + OCR 原始文本（不去 LaTeX 标记）
                        page.draw_rect(block_rect, color=None, fill=(1, 1, 1), overlay=True)
                        fallback_lines: list[str] = block.text.strip().split('\n')
                        fs: float = max(min(bbox_height / max(len(fallback_lines), 1) * 0.75, 14.0), 3.0)
                        wrapped_fb: list[str] = []
                        for ln in fallback_lines:
                            wrapped_fb.extend(_wrap_line(ln, font, fs, bbox_width))
                        for _ in range(20):
                            if len(wrapped_fb) * fs * 1.2 <= bbox_height or fs <= 3.0:
                                break
                            fs *= 0.9
                            wrapped_fb = []
                            for ln in fallback_lines:
                                wrapped_fb.extend(_wrap_line(ln, font, fs, bbox_width))
                        fs = max(fs, 3.0)
                        y_fb: float = pdf_y1 + fs * 0.85
                        for ln in wrapped_fb:
                            if y_fb > pdf_y2:
                                break
                            if ln.strip():
                                try:
                                    tw_visible.append((pdf_x1, y_fb), ln, font=font, fontsize=fs)
                                except Exception:
                                    pass
                            y_fb += fs * 1.2
                else:
                    # 纯文本 → 白色遮盖 + 矢量文字 + 自动换行
                    page.draw_rect(block_rect, color=None, fill=(1, 1, 1), overlay=True)
                    clean_text: str = _clean_markdown(block.text.strip())
                    lines: list[str] = clean_text.split('\n')
                    fontsize_txt: float = max(min(bbox_height / max(len(lines), 1) * 0.75, 14.0), 3.0)
                    wrapped: list[str] = []
                    for line in lines:
                        wrapped.extend(_wrap_line(line, font, fontsize_txt, bbox_width))
                    for _ in range(20):
                        if len(wrapped) * fontsize_txt * 1.2 <= bbox_height or fontsize_txt <= 3.0:
                            break
                        fontsize_txt *= 0.9
                        wrapped = []
                        for line in lines:
                            wrapped.extend(_wrap_line(line, font, fontsize_txt, bbox_width))
                    fontsize_txt = max(fontsize_txt, 3.0)
                    line_height: float = fontsize_txt * 1.2
                    y: float = pdf_y1 + fontsize_txt * 0.85
                    for line in wrapped:
                        if y > pdf_y2:
                            break
                        if line.strip():
                            try:
                                tw_visible.append((pdf_x1, y), line,
                                                  font=font, fontsize=fontsize_txt)
                            except Exception:
                                pass
                        y += line_height

            tw_visible.write_text(page, render_mode=0)
            tw_search.write_text(page, render_mode=3)
        else:
            # dual_layer 模式：完全不变
            tw = _fitz.TextWriter(page.rect)
            for block in parsed.blocks:
                if not block.text.strip():
                    continue
                pdf_x1 = block.bbox[0] / 999.0 * page.rect.width
                pdf_y1 = block.bbox[1] / 999.0 * page.rect.height
                pdf_x2 = block.bbox[2] / 999.0 * page.rect.width
                pdf_y2 = block.bbox[3] / 999.0 * page.rect.height
                if pdf_x2 <= pdf_x1 or pdf_y2 <= pdf_y1:
                    continue
                lines = block.text.strip().split('\n')
                bbox_height = pdf_y2 - pdf_y1
                fontsize: float = max(min(bbox_height / max(len(lines), 1) * 0.75, 36.0), 3.0)
                line_height = fontsize * 1.2
                y = pdf_y1 + fontsize * 0.85
                for line in lines:
                    if y > pdf_y2:
                        break
                    if line.strip():
                        try:
                            tw.append((pdf_x1, y), line, font=font, fontsize=fontsize)
                        except Exception:
                            pass
                    y += line_height
            tw.write_text(page, render_mode=3)
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

        # 3+4. rewrite模式：按块决定渲染方式
        if mode == "rewrite":
            tw_visible: pymupdf.TextWriter = pymupdf.TextWriter(page.rect)
            tw_search: pymupdf.TextWriter = pymupdf.TextWriter(page.rect)

            for block in parsed.blocks:
                if not block.text.strip():
                    continue
                if block.label in _KEEP_ORIGINAL_LABELS:
                    continue

                pdf_x1: float = block.bbox[0] / 999.0 * page.rect.width
                pdf_y1: float = block.bbox[1] / 999.0 * page.rect.height
                pdf_x2: float = block.bbox[2] / 999.0 * page.rect.width
                pdf_y2: float = block.bbox[3] / 999.0 * page.rect.height

                if pdf_x2 <= pdf_x1 or pdf_y2 <= pdf_y1:
                    continue
                bbox_width: float = pdf_x2 - pdf_x1
                bbox_height: float = pdf_y2 - pdf_y1
                block_rect: pymupdf.Rect = pymupdf.Rect(pdf_x1, pdf_y1, pdf_x2, pdf_y2)

                if block.label in ("equation", "formula"):
                    # 独立公式：成功→遮盖+渲染，失败→保留原始扫描
                    try:
                        png_bytes: bytes = _render_latex_image(block.text, bbox_width, bbox_height)
                        page.draw_rect(block_rect, color=None, fill=(1, 1, 1), overlay=True)
                        page.insert_image(block_rect, stream=png_bytes, overlay=True)
                    except Exception:
                        pass  # 保留原始扫描
                    try:
                        tw_search.append((pdf_x1, pdf_y1 + 10), block.text[:200],
                                         font=self.font, fontsize=3)
                    except Exception:
                        pass
                elif _contains_latex(block.text):
                    # 含内联 LaTeX：成功→遮盖+渲染，失败→遮盖+OCR原文回退
                    try:
                        png_bytes = _render_text_image(block.text, block.label,
                                                       bbox_width, bbox_height)
                        page.draw_rect(block_rect, color=None, fill=(1, 1, 1), overlay=True)
                        page.insert_image(block_rect, stream=png_bytes, overlay=True)
                        try:
                            tw_search.append((pdf_x1, pdf_y1 + 10), block.text[:200],
                                             font=self.font, fontsize=3)
                        except Exception:
                            pass
                    except Exception:
                        page.draw_rect(block_rect, color=None, fill=(1, 1, 1), overlay=True)
                        fb_lines: list[str] = block.text.strip().split('\n')
                        fs: float = max(min(bbox_height / max(len(fb_lines), 1) * 0.75, 14.0), 3.0)
                        wr: list[str] = []
                        for ln in fb_lines:
                            wr.extend(_wrap_line(ln, self.font, fs, bbox_width))
                        for _ in range(20):
                            if len(wr) * fs * 1.2 <= bbox_height or fs <= 3.0:
                                break
                            fs *= 0.9
                            wr = []
                            for ln in fb_lines:
                                wr.extend(_wrap_line(ln, self.font, fs, bbox_width))
                        fs = max(fs, 3.0)
                        y_fb: float = pdf_y1 + fs * 0.85
                        for ln in wr:
                            if y_fb > pdf_y2:
                                break
                            if ln.strip():
                                try:
                                    tw_visible.append((pdf_x1, y_fb), ln, font=self.font, fontsize=fs)
                                except Exception:
                                    pass
                            y_fb += fs * 1.2
                else:
                    # 纯文本 → 白色遮盖 + 矢量文字 + 自动换行
                    page.draw_rect(block_rect, color=None, fill=(1, 1, 1), overlay=True)
                    clean_text: str = _clean_markdown(block.text.strip())
                    lines: list[str] = clean_text.split('\n')
                    fontsize_txt: float = max(min(bbox_height / max(len(lines), 1) * 0.75, 14.0), 3.0)
                    wrapped: list[str] = []
                    for line in lines:
                        wrapped.extend(_wrap_line(line, self.font, fontsize_txt, bbox_width))
                    for _ in range(20):
                        if len(wrapped) * fontsize_txt * 1.2 <= bbox_height or fontsize_txt <= 3.0:
                            break
                        fontsize_txt *= 0.9
                        wrapped = []
                        for line in lines:
                            wrapped.extend(_wrap_line(line, self.font, fontsize_txt, bbox_width))
                    fontsize_txt = max(fontsize_txt, 3.0)
                    line_height: float = fontsize_txt * 1.2
                    y: float = pdf_y1 + fontsize_txt * 0.85
                    for line in wrapped:
                        if y > pdf_y2:
                            break
                        if line.strip():
                            try:
                                tw_visible.append((pdf_x1, y), line,
                                                  font=self.font, fontsize=fontsize_txt)
                            except Exception:
                                pass
                        y += line_height

            tw_visible.write_text(page, render_mode=0)
            tw_search.write_text(page, render_mode=3)
        else:
            # dual_layer 模式：完全不变
            tw: pymupdf.TextWriter = pymupdf.TextWriter(page.rect)

            for block in parsed.blocks:
                if not block.text.strip():
                    continue

                pdf_x1 = block.bbox[0] / 999.0 * page.rect.width
                pdf_y1 = block.bbox[1] / 999.0 * page.rect.height
                pdf_x2 = block.bbox[2] / 999.0 * page.rect.width
                pdf_y2 = block.bbox[3] / 999.0 * page.rect.height

                rect: pymupdf.Rect = pymupdf.Rect(pdf_x1, pdf_y1, pdf_x2, pdf_y2)

                if rect.width <= 0 or rect.height <= 0:
                    logger.debug(f"页 {page_img.page_index}: 跳过无效区域 {block.bbox}")
                    continue

                lines = block.text.strip().split('\n')
                bbox_height = pdf_y2 - pdf_y1
                fontsize: float = max(min(bbox_height / max(len(lines), 1) * 0.75, 36.0), 3.0)
                line_height = fontsize * 1.2
                y = pdf_y1 + fontsize * 0.85
                for line in lines:
                    if y > pdf_y2:
                        break
                    if line.strip():
                        try:
                            tw.append((pdf_x1, y), line, font=self.font, fontsize=fontsize)
                        except Exception:
                            pass
                    y += line_height

            tw.write_text(page, render_mode=3)
