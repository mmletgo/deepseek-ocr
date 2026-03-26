"""
Business Logic:
    定义Web API的所有路由端点，包括首页、文件上传、进度推送、结果下载和健康检查，
    为用户提供完整的PDF OCR转换Web交互流程。

Code Logic:
    使用FastAPI APIRouter管理路由。上传后启动异步后台任务执行转换，
    通过SSE(Server-Sent Events)实时推送转换进度，支持结果文件下载。
    任务状态通过内存字典管理。
"""

from fastapi import APIRouter, UploadFile, File, HTTPException, BackgroundTasks
from fastapi.responses import FileResponse, HTMLResponse
from sse_starlette.sse import EventSourceResponse
import asyncio
import concurrent.futures
import uuid
import json
from pathlib import Path
from typing import Any, List

from deepseek_ocr.config import AppConfig
from deepseek_ocr.utils.logger import logger
from deepseek_ocr.core.pdf_reader import PDFReader, PageImage
from deepseek_ocr.core.ocr_engine import OCREngine, OCRResult
from deepseek_ocr.core.output_parser import OutputParser, ParsedPage
from deepseek_ocr.core.pdf_writer import DualLayerPDFWriter
from deepseek_ocr.core.markdown_writer import MarkdownWriter

router = APIRouter()

# 内存中的任务管理字典，key为task_id，value为任务状态信息
tasks: dict[str, dict[str, Any]] = {}

# 全局配置
_config = AppConfig()

# OCR 串行锁：同一时刻只允许一个任务调用 Ollama
_ocr_semaphore: asyncio.Semaphore | None = None


def _get_ocr_semaphore() -> asyncio.Semaphore:
    """惰性初始化OCR串行锁，确保在事件循环启动后创建"""
    global _ocr_semaphore
    if _ocr_semaphore is None:
        _ocr_semaphore = asyncio.Semaphore(1)
    return _ocr_semaphore


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
async def upload_pdf(files: List[UploadFile] = File(...)) -> list[dict[str, str]]:
    """
    Business Logic:
        用户上传一个或多个PDF文件后，系统需要保存文件并启动异步OCR转换任务，
        返回任务ID列表供前端轮询进度。

    Code Logic:
        1. 验证每个上传文件是否为PDF格式
        2. 为每个文件生成唯一task_id (UUID)
        3. 将上传文件保存到 uploads/{task_id}/ 目录
        4. 初始化任务状态并创建后台异步转换任务
        5. 返回包含task_id和原始文件名的列表
    """
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
            "phase": "waiting_ocr",
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
        }

        # 启动后台转换任务
        asyncio.create_task(_run_conversion(task_id))

        logger.info(f"Task {task_id} created for file: {file.filename}")

        results.append({"task_id": task_id, "filename": file.filename})

    return results


async def _run_conversion(task_id: str) -> None:
    """
    Business Logic:
        后台执行PDF OCR转换的主流程编排，分步骤执行：
        CPU密集步骤放线程池以防阻塞事件循环，
        OCR步骤用串行锁确保同一时刻只有一个任务占用GPU。

    Code Logic:
        步骤1: PDF读取 → run_in_executor（非阻塞事件循环）
        步骤2: OCR → asyncio.Semaphore(1)串行锁 + 异步逐页调用
        步骤3: 解析 → 同步（极快）
        步骤4: 生成双层PDF → run_in_executor（内部页面级并行）
        步骤5: 写Markdown → run_in_executor
    """
    task = tasks.get(task_id)
    if task is None:
        return

    loop = asyncio.get_event_loop()
    config = AppConfig()

    pdf_reader: PDFReader = PDFReader(dpi=config.pdf.dpi, max_dimension=config.pdf.max_dimension)
    ocr_engine: OCREngine = OCREngine(config.ollama)
    parser: OutputParser = OutputParser()
    pdf_writer: DualLayerPDFWriter = DualLayerPDFWriter()
    md_writer: MarkdownWriter = MarkdownWriter()

    input_file: str = task["input_file"]
    output_dir: str = task["output_dir"]
    Path(output_dir).mkdir(parents=True, exist_ok=True)

    try:
        # 步骤1: 读取 PDF（CPU密集 → 线程池，非阻塞事件循环）
        task.update({"status": "running", "phase": "reading_pdf",
                     "message": "Reading PDF..."})
        page_images: list[PageImage] = await loop.run_in_executor(
            None, pdf_reader.read_pdf, input_file
        )
        total_pages: int = len(page_images)
        task["total"] = total_pages
        logger.info(f"Task {task_id}: PDF读取完成, 共 {total_pages} 页")

        # 步骤2: OCR（串行锁，逐页异步调用 Ollama）
        task.update({"phase": "waiting_ocr", "message": "Waiting for GPU..."})
        semaphore: asyncio.Semaphore = _get_ocr_semaphore()

        async with semaphore:
            ocr_results: list[OCRResult] = []
            for i, page_img in enumerate(page_images):
                task.update({
                    "phase": "ocr",
                    "current": i + 1,
                    "message": f"OCR page {i + 1}/{total_pages}",
                })
                result: OCRResult = await ocr_engine.ocr_single_image_async(
                    image_data=page_img.image_bytes,
                    page_index=page_img.page_index,
                )
                ocr_results.append(result)
                if not result.success:
                    logger.warning(f"Task {task_id}: 页 {i} OCR失败: {result.error_msg}")

        # 步骤3: 解析 OCR 结果（极快，直接同步）
        task.update({"phase": "generating", "message": "Parsing results..."})
        parsed_pages: list[ParsedPage] = [
            parser.parse(r.raw_text, r.page_index) for r in ocr_results
        ]

        # 步骤4: 生成双层 PDF（run_in_executor，内部页面级并行）
        task["message"] = "Generating PDF..."
        stem: str = Path(input_file).stem
        output_pdf_path: Path = Path(output_dir) / f"{stem}_ocr.pdf"
        await loop.run_in_executor(
            None,
            pdf_writer.create_dual_layer_pdf,
            page_images,
            parsed_pages,
            output_pdf_path,
        )

        # 步骤5: 写 Markdown（run_in_executor）
        task["message"] = "Writing Markdown..."
        output_md_path: Path = Path(output_dir) / f"{stem}.md"
        await loop.run_in_executor(
            None, md_writer.write, parsed_pages, output_md_path
        )

        task.update({
            "status": "completed",
            "phase": "done",
            "done": True,
            "current": total_pages,
            "message": "Conversion complete",
            "result_pdf": str(output_pdf_path),
            "result_markdown": str(output_md_path),
        })
        logger.info(f"Task {task_id} completed successfully")

    except Exception as e:
        task.update({
            "status": "error",
            "phase": "done",
            "done": True,
            "error": str(e),
            "message": f"Error: {str(e)}",
        })
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
                    "phase": task.get("phase", ""),
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
