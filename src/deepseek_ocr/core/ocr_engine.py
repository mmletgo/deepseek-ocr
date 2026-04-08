# -*- coding: utf-8 -*-
"""
Business Logic:
    通过vLLM调用DeepSeek-OCR-2模型对图片进行文字识别。
    支持同步(CLI)和异步(Web)两种推理模式。

Code Logic:
    封装vLLM的同步LLM和异步AsyncLLMEngine。
    使用DeepseekOCR2Processor进行图像预处理。
    支持三种提示模式(Markdown定位、OCR定位、自由OCR)。
    包含健康检查、重试机制(3次重试+指数退避)。
"""

import os
import sys
import time
import uuid
import asyncio
import io
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from PIL import Image

from deepseek_ocr.config import VLLMConfig
from deepseek_ocr.utils.logger import logger

# 环境变量: 必须使用v0引擎
os.environ.setdefault("VLLM_USE_V1", "0")

# 懒加载导入 (避免在未安装vLLM时导入失败)
_vllm_available: bool | None = None
_deepseek_ocr2_available: bool | None = None


def _check_vllm_available() -> bool:
    """检查vLLM是否可用"""
    global _vllm_available
    if _vllm_available is None:
        try:
            import vllm
            _vllm_available = True
        except ImportError:
            _vllm_available = False
    return _vllm_available


def _check_deepseek_ocr2_available() -> bool:
    """检查DeepSeek-OCR-2模块是否可用"""
    global _deepseek_ocr2_available
    if _deepseek_ocr2_available is None:
        try:
            from deepseek_ocr2 import DeepseekOCR2ForCausalLM
            from process.image_process import DeepseekOCR2Processor
            from process.ngram_norepeat import NoRepeatNGramLogitsProcessor
            _deepseek_ocr2_available = True
        except ImportError:
            _deepseek_ocr2_available = False
    return _deepseek_ocr2_available


def _register_model() -> None:
    """注册DeepSeek-OCR-2自定义模型到vLLM ModelRegistry"""
    from vllm.model_executor.models.registry import ModelRegistry
    from deepseek_ocr2 import DeepseekOCR2ForCausalLM
    ModelRegistry.register_model("DeepseekOCR2ForCausalLM", DeepseekOCR2ForCausalLM)
    logger.info("已注册 DeepseekOCR2ForCausalLM 到 vLLM ModelRegistry")


class PromptMode(Enum):
    """OCR提示模式，控制DeepSeek-OCR-2的输出格式"""
    MARKDOWN_GROUNDING = "<image>\n<|grounding|>Convert the document to markdown."
    OCR_GROUNDING = "<image>\n<|grounding|>OCR this image."
    FREE_OCR = "<image>\nFree OCR."


@dataclass
class OCRResult:
    """单页OCR识别结果"""
    raw_text: str           # 原始OCR输出文本(可能包含坐标标签)
    page_index: int         # 对应的页码(从0开始)
    success: bool           # 识别是否成功
    error_msg: str | None = None  # 错误信息(成功时为None)


class OCREngine:
    """OCR引擎，通过vLLM调用DeepSeek-OCR-2模型进行文字识别"""

    MAX_RETRIES: int = 3
    RETRY_DELAY: float = 2.0

    def __init__(self, config: VLLMConfig, *, async_mode: bool = False) -> None:
        """
        Args:
            config: vLLM配置
            async_mode: True时使用AsyncLLMEngine(Web场景)，False时使用同步LLM(CLI场景)
        """
        self.config: VLLMConfig = config
        self.async_mode: bool = async_mode
        self._sync_engine: Any = None  # vllm.LLM
        self._async_engine: Any = None  # vllm.AsyncLLMEngine
        self._processor: Any = None    # DeepseekOCR2Processor
        self._initialized: bool = False
        self._model_registered: bool = False
        logger.info(
            f"OCREngine初始化: model_path={config.model_path}, "
            f"async_mode={async_mode}"
        )

    def _ensure_model_registered(self) -> None:
        """确保自定义模型已注册到vLLM"""
        if not self._model_registered:
            _register_model()
            self._model_registered = True

    def _create_sampling_params(self) -> Any:
        """创建采样参数"""
        from vllm import SamplingParams
        from process.ngram_norepeat import NoRepeatNGramLogitsProcessor

        logits_processors = [
            NoRepeatNGramLogitsProcessor(
                ngram_size=20,
                window_size=90,
                whitelist_token_ids={128821, 128822},
            )
        ]
        return SamplingParams(
            temperature=0.0,
            max_tokens=self.config.max_model_len,
            logits_processors=logits_processors,
            skip_special_tokens=False,
        )

    def _preprocess_image(self, image_data: bytes) -> Any:
        """预处理图像为vLLM可接受格式"""
        from process.image_process import DeepseekOCR2Processor

        if self._processor is None:
            self._processor = DeepseekOCR2Processor()

        pil_image = Image.open(io.BytesIO(image_data)).convert("RGB")
        features = self._processor.tokenize_with_images(
            images=[pil_image],
            bos=True,
            eos=True,
            cropping=True,
        )
        return features

    def initialize(self) -> None:
        """加载模型到GPU（同步模式），CLI启动时调用"""
        if self._initialized:
            return

        from vllm import LLM

        self._ensure_model_registered()
        logger.info(f"正在加载模型: {self.config.model_path} (同步模式)...")

        self._sync_engine = LLM(
            model=self.config.model_path,
            hf_overrides={"architectures": ["DeepseekOCR2ForCausalLM"]},
            block_size=256,
            enforce_eager=False,
            trust_remote_code=True,
            max_model_len=self.config.max_model_len,
            swap_space=0,
            max_num_seqs=self.config.max_concurrency,
            tensor_parallel_size=self.config.tensor_parallel_size,
            gpu_memory_utilization=self.config.gpu_memory_utilization,
            disable_mm_preprocessor_cache=True,
            dtype=self.config.dtype,
        )
        self._initialized = True
        logger.info("模型加载完成 (同步模式)")

    async def initialize_async(self) -> None:
        """加载模型到GPU（异步模式），Web服务启动时调用"""
        if self._initialized:
            return

        from vllm import AsyncLLMEngine
        from vllm.engine.arg_utils import AsyncEngineArgs

        self._ensure_model_registered()
        logger.info(f"正在加载模型: {self.config.model_path} (异步模式)...")

        engine_args = AsyncEngineArgs(
            model=self.config.model_path,
            hf_overrides={"architectures": ["DeepseekOCR2ForCausalLM"]},
            dtype=self.config.dtype,
            max_model_len=self.config.max_model_len,
            enforce_eager=False,
            trust_remote_code=True,
            tensor_parallel_size=self.config.tensor_parallel_size,
            gpu_memory_utilization=self.config.gpu_memory_utilization,
        )
        self._async_engine = AsyncLLMEngine.from_engine_args(engine_args)
        self._initialized = True
        logger.info("模型加载完成 (异步模式)")

    def shutdown(self) -> None:
        """释放引擎和GPU显存"""
        if self._sync_engine is not None:
            del self._sync_engine
            self._sync_engine = None
        if self._async_engine is not None:
            # AsyncLLMEngine 的关闭
            if hasattr(self._async_engine, "shutdown"):
                self._async_engine.shutdown()
            del self._async_engine
            self._async_engine = None
        self._initialized = False
        self._processor = None

        # 强制释放GPU显存
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
                logger.info("GPU显存已释放")
        except ImportError:
            pass

    def check_health(self) -> bool:
        """
        检查GPU可用性和模型状态
        """
        try:
            import torch
            if not torch.cuda.is_available():
                logger.error("GPU不可用")
                return False

            # 检查模型文件
            model_path = Path(self.config.model_path)
            if not model_path.exists():
                # 可能是HuggingFace repo ID，检查模块可用性
                if not _check_deepseek_ocr2_available():
                    logger.error("DeepSeek-OCR-2模块未安装，请运行 setup_vllm.sh")
                    return False

            if not _check_vllm_available():
                logger.error("vLLM未安装")
                return False

            logger.info("环境检查通过")
            return True
        except Exception as e:
            logger.error(f"健康检查失败: {e}")
            return False

    def ocr_single_image(
        self,
        image_data: bytes,
        page_index: int,
        mode: PromptMode = PromptMode.MARKDOWN_GROUNDING,
    ) -> OCRResult:
        """同步OCR，使用vllm.LLM.generate()"""
        if not self._initialized:
            self.initialize()

        last_error: str | None = None

        for attempt in range(self.config.max_retries):
            try:
                logger.debug(f"页 {page_index}: 开始OCR (尝试 {attempt + 1}/{self.config.max_retries})")

                # 预处理图像
                image_features = self._preprocess_image(image_data)

                # 构建请求
                request: dict[str, Any] = {
                    "prompt": mode.value,
                    "multi_modal_data": {"image": image_features},
                }

                sampling_params = self._create_sampling_params()
                outputs = self._sync_engine.generate(
                    [request], sampling_params=sampling_params
                )

                raw_text: str = outputs[0].outputs[0].text
                logger.debug(f"页 {page_index}: OCR完成, 输出长度={len(raw_text)}")

                return OCRResult(
                    raw_text=raw_text,
                    page_index=page_index,
                    success=True,
                )

            except Exception as e:
                last_error = str(e)
                logger.warning(
                    f"页 {page_index}: OCR失败 (尝试 {attempt + 1}/{self.config.max_retries}): {last_error}"
                )
                if attempt < self.config.max_retries - 1:
                    delay: float = self.config.retry_delay * (2 ** attempt)
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
        """异步OCR，使用AsyncLLMEngine"""
        if not self._initialized:
            await self.initialize_async()

        last_error: str | None = None

        for attempt in range(self.config.max_retries):
            try:
                logger.debug(f"页 {page_index}: 开始异步OCR (尝试 {attempt + 1}/{self.config.max_retries})")

                # 在线程池中预处理图像（CPU密集操作）
                loop = asyncio.get_event_loop()
                image_features = await loop.run_in_executor(
                    None, self._preprocess_image, image_data
                )

                # 构建请求
                request: dict[str, Any] = {
                    "prompt": mode.value,
                    "multi_modal_data": {"image": image_features},
                }

                sampling_params = self._create_sampling_params()
                request_id = f"page-{page_index}-{uuid.uuid4()}"

                # 流式收集结果
                final_text: str = ""
                async for request_output in self._async_engine.generate(
                    request, sampling_params, request_id
                ):
                    if request_output.outputs:
                        final_text = request_output.outputs[0].text

                logger.debug(f"页 {page_index}: 异步OCR完成, 输出长度={len(final_text)}")

                return OCRResult(
                    raw_text=final_text,
                    page_index=page_index,
                    success=True,
                )

            except Exception as e:
                last_error = str(e)
                logger.warning(
                    f"页 {page_index}: 异步OCR失败 (尝试 {attempt + 1}/{self.config.max_retries}): {last_error}"
                )
                if attempt < self.config.max_retries - 1:
                    delay: float = self.config.retry_delay * (2 ** attempt)
                    logger.info(f"等待 {delay:.1f}s 后重试...")
                    await asyncio.sleep(delay)

        logger.error(f"页 {page_index}: 异步OCR最终失败: {last_error}")
        return OCRResult(
            raw_text="",
            page_index=page_index,
            success=False,
            error_msg=last_error,
        )
