"""
Business Logic:
    作为Web服务的入口，创建并配置FastAPI应用实例，
    统一管理静态文件挂载和路由注册。

Code Logic:
    使用工厂函数模式创建FastAPI应用，挂载static目录，注册API路由。
"""

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from pathlib import Path
from contextlib import asynccontextmanager
from typing import AsyncIterator


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """应用生命周期管理：关闭时释放全局OCR引擎"""
    yield
    # shutdown: 释放引擎
    from deepseek_ocr.web.routes import _global_ocr_engine
    if _global_ocr_engine is not None:
        _global_ocr_engine.shutdown()


def create_app() -> FastAPI:
    """
    Business Logic:
        用户通过Web界面上传PDF进行OCR转换，需要一个FastAPI应用作为服务入口。

    Code Logic:
        创建FastAPI实例，挂载静态文件目录(static)，注册路由模块(routes)，返回应用实例。
        通过 lifespan 管理全局 OCR 引擎的初始化和释放。
    """
    app = FastAPI(title="DeepSeek-OCR Web", version="0.2.0", lifespan=lifespan)

    static_dir = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    from deepseek_ocr.web.routes import router
    app.include_router(router)

    return app
