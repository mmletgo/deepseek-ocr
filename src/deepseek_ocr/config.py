"""
Business Logic:
    集中管理所有配置项，包括Ollama连接、PDF渲染参数、Web服务等，
    避免配置散落在各模块中。

Code Logic:
    使用dataclass定义配置结构，支持默认值和环境变量覆盖。
    通过 python-dotenv 加载 .env 文件，所有字段均可通过环境变量配置。
"""

from dataclasses import dataclass, field
from enum import StrEnum
import os

from dotenv import load_dotenv

load_dotenv()


class PDFOutputMode(StrEnum):
    """PDF输出模式"""
    DUAL_LAYER = "dual_layer"
    REWRITE = "rewrite"


@dataclass
class OllamaConfig:
    """Ollama服务配置"""
    host: str = field(default_factory=lambda: os.getenv("OLLAMA_HOST", "http://localhost:11434"))
    model: str = field(default_factory=lambda: os.getenv("OLLAMA_MODEL", "deepseek-ocr"))
    timeout: int = field(default_factory=lambda: int(os.getenv("OLLAMA_TIMEOUT", "300")))
    keep_alive: int = field(default_factory=lambda: int(os.getenv("OLLAMA_KEEP_ALIVE", "-1")))


@dataclass
class PDFConfig:
    """PDF渲染配置"""
    dpi: int = field(default_factory=lambda: int(os.getenv("PDF_DPI", "200")))
    max_dimension: int = field(default_factory=lambda: int(os.getenv("PDF_MAX_DIMENSION", "1920")))
    image_format: str = field(default_factory=lambda: os.getenv("PDF_IMAGE_FORMAT", "png"))
    output_mode: PDFOutputMode = field(default_factory=lambda: PDFOutputMode(os.getenv("PDF_OUTPUT_MODE", "dual_layer")))


@dataclass
class WebConfig:
    """Web服务配置"""
    host: str = field(default_factory=lambda: os.getenv("WEB_HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: int(os.getenv("WEB_PORT", "8080")))
    upload_dir: str = field(default_factory=lambda: os.getenv("WEB_UPLOAD_DIR", "./uploads"))
    max_upload_size_mb: int = field(default_factory=lambda: int(os.getenv("WEB_MAX_UPLOAD_SIZE_MB", "200")))


@dataclass
class TranslationConfig:
    """LLM翻译服务配置（OpenAI兼容接口）"""
    base_url: str = field(default_factory=lambda: os.getenv("TRANSLATION_BASE_URL", "https://api.openai.com/v1"))
    api_key: str = field(default_factory=lambda: os.getenv("TRANSLATION_API_KEY", ""))
    model: str = field(default_factory=lambda: os.getenv("TRANSLATION_MODEL", "gpt-4o-mini"))
    timeout: int = field(default_factory=lambda: int(os.getenv("TRANSLATION_TIMEOUT", "120")))
    max_retries: int = field(default_factory=lambda: int(os.getenv("TRANSLATION_MAX_RETRIES", "3")))
    temperature: float = field(default_factory=lambda: float(os.getenv("TRANSLATION_TEMPERATURE", "0.3")))


@dataclass
class AppConfig:
    """应用全局配置"""
    ollama: OllamaConfig = field(default_factory=OllamaConfig)
    pdf: PDFConfig = field(default_factory=PDFConfig)
    web: WebConfig = field(default_factory=WebConfig)
    translation: TranslationConfig = field(default_factory=TranslationConfig)
    output_dir: str = field(default_factory=lambda: os.getenv("OUTPUT_DIR", "./output"))
