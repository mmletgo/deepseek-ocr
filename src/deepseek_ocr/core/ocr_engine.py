# -*- coding: utf-8 -*-
"""
Business Logic:
    通过Ollama调用DeepSeek-OCR模型对图片进行文字识别。
    这是整个OCR流水线的核心环节，负责将扫描图片转换为带坐标的文本输出。

Code Logic:
    封装Ollama的同步和异步客户端，提供OCR接口。
    支持三种提示模式(Markdown定位、OCR定位、自由OCR)。
    包含健康检查、重试机制(3次重试+指数退避)。
"""

import time
from dataclasses import dataclass
from enum import Enum

from ollama import Client, AsyncClient

from deepseek_ocr.config import OllamaConfig
from deepseek_ocr.utils.logger import logger


class PromptMode(Enum):
    """OCR提示模式，控制DeepSeek-OCR的输出格式"""
    MARKDOWN_GROUNDING = "<|grounding|>Convert the document to markdown."
    OCR_GROUNDING = "<|grounding|>OCR this image."
    FREE_OCR = "Free OCR."


@dataclass
class OCRResult:
    """单页OCR识别结果"""
    raw_text: str           # 原始OCR输出文本(可能包含坐标标签)
    page_index: int         # 对应的页码(从0开始)
    success: bool           # 识别是否成功
    error_msg: str | None = None  # 错误信息(成功时为None)


class OCREngine:
    """OCR引擎，通过Ollama调用DeepSeek-OCR模型进行文字识别"""

    MAX_RETRIES: int = 3
    RETRY_DELAY: float = 2.0

    def __init__(self, config: OllamaConfig) -> None:
        """
        Business Logic:
            初始化OCR引擎，建立与Ollama服务的连接。
            需要确保Ollama服务可用且已加载DeepSeek-OCR模型。

        Code Logic:
            根据OllamaConfig创建同步和异步Ollama客户端，
            保存模型名称和超时配置。
        """
        self.config: OllamaConfig = config
        self.client: Client = Client(
            host=config.host,
            timeout=config.timeout,
        )
        self.async_client: AsyncClient = AsyncClient(
            host=config.host,
            timeout=config.timeout,
        )
        self.model: str = config.model
        logger.info(f"OCREngine初始化: host={config.host}, model={config.model}")

    def check_health(self) -> bool:
        """
        Business Logic:
            检查Ollama服务和DeepSeek-OCR模型是否可用。
            在开始处理之前需要先确认服务状态。

        Code Logic:
            调用client.list()获取已加载模型列表，
            检查目标模型是否在列表中。
        """
        try:
            model_list = self.client.list()
            available_models: list[str] = [
                m.model for m in model_list.models
            ]
            # 检查模型名称是否匹配（可能带版本后缀如:latest）
            for model_name in available_models:
                if self.model in model_name or model_name.startswith(self.model):
                    logger.info(f"模型 {self.model} 可用")
                    return True
            logger.warning(f"模型 {self.model} 不在已加载列表中, 可用模型: {available_models}")
            return False
        except Exception as e:
            logger.error(f"Ollama服务健康检查失败: {e}")
            return False

    def ocr_single_image(
        self,
        image_data: bytes,
        page_index: int,
        mode: PromptMode = PromptMode.MARKDOWN_GROUNDING,
    ) -> OCRResult:
        """
        Business Logic:
            对单张图片进行OCR识别，返回带坐标标签的文本结果。
            支持重试机制，确保因网络波动等临时故障不会导致整个任务失败。

        Code Logic:
            将图片字节传给Ollama chat接口，使用指定的提示模式。
            设置temperature=0.0保证输出一致性，num_ctx=8192支持长文本。
            失败时进行最多3次重试，每次等待时间指数递增。
        """
        last_error: str | None = None

        for attempt in range(self.MAX_RETRIES):
            try:
                logger.debug(f"页 {page_index}: 开始OCR (尝试 {attempt + 1}/{self.MAX_RETRIES})")

                response = self.client.chat(
                    model=self.model,
                    messages=[
                        {
                            "role": "user",
                            "content": mode.value,
                            "images": [image_data],
                        }
                    ],
                    options={
                        "temperature": 0.0,
                        "num_ctx": 8192,
                    },
                    keep_alive=self.config.keep_alive,
                )

                raw_text: str = response.message.content or ""
                logger.debug(f"页 {page_index}: OCR完成, 输出长度={len(raw_text)}")

                return OCRResult(
                    raw_text=raw_text,
                    page_index=page_index,
                    success=True,
                )

            except Exception as e:
                last_error = str(e)
                logger.warning(
                    f"页 {page_index}: OCR失败 (尝试 {attempt + 1}/{self.MAX_RETRIES}): {last_error}"
                )
                if attempt < self.MAX_RETRIES - 1:
                    delay: float = self.RETRY_DELAY * (2 ** attempt)
                    logger.info(f"等待 {delay:.1f}s 后重试...")
                    time.sleep(delay)

        logger.error(f"页 {page_index}: OCR最终失败: {last_error}")
        return OCRResult(
            raw_text="",
            page_index=page_index,
            success=False,
            error_msg=last_error,
        )

    async def ocr_single_image_async(
        self,
        image_data: bytes,
        page_index: int,
        mode: PromptMode = PromptMode.MARKDOWN_GROUNDING,
    ) -> OCRResult:
        """
        Business Logic:
            异步版本的OCR识别，用于Web界面等需要并发处理的场景。
            避免阻塞事件循环，提升并发性能。

        Code Logic:
            使用AsyncClient调用Ollama chat接口，逻辑与同步版本相同。
            使用asyncio.sleep替代time.sleep实现非阻塞重试等待。
        """
        import asyncio

        last_error: str | None = None

        for attempt in range(self.MAX_RETRIES):
            try:
                logger.debug(f"页 {page_index}: 开始异步OCR (尝试 {attempt + 1}/{self.MAX_RETRIES})")

                response = await self.async_client.chat(
                    model=self.model,
                    messages=[
                        {
                            "role": "user",
                            "content": mode.value,
                            "images": [image_data],
                        }
                    ],
                    options={
                        "temperature": 0.0,
                        "num_ctx": 8192,
                    },
                    keep_alive=self.config.keep_alive,
                )

                raw_text: str = response.message.content or ""
                logger.debug(f"页 {page_index}: 异步OCR完成, 输出长度={len(raw_text)}")

                return OCRResult(
                    raw_text=raw_text,
                    page_index=page_index,
                    success=True,
                )

            except Exception as e:
                last_error = str(e)
                logger.warning(
                    f"页 {page_index}: 异步OCR失败 (尝试 {attempt + 1}/{self.MAX_RETRIES}): {last_error}"
                )
                if attempt < self.MAX_RETRIES - 1:
                    delay: float = self.RETRY_DELAY * (2 ** attempt)
                    logger.info(f"等待 {delay:.1f}s 后重试...")
                    await asyncio.sleep(delay)

        logger.error(f"页 {page_index}: 异步OCR最终失败: {last_error}")
        return OCRResult(
            raw_text="",
            page_index=page_index,
            success=False,
            error_msg=last_error,
        )
