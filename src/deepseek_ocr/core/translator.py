# -*- coding: utf-8 -*-
"""
Business Logic:
    通过 OpenAI 兼容接口调用 LLM 将 OCR 解析出的文本块翻译为目标语言。
    跳过公式、图片、表格等不需要翻译的区域，仅翻译纯文本和标题等块。
    翻译结果保留原始坐标和标签信息，可直接用于后续 PDF 渲染。

Code Logic:
    将可翻译的 TextBlock 按编号组装成批量 prompt 一次性发送给 LLM，
    从响应中按编号正则解析出翻译结果。
    如果编号解析数量不匹配，回退为逐 block 单独翻译。
    提供同步和异步两套接口，支持重试机制(指数退避)。
"""

import asyncio
import re
import time
from dataclasses import dataclass

from openai import OpenAI, AsyncOpenAI

from deepseek_ocr.config import TranslationConfig
from deepseek_ocr.core.output_parser import ParsedPage, TextBlock
from deepseek_ocr.utils.logger import logger


# 这些标签类型的 block 不进行翻译，保留原文
_SKIP_LABELS: frozenset[str] = frozenset({"formula", "equation", "image", "table"})

# 从 LLM 响应中按编号提取翻译文本的正则
_NUMBERED_RESPONSE_PATTERN: re.Pattern[str] = re.compile(
    r"\[(\d+)\]\s*(.+?)(?=\[\d+\]|\Z)",
    re.DOTALL,
)


@dataclass
class TranslatedPage:
    """单页翻译结果"""
    original: ParsedPage                    # 原始解析页
    translated_blocks: list[TextBlock]      # 翻译后的文本块列表，与 original.blocks 一一对应
    page_index: int
    success: bool
    error_msg: str | None = None


class Translator:
    """LLM 翻译引擎，通过 OpenAI 兼容接口将 OCR 文本翻译为目标语言"""

    RETRY_DELAY: float = 2.0

    def __init__(self, config: TranslationConfig) -> None:
        """
        Business Logic:
            初始化翻译引擎，建立与 LLM 服务的连接。
            使用 OpenAI 兼容接口，支持任意兼容后端（OpenAI / DeepSeek / 本地部署等）。

        Code Logic:
            根据 TranslationConfig 创建同步和异步 OpenAI 客户端，
            保存模型名称、温度和重试配置。
        """
        self._config: TranslationConfig = config
        self._client: OpenAI = OpenAI(
            base_url=config.base_url,
            api_key=config.api_key,
            timeout=config.timeout,
        )
        self._async_client: AsyncOpenAI = AsyncOpenAI(
            base_url=config.base_url,
            api_key=config.api_key,
            timeout=config.timeout,
        )
        logger.info(
            f"Translator初始化: base_url={config.base_url}, model={config.model}"
        )

    def translate_page(
        self,
        parsed_page: ParsedPage,
        source_lang: str,
        target_lang: str,
    ) -> TranslatedPage:
        """
        Business Logic:
            同步翻译单页中所有可翻译的文本块。
            跳过公式、图片、表格等特殊区域，仅翻译纯文本内容。
            翻译失败时保留原文，不会因部分失败导致整页丢失。

        Code Logic:
            1. 筛选出需要翻译的 blocks 及其索引
            2. 批量组装 prompt 调用 LLM
            3. 解析编号化响应，数量不匹配时回退逐 block 翻译
            4. 构建 translated_blocks（深拷贝 + 替换翻译文本）
        """
        page_index: int = parsed_page.page_index
        blocks: list[TextBlock] = parsed_page.blocks

        # 筛选可翻译的 blocks
        translatable_indices: list[int] = []
        translatable_blocks: list[TextBlock] = []
        for i, block in enumerate(blocks):
            if block.label not in _SKIP_LABELS and block.text.strip():
                translatable_indices.append(i)
                translatable_blocks.append(block)

        # 构建深拷贝的 blocks 列表作为基础
        translated_blocks: list[TextBlock] = [
            TextBlock(text=b.text, label=b.label, bbox=list(b.bbox))
            for b in blocks
        ]

        # 无可翻译内容，直接返回
        if not translatable_blocks:
            logger.debug(f"页 {page_index}: 无可翻译文本块，跳过翻译")
            return TranslatedPage(
                original=parsed_page,
                translated_blocks=translated_blocks,
                page_index=page_index,
                success=True,
            )

        logger.info(
            f"页 {page_index}: 共 {len(blocks)} 个块, "
            f"{len(translatable_blocks)} 个需要翻译"
        )

        try:
            # 批量翻译
            prompt: str = self._build_batch_prompt(
                translatable_blocks, translatable_indices, source_lang, target_lang
            )
            response_text: str = self._call_llm_sync(prompt)
            translations: dict[int, str] = self._parse_numbered_response(
                response_text, len(translatable_blocks)
            )

            # 检查解析结果数量是否匹配
            if len(translations) != len(translatable_blocks):
                logger.warning(
                    f"页 {page_index}: 编号解析数量不匹配 "
                    f"(期望 {len(translatable_blocks)}, 实际 {len(translations)}), "
                    f"回退为逐 block 翻译"
                )
                # 回退：逐 block 单独翻译
                for seq, (orig_idx, block) in enumerate(
                    zip(translatable_indices, translatable_blocks)
                ):
                    try:
                        translated_text: str = self._translate_single_block(
                            block.text, source_lang, target_lang
                        )
                        translated_blocks[orig_idx] = TextBlock(
                            text=translated_text,
                            label=block.label,
                            bbox=list(block.bbox),
                        )
                    except Exception as e:
                        logger.warning(
                            f"页 {page_index}, 块 {orig_idx}: 单独翻译失败, 保留原文: {e}"
                        )
            else:
                # 批量翻译成功，按编号映射回原始索引
                for seq, orig_idx in enumerate(translatable_indices):
                    # seq 从 0 开始，编号从 1 开始
                    translated_text = translations.get(seq + 1, "")
                    if translated_text:
                        translated_blocks[orig_idx] = TextBlock(
                            text=translated_text.strip(),
                            label=blocks[orig_idx].label,
                            bbox=list(blocks[orig_idx].bbox),
                        )

            logger.info(f"页 {page_index}: 翻译完成")
            return TranslatedPage(
                original=parsed_page,
                translated_blocks=translated_blocks,
                page_index=page_index,
                success=True,
            )

        except Exception as e:
            logger.error(f"页 {page_index}: 翻译整体失败: {e}")
            return TranslatedPage(
                original=parsed_page,
                translated_blocks=translated_blocks,
                page_index=page_index,
                success=False,
                error_msg=str(e),
            )

    async def translate_page_async(
        self,
        parsed_page: ParsedPage,
        source_lang: str,
        target_lang: str,
    ) -> TranslatedPage:
        """
        Business Logic:
            异步翻译单页中所有可翻译的文本块。
            用于 Web 界面等需要并发处理的场景，避免阻塞事件循环。

        Code Logic:
            逻辑与同步版本相同，使用异步 OpenAI 客户端和 asyncio.sleep。
        """
        page_index: int = parsed_page.page_index
        blocks: list[TextBlock] = parsed_page.blocks

        # 筛选可翻译的 blocks
        translatable_indices: list[int] = []
        translatable_blocks: list[TextBlock] = []
        for i, block in enumerate(blocks):
            if block.label not in _SKIP_LABELS and block.text.strip():
                translatable_indices.append(i)
                translatable_blocks.append(block)

        # 构建深拷贝的 blocks 列表作为基础
        translated_blocks: list[TextBlock] = [
            TextBlock(text=b.text, label=b.label, bbox=list(b.bbox))
            for b in blocks
        ]

        # 无可翻译内容，直接返回
        if not translatable_blocks:
            logger.debug(f"页 {page_index}: 无可翻译文本块，跳过翻译")
            return TranslatedPage(
                original=parsed_page,
                translated_blocks=translated_blocks,
                page_index=page_index,
                success=True,
            )

        logger.info(
            f"页 {page_index}: 共 {len(blocks)} 个块, "
            f"{len(translatable_blocks)} 个需要翻译"
        )

        try:
            # 批量翻译
            prompt: str = self._build_batch_prompt(
                translatable_blocks, translatable_indices, source_lang, target_lang
            )
            response_text: str = await self._call_llm_async(prompt)
            translations: dict[int, str] = self._parse_numbered_response(
                response_text, len(translatable_blocks)
            )

            # 检查解析结果数量是否匹配
            if len(translations) != len(translatable_blocks):
                logger.warning(
                    f"页 {page_index}: 编号解析数量不匹配 "
                    f"(期望 {len(translatable_blocks)}, 实际 {len(translations)}), "
                    f"回退为逐 block 翻译"
                )
                # 回退：逐 block 单独翻译
                for seq, (orig_idx, block) in enumerate(
                    zip(translatable_indices, translatable_blocks)
                ):
                    try:
                        translated_text: str = await self._translate_single_block_async(
                            block.text, source_lang, target_lang
                        )
                        translated_blocks[orig_idx] = TextBlock(
                            text=translated_text,
                            label=block.label,
                            bbox=list(block.bbox),
                        )
                    except Exception as e:
                        logger.warning(
                            f"页 {page_index}, 块 {orig_idx}: 单独翻译失败, 保留原文: {e}"
                        )
            else:
                # 批量翻译成功，按编号映射回原始索引
                for seq, orig_idx in enumerate(translatable_indices):
                    translated_text = translations.get(seq + 1, "")
                    if translated_text:
                        translated_blocks[orig_idx] = TextBlock(
                            text=translated_text.strip(),
                            label=blocks[orig_idx].label,
                            bbox=list(blocks[orig_idx].bbox),
                        )

            logger.info(f"页 {page_index}: 异步翻译完成")
            return TranslatedPage(
                original=parsed_page,
                translated_blocks=translated_blocks,
                page_index=page_index,
                success=True,
            )

        except Exception as e:
            logger.error(f"页 {page_index}: 异步翻译整体失败: {e}")
            return TranslatedPage(
                original=parsed_page,
                translated_blocks=translated_blocks,
                page_index=page_index,
                success=False,
                error_msg=str(e),
            )

    def _build_batch_prompt(
        self,
        blocks: list[TextBlock],
        indices: list[int],
        source_lang: str,
        target_lang: str,
    ) -> str:
        """
        Business Logic:
            将多个文本块组装成编号化的批量翻译 prompt，
            一次 API 调用完成所有块的翻译，减少调用次数和延迟。

        Code Logic:
            按序号 [1], [2], ... 编排每个 block 的文本，
            在 prompt 开头附加翻译规则说明。
        """
        lines: list[str] = [
            f"Translate the following numbered text segments from {source_lang} to {target_lang}.",
            "",
            "Rules:",
            "1. Return ONLY the translated text in the same numbered format [N].",
            r"2. Keep mathematical expressions (LaTeX formulas like \(...\), \[...\], $...$) unchanged.",
            "3. Keep proper nouns and technical terms accurate.",
            "4. Maintain the original formatting within each segment.",
            "5. Do not add explanations or notes.",
            "",
        ]

        for seq, block in enumerate(blocks, start=1):
            lines.append(f"[{seq}] {block.text}")

        return "\n".join(lines)

    def _parse_numbered_response(
        self,
        response_text: str,
        expected_count: int,
    ) -> dict[int, str]:
        """
        Business Logic:
            从 LLM 返回的编号化文本中解析出每个编号对应的翻译结果。
            编号与 prompt 中的 [N] 一一对应。

        Code Logic:
            使用正则 \\[(\\d+)\\]\\s*(.+?)(?=\\[\\d+\\]|\\Z) 匹配编号和内容，
            返回 {编号: 翻译文本} 字典。
        """
        result: dict[int, str] = {}
        matches: list[tuple[str, str]] = _NUMBERED_RESPONSE_PATTERN.findall(response_text)

        for num_str, text in matches:
            num: int = int(num_str)
            cleaned: str = text.strip()
            if cleaned:
                result[num] = cleaned

        logger.debug(
            f"编号解析: 期望 {expected_count} 个, 实际解析到 {len(result)} 个"
        )
        return result

    def _call_llm_sync(self, prompt: str) -> str:
        """
        Business Logic:
            同步调用 LLM API，包含重试机制应对临时故障。

        Code Logic:
            使用 OpenAI chat completions 接口，
            失败时进行最多 max_retries 次重试，每次等待时间指数递增（2s, 4s, 8s...）。
        """
        last_error: Exception | None = None

        for attempt in range(self._config.max_retries):
            try:
                logger.debug(
                    f"LLM调用 (尝试 {attempt + 1}/{self._config.max_retries})"
                )
                response = self._client.chat.completions.create(
                    model=self._config.model,
                    temperature=self._config.temperature,
                    messages=[{"role": "user", "content": prompt}],
                )
                content: str = response.choices[0].message.content or ""
                logger.debug(f"LLM响应长度: {len(content)}")
                return content

            except Exception as e:
                last_error = e
                logger.warning(
                    f"LLM调用失败 (尝试 {attempt + 1}/{self._config.max_retries}): {e}"
                )
                if attempt < self._config.max_retries - 1:
                    delay: float = self.RETRY_DELAY * (2 ** attempt)
                    logger.info(f"等待 {delay:.1f}s 后重试...")
                    time.sleep(delay)

        raise RuntimeError(
            f"LLM调用在 {self._config.max_retries} 次重试后最终失败: {last_error}"
        )

    async def _call_llm_async(self, prompt: str) -> str:
        """
        Business Logic:
            异步调用 LLM API，用于 Web 场景避免阻塞事件循环。

        Code Logic:
            使用 AsyncOpenAI chat completions 接口，
            失败时使用 asyncio.sleep 进行非阻塞的指数退避重试。
        """
        last_error: Exception | None = None

        for attempt in range(self._config.max_retries):
            try:
                logger.debug(
                    f"异步LLM调用 (尝试 {attempt + 1}/{self._config.max_retries})"
                )
                response = await self._async_client.chat.completions.create(
                    model=self._config.model,
                    temperature=self._config.temperature,
                    messages=[{"role": "user", "content": prompt}],
                )
                content: str = response.choices[0].message.content or ""
                logger.debug(f"异步LLM响应长度: {len(content)}")
                return content

            except Exception as e:
                last_error = e
                logger.warning(
                    f"异步LLM调用失败 (尝试 {attempt + 1}/{self._config.max_retries}): {e}"
                )
                if attempt < self._config.max_retries - 1:
                    delay: float = self.RETRY_DELAY * (2 ** attempt)
                    logger.info(f"等待 {delay:.1f}s 后重试...")
                    await asyncio.sleep(delay)

        raise RuntimeError(
            f"异步LLM调用在 {self._config.max_retries} 次重试后最终失败: {last_error}"
        )

    def _translate_single_block(
        self,
        text: str,
        source_lang: str,
        target_lang: str,
    ) -> str:
        """
        Business Logic:
            回退策略：逐 block 单独翻译，当批量翻译解析失败时使用。
            每个 block 独立调用一次 API，确保翻译结果可靠。

        Code Logic:
            使用简化的 prompt 直接翻译单段文本，
            通过 _call_llm_sync 复用重试机制。
        """
        prompt: str = f"Translate from {source_lang} to {target_lang}:\n{text}"
        result: str = self._call_llm_sync(prompt)
        return result.strip()

    async def _translate_single_block_async(
        self,
        text: str,
        source_lang: str,
        target_lang: str,
    ) -> str:
        """
        Business Logic:
            回退策略的异步版本：逐 block 单独翻译。

        Code Logic:
            使用简化的 prompt 直接翻译单段文本，
            通过 _call_llm_async 复用异步重试机制。
        """
        prompt: str = f"Translate from {source_lang} to {target_lang}:\n{text}"
        result: str = await self._call_llm_async(prompt)
        return result.strip()
