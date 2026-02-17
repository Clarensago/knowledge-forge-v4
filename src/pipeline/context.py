"""
PipelineContext — 管线共享状态容器

持有 Book、BookStructure、ProcessingUnit[]、BookMind 等阶段间共享数据。
所有 Stage 通过 context 交换数据，不直接耦合。
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional, Callable

from ..core.models import Book, BookStructure, ProcessingUnit, BookMind, ExtractionResult


@dataclass
class PipelineContext:
    """管线共享状态"""

    # 基本信息
    book: Optional[Book] = None
    source_file: str = ""

    # Stage 0 产出
    extraction_result: Optional[ExtractionResult] = None
    full_text: str = ""
    structure: Optional[BookStructure] = None
    units: list[ProcessingUnit] = field(default_factory=list)

    # Stage 1 产出
    book_mind: Optional[BookMind] = None

    # 控制信号
    should_stop: bool = False

    # 暂停检查回调（由 Web TaskEngine 注入）
    pause_check: Optional[Callable[[], None]] = None

    def check_pause(self):
        """检查是否需要暂停，供各 Stage 调用"""
        if self.pause_check:
            self.pause_check()

    def check_stop(self) -> bool:
        """检查是否需要停止"""
        return self.should_stop
