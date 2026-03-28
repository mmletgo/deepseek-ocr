# -*- coding: utf-8 -*-
"""
Business Logic:
    提供 deepseek-ocr 的命令行入口，让用户通过终端完成 PDF OCR 转换、
    环境健康检查、以及启动 Web 服务三项核心操作。

Code Logic:
    基于 Click 框架构建命令组 (cli)，下辖 convert / translate / check / serve 四个子命令，
    使用 Rich 美化控制台输出（进度条、表格、面板）。
    CLI 参数默认值为 None，非 None 时覆盖 .env / 环境变量配置。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import List

import click
from rich.console import Console
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)
from rich.table import Table

from deepseek_ocr.config import AppConfig, PDFOutputMode
from deepseek_ocr.core.pipeline import ConversionPipeline, ConversionResult
from deepseek_ocr.core.ocr_engine import OCREngine
from deepseek_ocr.utils.logger import logger

console = Console()


def _collect_pdf_files(input_path: Path) -> List[Path]:
    """
    Business Logic:
        用户可能传入单个 PDF 文件或一个目录，需要统一收集待处理的 PDF 列表。

    Code Logic:
        如果 input_path 是文件则直接返回单元素列表；
        如果是目录则递归搜索所有 .pdf 文件并排序返回。
        路径不存在或既非文件也非目录时抛出 click.BadParameter。
    """
    if not input_path.exists():
        raise click.BadParameter(f"路径不存在: {input_path}")

    if input_path.is_file():
        if input_path.suffix.lower() != ".pdf":
            raise click.BadParameter(f"文件不是PDF格式: {input_path}")
        return [input_path]

    if input_path.is_dir():
        pdfs = sorted(input_path.rglob("*.pdf"))
        if not pdfs:
            raise click.BadParameter(f"目录中没有找到PDF文件: {input_path}")
        return pdfs

    raise click.BadParameter(f"无法识别的路径类型: {input_path}")


@click.group()
@click.version_option(package_name="deepseek-ocr")
def cli() -> None:
    """
    Business Logic:
        deepseek-ocr 命令行工具的根命令组，用于将扫描版 PDF 转换为
        可搜索 PDF 和 Markdown。

    Code Logic:
        使用 click.group() 注册为顶级命令组，子命令通过装饰器自动挂载。
    """
    pass


# ---------------------------------------------------------------------------
# 公共转换逻辑（供 convert 和 translate 子命令复用）
# ---------------------------------------------------------------------------
def _run_convert(
    input_path: str,
    output_dir: str | None,
    dpi: int | None,
    no_pdf: bool,
    no_markdown: bool,
    model: str | None,
    ollama_host: str | None,
    pdf_mode: str | None,
    translate: bool,
    source_lang: str | None,
    target_lang: str | None,
    translation_model: str | None,
    translation_base_url: str | None,
    translation_api_key: str | None,
) -> None:
    """
    Business Logic:
        核心转换逻辑——接收用户指定的 PDF 文件或目录，逐个调用 ConversionPipeline
        完成 OCR 识别，并将结果（可搜索 PDF / Markdown）写入输出目录。
        可选启用翻译功能，生成翻译PDF和双语对照PDF。
        支持单文件和目录批量处理，使用 Rich 进度条展示实时进度。

    Code Logic:
        1. 从 .env 和默认值构建基础 AppConfig
        2. 用非 None 的 CLI 参数覆盖配置
        3. 逐文件创建 ConversionPipeline，通过 progress_callback 驱动 Rich 进度条
        4. 汇总所有结果，打印处理摘要（成功/失败数、总页数、总耗时）
    """
    # 从 .env 和默认值构建基础配置
    config = AppConfig()

    # CLI 参数覆盖（非 None 时生效）
    if ollama_host is not None:
        config.ollama.host = ollama_host
    if model is not None:
        config.ollama.model = model
    if dpi is not None:
        config.pdf.dpi = dpi
    if pdf_mode is not None:
        config.pdf.output_mode = PDFOutputMode(pdf_mode)
    if output_dir is not None:
        config.output_dir = str(Path(output_dir).resolve())

    resolved_input: Path = Path(input_path).resolve()
    resolved_output: Path = Path(config.output_dir).resolve()
    resolved_output.mkdir(parents=True, exist_ok=True)

    pdf_files: List[Path] = _collect_pdf_files(resolved_input)

    generate_pdf: bool = not no_pdf
    generate_markdown: bool = not no_markdown

    if not generate_pdf and not generate_markdown:
        console.print("[bold red]错误: --no-pdf 和 --no-markdown 不能同时使用，至少需要一种输出格式[/bold red]")
        raise SystemExit(1)

    # 构建翻译配置
    if translate:
        if translation_base_url is not None:
            config.translation.base_url = translation_base_url
        if translation_api_key is not None:
            config.translation.api_key = translation_api_key
        if translation_model is not None:
            config.translation.model = translation_model
        if not config.translation.api_key:
            console.print(
                "[bold red]Error:[/bold red] --translate requires API key. "
                "Set --translation-api-key or TRANSLATION_API_KEY env var."
            )
            raise SystemExit(1)

    effective_source_lang: str = source_lang or "English"
    effective_target_lang: str = target_lang or "Simplified Chinese"

    panel_lines: str = (
        f"[bold]输入:[/bold] {resolved_input}\n"
        f"[bold]输出:[/bold] {resolved_output}\n"
        f"[bold]文件数:[/bold] {len(pdf_files)}    "
        f"[bold]DPI:[/bold] {config.pdf.dpi}    "
        f"[bold]模型:[/bold] {config.ollama.model}\n"
        f"[bold]生成PDF:[/bold] {'是' if generate_pdf else '否'}    "
        f"[bold]生成Markdown:[/bold] {'是' if generate_markdown else '否'}    "
        f"[bold]PDF模式:[/bold] {config.pdf.output_mode.value}"
    )
    if translate:
        panel_lines += (
            f"\n[bold]翻译:[/bold] 是    "
            f"[bold]源语言:[/bold] {effective_source_lang}    "
            f"[bold]目标语言:[/bold] {effective_target_lang}"
        )

    console.print(
        Panel(
            panel_lines,
            title="[bold cyan]DeepSeek-OCR 转换任务[/bold cyan]",
            border_style="cyan",
        )
    )

    results: List[ConversionResult] = []
    total_start: float = time.time()

    for file_idx, pdf_file in enumerate(pdf_files, start=1):
        console.print(f"\n[bold green][{file_idx}/{len(pdf_files)}][/bold green] 处理: {pdf_file.name}")

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            MofNCompleteColumn(),
            TimeElapsedColumn(),
            TimeRemainingColumn(),
            console=console,
            transient=False,
        ) as progress:
            task_id = progress.add_task(f"OCR {pdf_file.stem}", total=None)

            def make_progress_callback(prog: Progress, tid: int):
                """
                Business Logic:
                    为每个文件的 ConversionPipeline 创建独立的进度回调闭包，
                    避免多个文件共享同一个闭包变量导致进度条错乱。

                Code Logic:
                    返回一个闭包函数，该函数接收 (current_page, total_pages, status_msg)，
                    并更新对应 Rich Progress task 的 total 和 completed 值。
                """
                def _callback(current_page: int, total_pages: int, status_msg: str) -> None:
                    prog.update(tid, total=total_pages, completed=current_page, description=status_msg)
                return _callback

            callback = make_progress_callback(progress, task_id)
            pipeline = ConversionPipeline(config=config, progress_callback=callback)

            try:
                result: ConversionResult = pipeline.convert(
                    input_pdf=pdf_file,
                    output_dir=resolved_output,
                    generate_pdf=generate_pdf,
                    generate_markdown=generate_markdown,
                    translate=translate,
                    source_lang=effective_source_lang,
                    target_lang=effective_target_lang,
                )
                results.append(result)
                if result.success:
                    console.print(f"  [green]完成[/green] ({result.page_count} 页, {result.elapsed_seconds:.1f}s)")
                    if result.output_translated_pdf:
                        console.print(f"  [cyan]翻译PDF:[/cyan] {result.output_translated_pdf}")
                    if result.output_bilingual_pdf:
                        console.print(f"  [cyan]双语PDF:[/cyan] {result.output_bilingual_pdf}")
                    if result.translation_error:
                        console.print(f"  [yellow]翻译警告:[/yellow] {result.translation_error}")
                else:
                    console.print(f"  [red]失败: {result.error_msg}[/red]")
            except Exception as exc:
                logger.error(f"处理 {pdf_file.name} 时发生异常: {exc}")
                results.append(
                    ConversionResult(
                        source_pdf=pdf_file,
                        output_pdf=None,
                        output_markdown=None,
                        page_count=0,
                        success=False,
                        error_msg=str(exc),
                        elapsed_seconds=0.0,
                    )
                )

    total_elapsed: float = time.time() - total_start
    success_count: int = sum(1 for r in results if r.success)
    fail_count: int = len(results) - success_count
    total_pages: int = sum(r.page_count for r in results)

    summary_table = Table(title="转换摘要", border_style="cyan")
    summary_table.add_column("指标", style="bold")
    summary_table.add_column("值", justify="right")
    summary_table.add_row("处理文件数", str(len(results)))
    summary_table.add_row("成功", f"[green]{success_count}[/green]")
    summary_table.add_row("失败", f"[red]{fail_count}[/red]" if fail_count > 0 else "0")
    summary_table.add_row("总页数", str(total_pages))
    summary_table.add_row("总耗时", f"{total_elapsed:.1f}s")

    console.print()
    console.print(summary_table)


# ---------------------------------------------------------------------------
# convert 命令
# ---------------------------------------------------------------------------
@cli.command()
@click.argument("input_path", type=click.Path(exists=True))
@click.option("--output", "-o", "output_dir", default=None, help="输出目录 (env: OUTPUT_DIR, 默认 ./output)")
@click.option("--dpi", default=None, type=int, help="PDF渲染DPI (env: PDF_DPI, 默认200)")
@click.option("--no-pdf", "no_pdf", is_flag=True, default=False, help="不生成双层PDF")
@click.option("--no-markdown", "no_markdown", is_flag=True, default=False, help="不生成Markdown")
@click.option("--model", default=None, help="Ollama模型名称 (env: OLLAMA_MODEL, 默认 deepseek-ocr)")
@click.option("--ollama-host", default=None, help="Ollama服务地址 (env: OLLAMA_HOST, 默认 http://localhost:11434)")
@click.option("--pdf-mode", "pdf_mode", type=click.Choice(["dual_layer", "rewrite"], case_sensitive=False), default=None, help="PDF输出模式: dual_layer(双层) / rewrite(重绘) (env: PDF_OUTPUT_MODE)")
@click.option("--translate", is_flag=True, default=False, help="启用LLM翻译")
@click.option("--source-lang", default=None, help="源语言 (默认: English)")
@click.option("--target-lang", default=None, help="目标语言 (默认: Simplified Chinese)")
@click.option("--translation-model", default=None, help="翻译LLM模型名 (env: TRANSLATION_MODEL)")
@click.option("--translation-base-url", default=None, help="翻译API base_url (env: TRANSLATION_BASE_URL)")
@click.option("--translation-api-key", default=None, help="翻译API key (env: TRANSLATION_API_KEY)")
def convert(
    input_path: str,
    output_dir: str | None,
    dpi: int | None,
    no_pdf: bool,
    no_markdown: bool,
    model: str | None,
    ollama_host: str | None,
    pdf_mode: str | None,
    translate: bool,
    source_lang: str | None,
    target_lang: str | None,
    translation_model: str | None,
    translation_base_url: str | None,
    translation_api_key: str | None,
) -> None:
    """将扫描PDF转换为可搜索PDF和Markdown，可选启用翻译。"""
    _run_convert(
        input_path=input_path,
        output_dir=output_dir,
        dpi=dpi,
        no_pdf=no_pdf,
        no_markdown=no_markdown,
        model=model,
        ollama_host=ollama_host,
        pdf_mode=pdf_mode,
        translate=translate,
        source_lang=source_lang,
        target_lang=target_lang,
        translation_model=translation_model,
        translation_base_url=translation_base_url,
        translation_api_key=translation_api_key,
    )


# ---------------------------------------------------------------------------
# translate 命令
# ---------------------------------------------------------------------------
@cli.command()
@click.argument("input_path", type=click.Path(exists=True))
@click.option("--output", "-o", "output_dir", default=None, help="输出目录 (env: OUTPUT_DIR, 默认 ./output)")
@click.option("--source-lang", default=None, help="源语言 (默认: English)")
@click.option("--target-lang", default=None, help="目标语言 (默认: Simplified Chinese)")
@click.option("--dpi", default=None, type=int, help="PDF渲染DPI (env: PDF_DPI, 默认200)")
@click.option("--model", default=None, help="OCR模型名 (env: OLLAMA_MODEL, 默认 deepseek-ocr)")
@click.option("--ollama-host", default=None, help="Ollama服务地址 (env: OLLAMA_HOST, 默认 http://localhost:11434)")
@click.option("--translation-model", default=None, help="翻译LLM模型名 (env: TRANSLATION_MODEL)")
@click.option("--translation-base-url", default=None, help="翻译API base_url (env: TRANSLATION_BASE_URL)")
@click.option("--translation-api-key", default=None, help="翻译API key (env: TRANSLATION_API_KEY)")
def translate(
    input_path: str,
    output_dir: str | None,
    source_lang: str | None,
    target_lang: str | None,
    dpi: int | None,
    model: str | None,
    ollama_host: str | None,
    translation_model: str | None,
    translation_base_url: str | None,
    translation_api_key: str | None,
) -> None:
    """翻译扫描PDF：先OCR识别再翻译为目标语言。等效于 convert --translate。"""
    _run_convert(
        input_path=input_path,
        output_dir=output_dir,
        dpi=dpi,
        no_pdf=False,
        no_markdown=False,
        model=model,
        ollama_host=ollama_host,
        pdf_mode=None,
        translate=True,
        source_lang=source_lang,
        target_lang=target_lang,
        translation_model=translation_model,
        translation_base_url=translation_base_url,
        translation_api_key=translation_api_key,
    )


# ---------------------------------------------------------------------------
# check 命令
# ---------------------------------------------------------------------------
@cli.command()
@click.option("--ollama-host", default=None, help="Ollama服务地址 (env: OLLAMA_HOST, 默认 http://localhost:11434)")
@click.option("--model", default=None, help="Ollama模型名称 (env: OLLAMA_MODEL, 默认 deepseek-ocr)")
def check(ollama_host: str | None, model: str | None) -> None:
    """
    Business Logic:
        在正式转换前，让用户快速确认运行环境是否就绪——Ollama 服务是否
        可达、所需模型是否已拉取。

    Code Logic:
        1. 构建 AppConfig 获取 .env 默认值，然后用非 None 的 CLI 参数覆盖
        2. 实例化 OCREngine
        3. 调用 check_health() 判断服务状态
        4. 通过 ollama 库列出已有模型，检查目标模型是否存在
        5. 用 Rich Table 展示检查结果
    """
    config = AppConfig()
    if ollama_host is not None:
        config.ollama.host = ollama_host
    if model is not None:
        config.ollama.model = model

    effective_host: str = config.ollama.host
    effective_model: str = config.ollama.model

    engine = OCREngine(config.ollama)

    check_table = Table(title="环境检查", border_style="cyan")
    check_table.add_column("检查项", style="bold")
    check_table.add_column("状态", justify="center")
    check_table.add_column("详情")

    # 检查 Ollama 服务
    ollama_ok: bool = False
    try:
        ollama_ok = engine.check_health()
    except Exception as exc:
        logger.debug(f"Ollama 健康检查异常: {exc}")

    if ollama_ok:
        check_table.add_row("Ollama 服务", "[green]正常[/green]", f"地址: {effective_host}")
    else:
        check_table.add_row("Ollama 服务", "[red]不可用[/red]", f"无法连接 {effective_host}")

    # 检查模型是否已拉取
    model_ok: bool = False
    if ollama_ok:
        try:
            import ollama as ollama_lib
            client = ollama_lib.Client(host=effective_host)
            model_list = client.list()
            available_models: List[str] = [m.model for m in model_list.models]
            # 模型名可能带 :latest 后缀，做宽松匹配
            model_ok = any(
                m == effective_model or m.startswith(f"{effective_model}:") for m in available_models
            )
        except Exception as exc:
            logger.debug(f"模型列表获取异常: {exc}")

    if model_ok:
        check_table.add_row("OCR 模型", "[green]已就绪[/green]", f"模型: {effective_model}")
    elif ollama_ok:
        check_table.add_row(
            "OCR 模型",
            "[yellow]未找到[/yellow]",
            f"请运行: ollama pull {effective_model}",
        )
    else:
        check_table.add_row("OCR 模型", "[dim]跳过[/dim]", "Ollama 服务不可用，无法检查模型")

    console.print()
    console.print(check_table)

    if ollama_ok and model_ok:
        console.print("\n[bold green]所有检查通过，可以开始转换![/bold green]")
    else:
        console.print("\n[bold yellow]部分检查未通过，请根据上述提示修复后重试。[/bold yellow]")


# ---------------------------------------------------------------------------
# serve 命令
# ---------------------------------------------------------------------------
@cli.command()
@click.option("--host", default=None, help="绑定地址 (env: WEB_HOST, 默认 0.0.0.0)")
@click.option("--port", default=None, type=int, help="端口 (env: WEB_PORT, 默认 8080)")
def serve(host: str | None, port: int | None) -> None:
    """
    Business Logic:
        启动基于 FastAPI 的 Web 服务，让用户通过浏览器上传 PDF 并获取
        OCR 转换结果，适用于不习惯命令行的用户或需要远程访问的场景。

    Code Logic:
        导入 Web 模块中的 FastAPI app，使用 uvicorn.run() 启动服务。
        从 AppConfig 获取 .env 默认值，CLI 参数非 None 时覆盖。
    """
    import uvicorn
    from deepseek_ocr.web.app import create_app

    config = AppConfig()
    effective_host: str = host if host is not None else config.web.host
    effective_port: int = port if port is not None else config.web.port

    console.print(
        Panel(
            f"[bold]地址:[/bold] http://{effective_host}:{effective_port}\n"
            f"[bold]文档:[/bold] http://{effective_host}:{effective_port}/docs",
            title="[bold cyan]DeepSeek-OCR Web 服务[/bold cyan]",
            border_style="cyan",
        )
    )

    app = create_app()
    uvicorn.run(app, host=effective_host, port=effective_port, log_level="info")


if __name__ == "__main__":
    cli()
