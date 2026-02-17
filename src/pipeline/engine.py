"""
Pipeline Engine — 可插拔阶段调度器

通过 Registry 注册 Stage，支持动态增减、条件跳过、断点续作。
替代 v3 orchestrator.py 的硬编码调度。
"""

import time
import logging
from typing import Optional
from pathlib import Path

from .context import PipelineContext
from ..core.models import Book
from ..core.enums import ProcessingStatus
from ..infra.config import Config
from ..infra.file_store import FileStore
from ..infra.progress import ProgressManager
from ..llm.client import LLMClient

logger = logging.getLogger("knowledge-forge.pipeline")


class PipelineEngine:
    """管线引擎 — 可插拔阶段调度"""

    def __init__(
        self,
        config: Config,
        llm_client: LLMClient,
        file_store: FileStore,
        progress: ProgressManager,
    ):
        self.config = config
        self.llm = llm_client
        self.file_store = file_store
        self.progress = progress
        self._stages: list[tuple[str, object]] = []

    def register_stage(self, name: str, stage):
        """注册一个 Stage"""
        self._stages.append((name, stage))
        logger.debug(f"注册阶段: {name}")

    def run(self, source_file: str, context: Optional[PipelineContext] = None) -> bool:
        """
        运行完整管线。

        Args:
            source_file: 输入文件路径
            context: 可选外部 context（Web UI 会注入 pause_check 回调）

        Returns:
            是否成功完成
        """
        if context is None:
            context = PipelineContext()
        context.source_file = source_file

        filename = Path(source_file).name
        safe_name = FileStore.sanitize_dirname(filename)

        # 断点恢复
        book = self.progress.get_book(safe_name)
        if not book:
            book = Book(
                name=filename,
                source_file=str(source_file),
                safe_name=safe_name,
            )
        context.book = book

        logger.info(f"{'=' * 60}")
        logger.info(f"开始处理: {filename}")
        logger.info(f"当前状态: {book.status.value}")
        logger.info(f"注册阶段: {[name for name, _ in self._stages]}")
        logger.info(f"{'=' * 60}")

        start_time = time.time()

        for stage_name, stage in self._stages:
            if context.check_stop():
                logger.info("收到停止信号，终止管线")
                return False

            # 断点跳过已完成的阶段
            if self._should_skip(book, stage_name):
                logger.info(f"跳过已完成阶段: {stage_name}")
                continue

            context.check_pause()

            logger.info(f"{'─' * 40}")
            logger.info(f"执行阶段: {stage_name}")
            stage_start = time.time()

            try:
                stage.process(context)
                elapsed = time.time() - stage_start
                logger.info(f"阶段完成: {stage_name} ({elapsed:.1f}s)")
            except Exception as e:
                logger.error(f"阶段失败: {stage_name} — {e}", exc_info=True)
                book.status = ProcessingStatus.FAILED
                self.progress.save_book(book)
                return False

        total_time = time.time() - start_time
        logger.info(f"{'=' * 60}")
        logger.info(f"处理完成: {filename} ({total_time:.1f}s)")
        logger.info(f"LLM 用量: {self.llm.usage.summary()}")
        logger.info(f"{'=' * 60}")
        return True

    def _should_skip(self, book: Book, stage_name: str) -> bool:
        """检查某阶段是否已完成"""
        stage_map = {
            "preprocess": ProcessingStatus.STAGE0_DONE,
            "mind_builder": ProcessingStatus.STAGE1_DONE,
            "modeler": ProcessingStatus.STAGE2_DONE,
            "assembler": ProcessingStatus.STAGE2_5_DONE,
            "retrospector": ProcessingStatus.STAGE3_DONE,
        }
        target = stage_map.get(stage_name)
        if not target:
            return False
        try:
            status_list = list(ProcessingStatus)
            return status_list.index(book.status) >= status_list.index(target)
        except ValueError:
            return False
