"""
接口协议（Protocol）

用 typing.Protocol 定义松耦合接口，各模块只依赖协议而非具体实现。
"""

from __future__ import annotations
from typing import Protocol, Optional, runtime_checkable, TYPE_CHECKING

if TYPE_CHECKING:
    from .models import BookStructure, ProcessingUnit


# ── 结构解析策略 ──

@runtime_checkable
class StructureStrategy(Protocol):
    """结构解析策略接口 — 三路策略（epub / llm / regex）各自实现"""

    name: str

    def analyze(self, text: str, metadata: dict) -> Optional[BookStructure]:
        """
        分析文本，返回书的树形结构。

        Args:
            text: 全文文本
            metadata: 提取阶段产出的元数据（如 epub_toc、书名等）

        Returns:
            成功则返回 BookStructure，失败返回 None（交由下一策略）
        """
        ...


# ── 管线阶段 ──

@runtime_checkable
class Stage(Protocol):
    """管线阶段接口 — 每个 Stage 实现此协议即可插入 Pipeline"""

    name: str

    def process(self, context: "PipelineContext") -> None:
        """
        执行本阶段处理，结果写入 context。

        Args:
            context: 管线共享状态，包含 Book、BookStructure、units、BookMind 等
        """
        ...


# ── 文本提取器 ──

@runtime_checkable
class Extractor(Protocol):
    """文本提取器接口 — EPUB / PDF 各自实现"""

    supported_extensions: list[str]

    def extract(self, file_path: str) -> "ExtractionResult":
        """
        从文件提取文本和元数据。

        Args:
            file_path: 输入文件路径

        Returns:
            ExtractionResult 包含 text + metadata（可选含原生 TOC）
        """
        ...


# ── LLM 客户端 ──

@runtime_checkable
class LLMClientProtocol(Protocol):
    """LLM 调用接口"""

    def chat(
        self,
        messages: list[dict],
        max_tokens: int = 4096,
        temperature: float = 0.2,
    ) -> str:
        """发送对话请求，返回模型回复文本"""
        ...
