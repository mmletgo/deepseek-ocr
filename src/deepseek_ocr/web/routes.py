"""
Business Logic:
    定义Web API的所有路由端点，包括首页、文件上传、进度推送、结果下载和健康检查，
    为用户提供完整的PDF OCR转换Web交互流程。

Code Logic:
    使用FastAPI APIRouter管理路由。上传后启动异步后台任务执行转换，
    通过SSE(Server-Sent Events)实时推送转换进度，支持结果文件下载。
    任务状态通过内存字典管理。

    并发控制：
    - _ocr_semaphore: Semaphore(1) 串行化GPU OCR步骤，避免显存溢出
    - _generating_semaphore: Semaphore(1) 串行化PDF生成步骤，避免PyMuPDF GIL争用

    OCR缓存：
    - 以PDF的MD5哈希为key，每页OCR结果持久化到 uploads/ocr_cache/{md5}/page_NNNN.json
    - 支持断点续传：已缓存的页直接跳过OCR
"""

from fastapi import APIRouter, UploadFile, File, Form, HTTPException, BackgroundTasks
from fastapi.responses import FileResponse, HTMLResponse
from sse_starlette.sse import EventSourceResponse
import asyncio
import uuid
import json
from pathlib import Path
from typing import Any, List

from deepseek_ocr.config import AppConfig, PDFOutputMode
from deepseek_ocr.utils.logger import logger

router = APIRouter()

# 内存中的任务管理字典，key为task_id，value为任务状态信息
tasks: dict[str, dict[str, Any]] = {}

# 全局配置
_config = AppConfig()

# 全局 OCR GPU 串行锁（同一时刻只允许一个任务使用GPU）
_ocr_semaphore: asyncio.Semaphore | None = None

# 全局 PDF 生成串行锁（避免PyMuPDF GIL争用）
_generating_semaphore: asyncio.Semaphore | None = None


def _get_ocr_semaphore() -> asyncio.Semaphore:
    """获取或创建 OCR 串行信号量（懒初始化，确保在事件循环中创建）"""
    global _ocr_semaphore
    if _ocr_semaphore is None:
        _ocr_semaphore = asyncio.Semaphore(1)
    return _ocr_semaphore


def _get_generating_semaphore() -> asyncio.Semaphore:
    """获取或创建 PDF 生成串行信号量（懒初始化，确保在事件循环中创建）"""
    global _generating_semaphore
    if _generating_semaphore is None:
        _generating_semaphore = asyncio.Semaphore(1)
    return _generating_semaphore


@router.get("/")
async def index() -> HTMLResponse:
    """
    Business Logic:
        用户访问根路径时展示Web界面，提供PDF上传和转换操作的入口。

    Code Logic:
        读取static/index.html文件内容并作为HTMLResponse返回。
    """
    html_path = Path(__file__).parent / "static" / "index.html"
    if not html_path.exists():
        raise HTTPException(status_code=404, detail="index.html not found")
    content: str = html_path.read_text(encoding="utf-8")
    return HTMLResponse(content=content)


@router.post("/api/upload")
async def upload_pdf(
    files: List[UploadFile] = File(...),
    pdf_mode: str = Form("dual_layer"),
) -> list[dict[str, str]]:
    """
    Business Logic:
        用户上传一个或多个PDF文件后，系统保存文件并为每个文件启动异步OCR任务，
        返回任务ID列表供前端独立追踪进度。

    Code Logic:
        对每个上传文件：
        1. 验证文件是否为PDF格式
        2. 生成唯一task_id (UUID)
        3. 将文件保存到 uploads/{task_id}/ 目录
        4. 初始化任务状态并启动后台转换任务
        5. 收集并返回所有 {task_id, filename}
    """
    # 验证pdf_mode
    if pdf_mode not in ("dual_layer", "rewrite"):
        raise HTTPException(status_code=400, detail=f"Invalid pdf_mode: {pdf_mode}")

    results: list[dict[str, str]] = []

    for file in files:
        # 验证文件类型
        if not file.filename or not file.filename.lower().endswith(".pdf"):
            raise HTTPException(status_code=400, detail="Only PDF files are accepted")

        # 检查content_type
        if file.content_type and file.content_type != "application/pdf":
            # 某些浏览器可能不设置content_type，所以仅在有值时校验
            if "pdf" not in file.content_type.lower():
                raise HTTPException(status_code=400, detail="Only PDF files are accepted")

        task_id: str = str(uuid.uuid4())

        # 创建上传目录
        upload_dir = Path(_config.web.upload_dir) / task_id
        upload_dir.mkdir(parents=True, exist_ok=True)

        # 保存上传文件
        file_path = upload_dir / file.filename
        file_content: bytes = await file.read()

        # 检查文件大小
        max_size: int = _config.web.max_upload_size_mb * 1024 * 1024
        if len(file_content) > max_size:
            raise HTTPException(
                status_code=413,
                detail=f"File too large. Maximum size: {_config.web.max_upload_size_mb}MB"
            )

        with open(file_path, "wb") as f:
            f.write(file_content)

        # 初始化任务状态
        tasks[task_id] = {
            "status": "queued",
            "phase": "queued",
            "current": 0,
            "total": 0,
            "message": "Waiting to start...",
            "done": False,
            "error": None,
            "input_file": str(file_path),
            "output_dir": str(upload_dir / "output"),
            "result_pdf": None,
            "result_markdown": None,
            "filename": file.filename,
            "pdf_md5": None,
            "pdf_mode": pdf_mode,
        }

        # 启动后台转换任务
        asyncio.create_task(_run_conversion(task_id))

        logger.info(f"Task {task_id} created for file: {file.filename}")

        results.append({"task_id": task_id, "filename": file.filename})

    return results


async def _run_conversion(task_id: str) -> None:
    """
    Business Logic:
        后台执行PDF OCR转换，实时更新任务进度供SSE推送。
        支持OCR结果持久化缓存和断点续传。

    Code Logic:
        展开转换步骤（不再使用pipeline.convert_async），以便精确控制并发：
        1. 读取PDF，渲染为图片
        2. 计算PDF MD5，初始化OCR缓存
        3. 等待OCR信号量，逐页OCR（缓存命中则跳过）
        4. 解析OCR结果
        5. 等待生成信号量，生成双层PDF
        6. 生成Markdown（在生成信号量外执行）
    """
    task: dict[str, Any] | None = tasks.get(task_id)
    if task is None:
        return

    task["status"] = "running"
    task["message"] = "Initializing..."

    try:
        import time
        from deepseek_ocr.core.pdf_reader import PDFReader, PageImage
        from deepseek_ocr.core.ocr_engine import OCREngine, OCRResult
        from deepseek_ocr.core.output_parser import OutputParser, ParsedPage
        from deepseek_ocr.core.pdf_writer import DualLayerPDFWriter
        from deepseek_ocr.core.markdown_writer import MarkdownWriter
        from deepseek_ocr.core.ocr_cache import OCRCache

        pdf_mode: str = task.get("pdf_mode", "dual_layer")
        config = AppConfig()
        loop = asyncio.get_event_loop()

        pdf_reader = PDFReader(
            dpi=config.pdf.dpi,
            max_dimension=config.pdf.max_dimension,
        )
        ocr_engine = OCREngine(config.ollama)
        parser = OutputParser()
        pdf_writer = DualLayerPDFWriter()
        markdown_writer = MarkdownWriter()

        input_file = Path(task["input_file"])
        output_dir = Path(task["output_dir"])
        output_dir.mkdir(parents=True, exist_ok=True)
        stem: str = input_file.stem

        start_time: float = time.time()

        # 步骤1: 读取PDF，渲染为图片
        task.update({"phase": "reading", "message": "Reading PDF..."})
        page_images: list[PageImage] = await loop.run_in_executor(
            None, pdf_reader.read_pdf, input_file
        )
        total_pages: int = len(page_images)
        task["total"] = total_pages
        logger.info(f"Task {task_id}: PDF读取完成, 共 {total_pages} 页")

        # 步骤2: 计算 MD5，初始化缓存
        task.update({"message": "Computing MD5..."})
        pdf_md5: str = await loop.run_in_executor(None, OCRCache.compute_md5, input_file)
        task["pdf_md5"] = pdf_md5
        ocr_cache = OCRCache(Path(config.web.upload_dir) / "ocr_cache")
        cached_count: int = ocr_cache.count_cached_pages(pdf_md5)
        logger.info(f"Task {task_id}: PDF MD5={pdf_md5}, 已缓存 {cached_count}/{total_pages} 页")

        # 步骤3: OCR（检查缓存，跳过已缓存页）
        pages_needing_ocr: list[int] = [
            i for i in range(total_pages) if not ocr_cache.is_page_cached(pdf_md5, i)
        ]

        ocr_semaphore = _get_ocr_semaphore()
        ocr_results: list[OCRResult] = []

        if pages_needing_ocr:
            task.update({"phase": "waiting_ocr", "message": "Waiting for GPU..."})
            async with ocr_semaphore:
                for i, page_img in enumerate(page_images):
                    cached_result: OCRResult | None = ocr_cache.load_page(pdf_md5, i)
                    if cached_result is not None:
                        # 缓存命中，直接使用
                        ocr_results.append(cached_result)
                        task.update({
                            "phase": "ocr",
                            "current": i + 1,
                            "message": f"OCR page {i + 1}/{total_pages} (cached)",
                        })
                        logger.debug(f"Task {task_id}: 页 {i} 缓存命中，跳过OCR")
                        continue

                    # 调用 OCR
                    task.update({
                        "phase": "ocr",
                        "current": i + 1,
                        "message": f"OCR page {i + 1}/{total_pages}",
                    })
                    result: OCRResult = await ocr_engine.ocr_single_image_async(
                        image_data=page_img.image_bytes,
                        page_index=page_img.page_index,
                    )
                    # 保存到缓存
                    await loop.run_in_executor(
                        None, ocr_cache.save_page, pdf_md5, i, result
                    )
                    ocr_results.append(result)
                    if not result.success:
                        logger.warning(f"Task {task_id}: 页 {i} OCR失败: {result.error_msg}")
        else:
            # 全部缓存命中，直接跳过 OCR
            task.update({
                "phase": "ocr",
                "current": total_pages,
                "message": f"Loading {total_pages} pages from cache...",
            })
            for i in range(total_pages):
                cached: OCRResult | None = ocr_cache.load_page(pdf_md5, i)
                if cached is not None:
                    ocr_results.append(cached)
                else:
                    # 理论上不应发生（is_page_cached 检查通过），防御性处理
                    logger.warning(f"Task {task_id}: 页 {i} 缓存读取失败，结果为空")
                    ocr_results.append(OCRResult(
                        page_index=i,
                        raw_text="",
                        success=False,
                        error_msg="Cache read failed unexpectedly",
                    ))
            logger.info(f"Task {task_id}: 所有页面均命中缓存，跳过OCR")

        # 步骤4: 解析OCR结果
        task.update({
            "phase": "parsing",
            "current": total_pages,
            "message": "Parsing OCR results...",
        })
        parsed_pages: list[ParsedPage] = []
        for ocr_result in ocr_results:
            parsed: ParsedPage = parser.parse(
                raw_text=ocr_result.raw_text,
                page_index=ocr_result.page_index,
            )
            parsed_pages.append(parsed)

        # 步骤5: 生成双层PDF（加 generating 串行锁，避免 PyMuPDF GIL 争用）
        output_pdf_path = output_dir / f"{stem}_ocr.pdf"
        task.update({"phase": "waiting_generate", "message": "Waiting to generate..."})
        generating_semaphore = _get_generating_semaphore()
        async with generating_semaphore:
            task.update({"phase": "generating", "message": "Generating PDF..."})
            await loop.run_in_executor(
                None,
                pdf_writer.create_dual_layer_pdf,
                page_images,
                parsed_pages,
                output_pdf_path,
                pdf_mode,
            )

        task["result_pdf"] = str(output_pdf_path)

        # 步骤6: 生成Markdown（在 generating 信号量外执行）
        output_md_path = output_dir / f"{stem}.md"
        task.update({"phase": "markdown", "message": "Generating Markdown..."})
        await loop.run_in_executor(
            None,
            markdown_writer.write,
            parsed_pages,
            output_md_path,
        )
        task["result_markdown"] = str(output_md_path)

        elapsed: float = time.time() - start_time
        task.update({
            "status": "completed",
            "phase": "completed",
            "done": True,
            "message": f"Conversion completed in {elapsed:.1f}s",
        })
        logger.info(f"Task {task_id} completed in {elapsed:.1f}s")

    except ImportError as e:
        # 核心模块尚未实现时的处理
        task["status"] = "error"
        task["done"] = True
        task["error"] = f"Core module not available: {str(e)}"
        task["message"] = f"Error: Core module not available"
        logger.error(f"Task {task_id} failed - module not found: {e}")

    except Exception as e:
        task["status"] = "error"
        task["done"] = True
        task["error"] = str(e)
        task["message"] = f"Error: {str(e)}"
        logger.error(f"Task {task_id} failed: {e}", exc_info=True)


@router.get("/api/progress/{task_id}")
async def get_progress(task_id: str) -> EventSourceResponse:
    """
    Business Logic:
        前端需要实时获取转换进度，通过SSE持续推送任务状态变化，
        让用户看到实时进度条。

    Code Logic:
        返回EventSourceResponse，内部轮询tasks字典中的任务状态，
        每500ms检查一次并推送更新。任务完成或出错时发送最终事件并关闭连接。
        事件格式: {"current": N, "total": M, "status": "...", "done": bool}
    """
    if task_id not in tasks:
        raise HTTPException(status_code=404, detail="Task not found")

    async def event_generator():
        """
        Business Logic:
            持续推送任务进度直到任务完成。

        Code Logic:
            异步生成器，循环检查任务状态并通过yield发送SSE事件。
        """
        last_message: str = ""
        last_current: int = -1

        while True:
            task = tasks.get(task_id)
            if task is None:
                yield {
                    "event": "error",
                    "data": json.dumps({"error": "Task not found"}, ensure_ascii=False),
                }
                break

            current: int = task["current"]
            total: int = task["total"]
            message: str = task["message"]
            done: bool = task["done"]
            error: str | None = task["error"]

            # 只在状态变化时推送，减少不必要的传输
            if message != last_message or current != last_current or done:
                event_data: dict[str, Any] = {
                    "current": current,
                    "total": total,
                    "status": message,
                    "done": done,
                }
                if error:
                    event_data["error"] = error

                yield {
                    "event": "progress",
                    "data": json.dumps(event_data, ensure_ascii=False),
                }

                last_message = message
                last_current = current

                if done:
                    break

            await asyncio.sleep(0.5)

    return EventSourceResponse(event_generator())


@router.get("/api/download/{task_id}/{file_type}")
async def download_result(task_id: str, file_type: str) -> FileResponse:
    """
    Business Logic:
        转换完成后用户需要下载结果文件（PDF或Markdown），
        根据task_id和文件类型返回对应的文件。

    Code Logic:
        从tasks字典中查找对应任务的结果文件路径，
        验证文件存在后返回FileResponse供浏览器下载。
        file_type支持 "pdf" 和 "markdown" 两种。
    """
    if task_id not in tasks:
        raise HTTPException(status_code=404, detail="Task not found")

    task = tasks[task_id]

    if not task["done"]:
        raise HTTPException(status_code=400, detail="Task not completed yet")

    if task["status"] == "error":
        raise HTTPException(status_code=500, detail=f"Task failed: {task['error']}")

    if file_type == "pdf":
        file_path_str: str | None = task.get("result_pdf")
        media_type: str = "application/pdf"
        suffix: str = ".pdf"
    elif file_type == "markdown":
        file_path_str = task.get("result_markdown")
        media_type = "text/markdown"
        suffix = ".md"
    else:
        raise HTTPException(status_code=400, detail="Invalid file type. Use 'pdf' or 'markdown'")

    if not file_path_str:
        raise HTTPException(status_code=404, detail=f"Result file ({file_type}) not available")

    result_path = Path(file_path_str)
    if not result_path.exists():
        raise HTTPException(status_code=404, detail="Result file not found on disk")

    # 生成下载文件名
    original_name: str = task.get("filename", "result")
    if original_name.lower().endswith(".pdf"):
        original_name = original_name[:-4]
    download_name: str = f"{original_name}_ocr{suffix}"

    return FileResponse(
        path=str(result_path),
        media_type=media_type,
        filename=download_name,
    )


@router.get("/api/health")
async def health_check() -> dict[str, Any]:
    """
    Business Logic:
        运维和前端需要检查后端服务、Ollama连接、模型是否可用，
        用于服务健康监控和用户提示。

    Code Logic:
        检查Ollama服务是否可连接，检查所需模型是否已加载。
        返回各组件的状态信息。
    """
    config = AppConfig()
    health: dict[str, Any] = {
        "service": "ok",
        "ollama": "unknown",
        "model": config.ollama.model,
    }

    try:
        import ollama
        client = ollama.Client(host=config.ollama.host)
        # 尝试列出模型
        models_response = client.list()
        available_models: list[str] = []
        if hasattr(models_response, "models"):
            available_models = [m.model for m in models_response.models]
        elif isinstance(models_response, dict) and "models" in models_response:
            available_models = [m.get("model", "") for m in models_response["models"]]

        health["ollama"] = "connected"
        health["available_models"] = available_models

        # 检查目标模型是否存在
        model_found: bool = any(
            config.ollama.model in m for m in available_models
        )
        health["model_available"] = model_found

    except ImportError:
        health["ollama"] = "ollama package not installed"
    except Exception as e:
        health["ollama"] = f"error: {str(e)}"

    return health
