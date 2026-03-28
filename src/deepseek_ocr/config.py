"""
Business Logic:
    集中管理所有配置项，包括Ollama连接、PDF渲染参数、Web服务等，
    避免配置散落在各模块中。

Code Logic:
    使用dataclass定义配置结构，支持默认值和环境变量覆盖。
"""

from dataclasses import dataclass, field
from enum import StrEnum
import os


class PDFOutputMode(StrEnum):
    """PDF输出模式"""
    DUAL_LAYER = "dual_layer"
    REWRITE = "rewrite"


@dataclass
class OllamaConfig:
    """Ollama服务配置"""
    host: str = field(default_factory=lambda: os.getenv("OLLAMA_HOST", "http://localhost:11434"))
    model: str = "deepseek-ocr"
    timeout: int = 300
    keep_alive: int = -1


@dataclass
class PDFConfig:
    """PDF渲染配置"""
    dpi: int = 200
    max_dimension: int = 1920
    image_format: str = "png"
    output_mode: PDFOutputMode = PDFOutputMode.DUAL_LAYER


@dataclass
class WebConfig:
    """Web服务配置"""
    host: str = "0.0.0.0"
    port: int = 8080
    upload_dir: str = "./uploads"
    max_upload_size_mb: int = 200


@dataclass
class TranslationConfig:
    """LLM翻译服务配置（OpenAI兼容接口）"""
    base_url: str = field(default_factory=lambda: os.getenv("TRANSLATION_BASE_URL", "https://api.openai.com/v1"))
    api_key: str = field(default_factory=lambda: os.getenv("TRANSLATION_API_KEY", ""))
    model: str = field(default_factory=lambda: os.getenv("TRANSLATION_MODEL", "gpt-4o-mini"))
    timeout: int = 120
    max_retries: int = 3
    temperature: float = 0.3


@dataclass
class AppConfig:
    """应用全局配置"""
    ollama: OllamaConfig = field(default_factory=OllamaConfig)
    pdf: PDFConfig = field(default_factory=PDFConfig)
    web: WebConfig = field(default_factory=WebConfig)
    translation: TranslationConfig = field(default_factory=TranslationConfig)
    output_dir: str = "./output"
