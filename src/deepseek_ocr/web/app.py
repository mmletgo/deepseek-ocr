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


def create_app() -> FastAPI:
    """
    Business Logic:
        用户通过Web界面上传PDF进行OCR转换，需要一个FastAPI应用作为服务入口。

    Code Logic:
        创建FastAPI实例，挂载静态文件目录(static)，注册路由模块(routes)，返回应用实例。
    """
    app = FastAPI(title="DeepSeek-OCR Web", version="0.1.0")

    static_dir = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    from deepseek_ocr.web.routes import router
    app.include_router(router)

    return app
