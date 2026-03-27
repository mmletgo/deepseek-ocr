"""
Business Logic:
    定义Web API的所有路由端点，包括首页、文件上传、进度推送、结果下载和健康检查，
    为用户提供完整的PDF OCR转换Web交互流程。

Code Logic:
    使用FastAPI APIRouter管理路由。上传后启动异步后台任务执行转换，
    通过SSE(Server-Sent Events)实时推送转换进度，支持结果文件下载。
    任务状态通过内存字典管理。
"""

from fastapi import APIRouter, UploadFile, File, Form, HTTPException, BackgroundTasks
from fastapi.responses import FileResponse, HTMLResponse
from sse_starlette.sse import EventSourceResponse
import asyncio
import uuid
import json
from pathlib import Path
from typing import Any

from deepseek_ocr.config import AppConfig, PDFConfig, PDFOutputMode
from deepseek_ocr.utils.logger import logger

router = APIRouter()

# 内存中的任务管理字典，key为task_id，value为任务状态信息
tasks: dict[str, dict[str, Any]] = {}

# 全局配置
_config = AppConfig()


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
    file: UploadFile = File(...),
    pdf_mode: str = Form("dual_layer"),
) -> dict[str, str]:
    """
    Business Logic:
        用户上传PDF文件后，系统需要保存文件并启动异步OCR转换任务，
        返回任务ID供前端轮询进度。

    Code Logic:
        1. 验证上传文件是否为PDF格式
        2. 生成唯一task_id (UUID)
        3. 将上传文件保存到 uploads/{task_id}/ 目录
        4. 初始化任务状态并创建后台异步转换任务
        5. 返回task_id和原始文件名
    """
    # 验证pdf_mode
    if pdf_mode not in ("dual_layer", "rewrite"):
        raise HTTPException(status_code=400, detail=f"Invalid pdf_mode: {pdf_mode}")

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
        "pdf_mode": pdf_mode,
    }

    # 启动后台转换任务
    asyncio.create_task(_run_conversion(task_id))

    logger.info(f"Task {task_id} created for file: {file.filename}")

    return {"task_id": task_id, "filename": file.filename}


async def _run_conversion(task_id: str) -> None:
    """
    Business Logic:
        后台执行PDF OCR转换，实时更新任务进度供SSE推送。

    Code Logic:
        使用ConversionPipeline的异步接口执行转换，通过进度回调更新tasks字典。
        转换完成后记录结果文件路径，出错时记录错误信息。
    """
    task = tasks.get(task_id)
    if task is None:
        return

    task["status"] = "running"
    task["message"] = "Initializing..."

    def progress_callback(current: int, total: int, message: str) -> None:
        """进度回调，更新任务状态"""
        if task_id in tasks:
            tasks[task_id]["current"] = current
            tasks[task_id]["total"] = total
            tasks[task_id]["message"] = message

    try:
        from deepseek_ocr.core.pipeline import ConversionPipeline

        pdf_mode: str = task.get("pdf_mode", "dual_layer")
        config = AppConfig(
            pdf=PDFConfig(output_mode=PDFOutputMode(pdf_mode)),
        )
        pipeline = ConversionPipeline(
            config=config,
            progress_callback=progress_callback,
        )

        input_file = task["input_file"]
        output_dir = task["output_dir"]
        Path(output_dir).mkdir(parents=True, exist_ok=True)

        result = await pipeline.convert_async(
            input_pdf=input_file,
            output_dir=output_dir,
            generate_pdf=True,
            generate_markdown=True,
        )

        # 记录结果文件路径
        if result.output_pdf:
            task["result_pdf"] = str(result.output_pdf)
        if result.output_markdown:
            task["result_markdown"] = str(result.output_markdown)

        task["status"] = "completed"
        task["done"] = True
        task["message"] = "Conversion completed"
        logger.info(f"Task {task_id} completed successfully")

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
        logger.error(f"Task {task_id} failed: {e}")


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
