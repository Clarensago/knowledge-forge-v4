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

    # 停止检查回调（可选，优先于 should_stop 静态字段）
    stop_check: Optional[Callable[[], bool]] = None

    # Stage 进入回调（通知外部当前 stage 名）
    on_stage_enter: Optional[Callable[[str], None]] = None

    # 章节级进度回调（通知外部正在处理的 unit 标题、序号/总数）
    on_unit_start: Optional[Callable[[str, int, int], None]] = None

    # unit 完成回调（通知外部一个 unit 已完成）
    on_unit_done: Optional[Callable[[], None]] = None

    # 子步骤进度回调（stage_name, step_idx, step_total, step_label）
    on_substep: Optional[Callable[[str, int, int, str], None]] = None

    def check_pause(self):
        """检查是否需要暂停，供各 Stage 调用"""
        if self.pause_check:
            self.pause_check()

    def check_stop(self) -> bool:
        """检查是否需要停止"""
        if self.stop_check:
            return self.stop_check()
        return self.should_stop

    def notify_substep(self, stage_name: str, step_idx: int, step_total: int, label: str = ""):
        """通知外部子步骤进度"""
        if self.on_substep:
            self.on_substep(stage_name, step_idx, step_total, label)
