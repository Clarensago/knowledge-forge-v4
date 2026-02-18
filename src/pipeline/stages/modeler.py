"""
Stage 2: 逐章双轮建模

Round 1: 初稿生成 + 质量验证
Round 2: 定向修补或全文精修

复用 v3 的 chapter_modeler + chapter_refiner + chapter_router 核心逻辑，
prompt 模板从配置加载。

v4.1 改进：增量输出 — 每完成一个 group 立即触发 Assembler 合并到 outbox。
"""

import threading
import time
import logging
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

from ..context import PipelineContext
from ...core.models import ProcessingUnit, BookMind, ChapterRoute
from ...core.enums import ProcessingStatus, ChapterType, RouteLevel
from ...infra.config import Config
from ...infra.file_store import FileStore
from ...infra.progress import ProgressManager
from ...llm.client import LLMClient
from ...llm.prompt_builder import PromptBuilder
from ...llm.router import ChapterRouter
from ...quality.validator import validate_output, get_quality_score, detailed_validate

logger = logging.getLogger("knowledge-forge.stage2")


class ModelerStage:
    """Stage 2: 逐章双轮建模（支持增量输出）"""

    name = "modeler"

    def __init__(self, config: Config, llm_client: LLMClient,
                 file_store: FileStore, progress: ProgressManager):
        self.config = config
        self.llm = llm_client
        self.file_store = file_store
        self.progress = progress
        self.prompt_builder = PromptBuilder(config)
        self.router = ChapterRouter(config)

        # 增量输出相关
        self._assembler = None          # 由 PipelineEngine 注入
        self._group_map: dict[str, list[ProcessingUnit]] = {}
        self._unit_to_group: dict[str, str] = {}
        self._emitted_groups: set[str] = set()
        self._emit_lock = threading.Lock()

    def set_assembler(self, assembler) -> None:
        """注入 Assembler 实例，启用增量输出"""
        self._assembler = assembler

    def _build_group_map(self, units: list[ProcessingUnit]) -> None:
        """预计算 unit → group 映射，复用 Assembler 的分组逻辑"""
        if self._assembler:
            from .assembler import AssemblerStage
            groups = self._assembler._group_by_part(
                units, getattr(self, '_current_structure', None)
            )
            self._group_map = groups
            self._unit_to_group = {}
            for gid, gunits in groups.items():
                for u in gunits:
                    self._unit_to_group[u.id] = gid
            self._emitted_groups = set()
            logger.info(f"增量输出就绪: {len(groups)} 个分组")
        else:
            self._group_map = {}
            self._unit_to_group = {}

    def _check_and_emit_group(self, unit: ProcessingUnit, book) -> None:
        """检查 unit 所属分组是否就绪，如果是则触发增量输出"""
        if not self._assembler or not self._group_map:
            return

        group_id = self._unit_to_group.get(unit.id)
        if not group_id:
            return

        with self._emit_lock:
            if group_id in self._emitted_groups:
                return

            group_units = self._group_map.get(group_id, [])
            if not group_units:
                return

            # 检查分组内所有 unit 是否都已完成（STAGE2_DONE 或更高）
            ready = all(
                u.status in (
                    ProcessingStatus.STAGE2_DONE,
                    ProcessingStatus.STAGE2_5_DONE,
                    ProcessingStatus.STAGE3_DONE,
                )
                for u in group_units
            )
            if not ready:
                return

            # 触发增量输出
            self._emitted_groups.add(group_id)

        # 在锁外执行 I/O
        try:
            success = self._assembler.assemble_one_group(
                book, group_id, group_units,
                getattr(self, '_current_structure', None),
            )
            if success:
                self.progress.save_book(book)
        except Exception as e:
            logger.warning(f"增量输出 [{group_id}] 失败: {e}")

    def process(self, context: PipelineContext):
        book = context.book
        book_mind = context.book_mind or book.book_mind
        units = book.units

        if not book_mind:
            raise RuntimeError("BookMind 未构建，无法执行 Stage 2")

        # 缓存 structure 供增量输出使用
        self._current_structure = book.structure or context.structure

        # 构建分组映射（用于增量输出）
        self._build_group_map(units)

        # Round 1: 初稿生成
        round1_units = [
            u for u in units
            if u.status in (ProcessingStatus.PENDING, ProcessingStatus.STAGE0_DONE)
            and u.is_processable
        ]
        if round1_units:
            logger.info(f"Stage 2 Round 1: {len(round1_units)} 个单元待处理")
            self._run_round(round1_units, book, book_mind, context, round_num=1)

        # Round 2: 精修（默认关闭）
        round2_units = [
            u for u in units
            if u.status == ProcessingStatus.STAGE2_ROUND1 and u.is_processable
        ]
        if round2_units and self.config.dual_round:
            logger.info(f"Stage 2 Round 2: {len(round2_units)} 个单元待精修")
            self._run_round(round2_units, book, book_mind, context, round_num=2)

        # 标记所有 ROUND1 单元为 DONE（没有 Round 2 的情况）
        for u in units:
            if u.status == ProcessingStatus.STAGE2_ROUND1:
                u.round2_output = u.round1_output
                u.status = ProcessingStatus.STAGE2_DONE
                # 触发可能的增量输出
                self._check_and_emit_group(u, book)

        book.status = ProcessingStatus.STAGE2_DONE
        self.progress.save_book(book)
        logger.info("Stage 2 完成")

    def _run_round(self, units: list[ProcessingUnit], book, book_mind,
                   context: PipelineContext, round_num: int):
        """执行一轮处理（支持并发）"""
        concurrency = self.config.concurrency
        total = len(units)

        if concurrency <= 1:
            for i, unit in enumerate(units, 1):
                if context.check_stop():
                    break
                context.check_pause()
                logger.info(f"Round {round_num}: [{unit.id}] "
                            f"{unit.title} ({i}/{total}) ({unit.word_count}字)")
                if context.on_unit_start:
                    context.on_unit_start(unit.title, i, total)
                self._process_one(unit, book, book_mind, round_num)
                if context.on_unit_done:
                    context.on_unit_done()
        else:
            submitted = 0
            with ThreadPoolExecutor(max_workers=concurrency) as pool:
                futures = {}
                for unit in units:
                    if context.check_stop():
                        break
                    submitted += 1
                    if context.on_unit_start:
                        context.on_unit_start(unit.title, submitted, total)
                    f = pool.submit(self._process_one, unit, book, book_mind, round_num)
                    futures[f] = unit
                for f in as_completed(futures):
                    unit = futures[f]
                    try:
                        f.result()
                    except Exception as e:
                        logger.error(f"[{unit.id}] 处理失败: {e}")
                    if context.on_unit_done:
                        context.on_unit_done()

    def _process_one(self, unit: ProcessingUnit, book, book_mind: BookMind, round_num: int):
        """处理单个 ProcessingUnit"""
        route = self.router.route(unit, book_mind.chapter_metas.get(unit.id))
        max_retries = self.config.llm_max_retries

        if round_num == 1:
            self._round1(unit, book, book_mind, route, max_retries)
        else:
            self._round2(unit, book, book_mind, route, max_retries)

        self.progress.save_book(book)

        # 增量输出：如果 dual_round 关闭且 Round 1 完成，标记为 DONE 并尝试输出
        if round_num == 1 and not self.config.dual_round and unit.status == ProcessingStatus.STAGE2_ROUND1:
            unit.round2_output = unit.round1_output
            unit.status = ProcessingStatus.STAGE2_DONE
            self._check_and_emit_group(unit, book)
        elif round_num == 2 and unit.status == ProcessingStatus.STAGE2_DONE:
            self._check_and_emit_group(unit, book)

    def _round1(self, unit: ProcessingUnit, book, book_mind: BookMind,
                route: ChapterRoute, max_retries: int):
        """Round 1: 初稿生成"""
        unit_context = book_mind.get_unit_context(unit.id)
        system_prompt, user_prompt = self.prompt_builder.build_chapter_prompt(
            book_name=book.name,
            unit=unit,
            book_mind_context=unit_context,
        )

        for attempt in range(1, max_retries + 1):
            try:
                logger.info(f"  尝试 {attempt}/{max_retries}...")
                output = self.llm.chat(
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    temperature=route.temperature,
                    max_tokens=route.max_tokens,
                    stream=self.config.stream,
                    task_label=f"{unit.id}",
                )

                passed, reason = validate_output(output, unit.chapter_type)
                if passed:
                    unit.round1_output = output
                    unit.quality_score = get_quality_score(output, unit.chapter_type)
                    unit.status = ProcessingStatus.STAGE2_ROUND1
                    logger.info(f"  [{unit.id}] Round 1 通过 "
                                f"(质量={unit.quality_score:.2f})")
                    return
                else:
                    logger.warning(f"  [{unit.id}] 验证未通过: {reason}")
                    unit.retry_count += 1

            except (ConnectionError, TimeoutError) as e:
                logger.warning(f"  [{unit.id}] 网络错误: {e}")
                unit.retry_count += 1
                time.sleep(self.config.llm_retry_delay * attempt)
            except Exception as e:
                logger.error(f"  [{unit.id}] 异常: {e}")
                unit.error_message = str(e)
                unit.status = ProcessingStatus.FAILED
                return

        unit.error_message = "超过最大重试次数"
        unit.status = ProcessingStatus.FAILED

    def _round2(self, unit: ProcessingUnit, book, book_mind: BookMind,
                route: ChapterRoute, max_retries: int):
        """Round 2: 定向修补或全文精修"""
        if route.skip_round2:
            unit.round2_output = unit.round1_output
            unit.status = ProcessingStatus.STAGE2_DONE
            return

        # 智能跳过高质量初稿
        if (self.config.smart_skip_enabled
                and unit.quality_score >= self.config.smart_skip_threshold):
            logger.info(f"  [{unit.id}] 质量 {unit.quality_score:.2f} ≥ 阈值，跳过 Round 2")
            unit.round2_output = unit.round1_output
            unit.status = ProcessingStatus.STAGE2_DONE
            return

        # 定向修补
        defects = detailed_validate(unit.round1_output, unit.chapter_type)
        if not defects:
            unit.round2_output = unit.round1_output
            unit.status = ProcessingStatus.STAGE2_DONE
            return

        system_prompt, user_prompt = self.prompt_builder.build_fix_prompt(
            unit=unit, defects=defects,
        )
        try:
            output = self.llm.chat(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.1,
                max_tokens=route.max_tokens,
                stream=self.config.stream,
                task_label=f"{unit.id}-R2",
            )
            new_score = get_quality_score(output, unit.chapter_type)
            if new_score >= unit.quality_score * 0.9:
                unit.round2_output = output
                unit.quality_score = new_score
            else:
                unit.round2_output = unit.round1_output
                logger.info(f"  [{unit.id}] Round 2 未改善，保留 Round 1")
        except Exception as e:
            logger.warning(f"  [{unit.id}] Round 2 失败: {e}，保留 Round 1")
            unit.round2_output = unit.round1_output

        unit.status = ProcessingStatus.STAGE2_DONE
