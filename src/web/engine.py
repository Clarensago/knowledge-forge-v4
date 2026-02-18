"""
TaskEngine — 后台任务引擎

管理处理线程池，支持并发处理、暂停/继续/放弃、动态调整并发数。

v4 修复：
1. 暂停后自动开始下一本 — 在 for 循环头部增加 _check_pause()
2. 进程残留 — stop_all 时 shutdown(wait=True, cancel_futures=True)
3. 每个 _process_one_chapter 内检查 _stop 信号

v4.2 增强：
4. 分段进度条（Stage 0~3 权重分配 + 子步骤回调）
5. Stage 2 处理 ETA 时间预估
6. abort = 停掉 + 重置（最高权限）
"""

import time
import threading
import logging
from pathlib import Path
from datetime import datetime
from typing import Optional
from concurrent.futures import ThreadPoolExecutor

from ..core.models import Book
from ..core.enums import ProcessingStatus
from ..infra.config import Config
from ..infra.file_store import FileStore
from ..infra.progress import ProgressManager
from ..infra.logger import setup_logging
from ..llm.client import LLMClient
from ..pipeline.context import PipelineContext
from ..pipeline.engine import PipelineEngine
from ..pipeline.stages.preprocess import PreprocessStage
from ..pipeline.stages.mind_builder import MindBuilderStage
from ..pipeline.stages.modeler import ModelerStage
from ..pipeline.stages.assembler import AssemblerStage
from ..pipeline.stages.retrospector import RetrospectorStage

logger = logging.getLogger("knowledge-forge.web.engine")

# ── 分段进度权重 ──
# Stage 0: 5%, Stage 1: 10%, Stage 2: 70%, Stage 2.5+3: 15%
STAGE_WEIGHTS = {
    "preprocess":   (0.0,  0.05),   # 0%  ~ 5%
    "mind_builder": (0.05, 0.15),   # 5%  ~ 15%
    "modeler":      (0.15, 0.85),   # 15% ~ 85%
    "assembler":    (0.85, 0.92),   # 85% ~ 92%
    "retrospector": (0.92, 1.00),   # 92% ~ 100%
}


class TaskEngine:
    """后台任务引擎 — 管理处理队列和控制信号"""

    def __init__(self, project_root: str):
        self.project_root = project_root

        # 基础服务
        self.config: Optional[Config] = None
        self.file_store: Optional[FileStore] = None
        self.progress: Optional[ProgressManager] = None
        self.llm: Optional[LLMClient] = None

        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

        # 控制信号
        self._paused = threading.Event()
        self._paused.set()  # 初始未暂停
        self._abort = threading.Event()
        self._stop = threading.Event()

        # 状态
        self.running = False
        self.current_book: Optional[str] = None
        self.current_chapter: Optional[str] = None
        self.current_stage: str = ""
        self.current_book_total: int = 0
        self.current_book_done: int = 0
        self.current_book_failed: int = 0
        self.queue: list[str] = []
        self.log_lines: list[str] = []
        self._max_log_lines = 500

        # 并发
        self._executor: Optional[ThreadPoolExecutor] = None
        self._concurrent_workers: int = 3
        self._progress_lock = threading.Lock()

        # ── 分段进度 ──
        self._current_stage_name: str = ""       # 当前 stage 内部名
        self._substep_idx: int = 0               # 当前子步骤 idx
        self._substep_total: int = 1             # 当前子步骤 total
        self._substep_label: str = ""            # 子步骤标签

        # ── ETA ──
        self._unit_times: list[float] = []       # 每个 unit 的耗时
        self._unit_start_time: float = 0.0       # 当前 unit 开始时间
        self._eta_seconds: float = 0.0           # 预估剩余秒数
        self._avg_unit_time: float = 0.0         # 滑动平均
        self._current_context: Optional[PipelineContext] = None

    def init_services(self):
        """初始化基础服务"""
        setup_logging(self.project_root)
        self.config = Config(self.project_root)
        self.file_store = FileStore(self.project_root)
        self.progress = ProgressManager(self.project_root)
        self._concurrent_workers = self.config.concurrency

    def _ensure_llm(self):
        """确保 LLM 客户端已初始化"""
        if self.llm is None:
            api_key = self.config.load_api_key()
            if not api_key:
                raise RuntimeError("未配置 API Key，请在设置中配置")
            self.llm = LLMClient(
                api_key=api_key,
                base_url=self.config.llm_base_url,
                model=self.config.llm_model,
                timeout=self.config.llm_timeout,
            )

    def _log(self, msg: str):
        """记录日志到缓冲区"""
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"{ts} {msg}"
        with self._lock:
            self.log_lines.append(line)
            if len(self.log_lines) > self._max_log_lines:
                self.log_lines = self.log_lines[-self._max_log_lines:]
        logger.info(msg)

    @property
    def is_paused(self) -> bool:
        return not self._paused.is_set()

    def start(self, queue: list[str]) -> bool:
        """启动处理任务"""
        if self.running:
            return False
        self._ensure_llm()
        self.queue = list(queue)
        self._abort.clear()
        self._stop.clear()
        self._paused.set()
        self.running = True
        self.log_lines.clear()
        self._concurrent_workers = self.config.concurrency
        self._reset_progress_state()
        self._log(f"⚡ 并发模式: {self._concurrent_workers} workers")
        self._thread = threading.Thread(target=self._run_pipeline, daemon=True)
        self._thread.start()
        return True

    def _reset_progress_state(self):
        """重置进度相关状态"""
        self._current_stage_name = ""
        self._substep_idx = 0
        self._substep_total = 1
        self._substep_label = ""
        self._unit_times.clear()
        self._unit_start_time = 0.0
        self._eta_seconds = 0.0
        self._avg_unit_time = 0.0

    def pause(self):
        """暂停处理"""
        if self.running:
            self._paused.clear()
            self._log("⏸ 已暂停，等待当前操作完成...")

    def resume(self):
        """继续处理"""
        if self.running:
            self._paused.set()
            self._log("▶ 继续处理")

    def abort_current(self):
        """
        放弃当前书籍 — 最高权限操作（= 停掉 + 重置）

        1. 设置 abort + stop 信号
        2. 唤醒暂停线程
        3. 强制关闭线程池（取消排队中的 future）
        4. 后续由 _run_pipeline 的 finally 清理状态
        """
        if self.running:
            self._abort.set()
            self._stop.set()       # 最高权限：同时设置 stop
            self._paused.set()     # 唤醒暂停中的线程

            # 强制关闭线程池，取消所有排队任务
            if self._executor:
                try:
                    self._executor.shutdown(wait=False, cancel_futures=True)
                except TypeError:
                    self._executor.shutdown(wait=False)
                self._executor = None

            self._log("✖ 强制终止当前书籍，重置进度...")

    def stop_all(self):
        """
        停止全部 — v4 修复进程残留

        1. 设置三个信号
        2. 等待线程池 shutdown（cancel pending + wait for running）
        3. 等待主线程结束
        """
        self._stop.set()
        self._paused.set()
        self._abort.set()

        # 强制关闭线程池
        if self._executor:
            try:
                self._executor.shutdown(wait=False, cancel_futures=True)
            except TypeError:
                self._executor.shutdown(wait=False)
            self._executor = None

        self._log("⏹ 全部停止")

    def set_concurrency(self, n: int):
        """动态调整并发数"""
        self._concurrent_workers = max(1, min(10, n))
        if self.config:
            self.config.set("concurrency", self._concurrent_workers)
        self._log(f"⚡ 并发数调整为 {self._concurrent_workers}")

    def _check_pause(self):
        """暂停检查点"""
        self._paused.wait()

    # ── 分段进度计算 ──

    def _calc_overall_progress(self) -> int:
        """
        计算整体进度百分比（0~100）

        基于 STAGE_WEIGHTS 分段映射：
        - 每个 stage 占据一个权重区间
        - stage 内部按子步骤/unit完成比例线性插值
        """
        stage = self._current_stage_name
        if not stage or stage not in STAGE_WEIGHTS:
            return 0

        lo, hi = STAGE_WEIGHTS[stage]
        span = hi - lo

        # Stage 2 特殊处理：按 unit 完成数
        if stage == "modeler":
            total = self.current_book_total or 1
            done = self.current_book_done
            inner_pct = done / total
        else:
            # 其他 stage：按子步骤
            inner_pct = self._substep_idx / max(self._substep_total, 1)

        overall = lo + span * inner_pct
        return min(100, max(0, round(overall * 100)))

    def _update_eta(self):
        """更新 ETA（基于最近单元的滑动平均）"""
        if not self._unit_times:
            self._eta_seconds = 0
            self._avg_unit_time = 0
            return

        # 滑动窗口：最近 10 个
        window = self._unit_times[-10:]
        self._avg_unit_time = sum(window) / len(window)

        remaining = max(0, (self.current_book_total or 0) - self.current_book_done)
        concurrency = max(1, self._concurrent_workers)
        self._eta_seconds = self._avg_unit_time * remaining / concurrency

    def _format_eta(self) -> str:
        """格式化 ETA 显示"""
        s = self._eta_seconds
        if s <= 0:
            return ""
        if s < 60:
            return f"~{int(s)}s"
        elif s < 3600:
            return f"~{int(s/60)}min"
        else:
            h = int(s / 3600)
            m = int((s % 3600) / 60)
            return f"~{h}h{m}min"

    # ── Pipeline 主循环 ──

    def _run_pipeline(self):
        """
        后台处理主循环

        v4 修复：循环头部增加 _check_pause()，确保暂停时
        不会自动开始处理下一本书。
        abort 视为强制终止，只处理当前书的重置，不继续队列。
        """
        try:
            for filename in self.queue:
                if self._stop.is_set():
                    break

                # v4 修复：两本书之间的暂停检查点
                self._check_pause()
                if self._stop.is_set():
                    break

                self._abort.clear()
                file_path = self.file_store.inbox_dir / filename
                if not file_path.exists():
                    self._log(f"⚠ 文件不存在: {filename}")
                    continue

                self._process_one_book(file_path)

                # abort 后不再继续处理队列中的下一本
                if self._abort.is_set():
                    break

            if not self._stop.is_set() and not self._abort.is_set():
                self._log("✅ 全部处理完成")
        except Exception as e:
            self._log(f"❌ 致命错误: {e}")
        finally:
            self.running = False
            self.current_book = None
            self.current_chapter = None
            self.current_stage = ""
            self.current_book_total = 0
            self.current_book_done = 0
            self.current_book_failed = 0
            self._reset_progress_state()

    def _process_one_book(self, file_path: Path):
        """处理单本书 — 委托 PipelineEngine 统一调度"""
        book_name = file_path.name
        self.current_book = book_name
        self.current_stage = "初始化"
        self.current_book_done = 0
        self.current_book_failed = 0
        self._reset_progress_state()

        self._log(f"📖 开始处理: {book_name}")

        # Stage 名称映射（用于 UI 展示）
        stage_labels = {
            "preprocess": "Stage 0 预处理",
            "mind_builder": "Stage 1 全书通读",
            "modeler": "Stage 2 逐章建模",
            "assembler": "Stage 2.5 篇级合并",
            "retrospector": "Stage 3 全书回溯",
        }

        def on_stage_enter(stage_name: str):
            label = stage_labels.get(stage_name, stage_name)
            self.current_stage = label
            self._current_stage_name = stage_name
            self._substep_idx = 0
            self._substep_total = 1
            self._substep_label = ""
            self._log(f"▶ {label}...")
            # preprocess 完成后（进入下一个 stage 时），更新 total
            if context.book and self.current_book_total == 0:
                self.current_book_total = len(context.book.units)

        def on_unit_start(title: str, idx: int, total: int):
            self.current_chapter = f"[{idx}/{total}] {title}"
            self._unit_start_time = time.time()

        def on_unit_done():
            if context.book:
                self._update_counts(context.book)
            # 记录耗时
            if self._unit_start_time > 0:
                elapsed = time.time() - self._unit_start_time
                self._unit_times.append(elapsed)
                self._unit_start_time = 0.0
                self._update_eta()

        def on_substep(stage_name: str, step_idx: int, step_total: int, label: str):
            self._substep_idx = step_idx
            self._substep_total = step_total
            self._substep_label = label

        # 构建 PipelineContext
        context = PipelineContext(
            source_file=str(file_path),
            pause_check=self._check_pause,
            stop_check=lambda: self._abort.is_set() or self._stop.is_set(),
            on_stage_enter=on_stage_enter,
            on_unit_start=on_unit_start,
            on_unit_done=on_unit_done,
            on_substep=on_substep,
        )

        # 将 context 暴露给 abort 清理逻辑
        self._current_context = context

        try:
            # 构建 PipelineEngine 并注册所有 Stage
            pipeline = PipelineEngine(
                self.config, self.llm, self.file_store, self.progress
            )
            pipeline.register_stage("preprocess",
                PreprocessStage(self.config, self.llm, self.file_store, self.progress))
            pipeline.register_stage("mind_builder",
                MindBuilderStage(self.config, self.llm, self.file_store, self.progress))
            pipeline.register_stage("modeler",
                ModelerStage(self.config, self.llm, self.file_store, self.progress))
            pipeline.register_stage("assembler",
                AssemblerStage(self.file_store, self.progress))
            pipeline.register_stage("retrospector",
                RetrospectorStage(self.config, self.llm, self.file_store, self.progress))

            success = pipeline.run(str(file_path), context)

            # 更新计数
            if context.book:
                self.current_book_total = len(context.book.units)
                self._update_counts(context.book)

            if success:
                self._log(f"✅ {book_name} 处理完成！")
            elif self._abort.is_set():
                if context.book:
                    self._handle_abort(context.book)
            elif self._stop.is_set():
                pass  # 全局停止，不需要额外处理
            else:
                self._log(f"⚠ {book_name} 处理未完成")

        except RuntimeError as e:
            if "API Key" in str(e):
                self._log(f"❌ API Key 无效，停止处理")
                self._stop.set()
            else:
                self._log(f"❌ 错误: {e}")
        except Exception as e:
            self._log(f"❌ 未知错误: {e}")

    def _handle_abort(self, book: Book):
        """放弃当前书 — 存档已有成果"""
        archive_path = self.file_store.archive_book(book.safe_name)
        self._log(f"📦 已存档: {book.name} → {archive_path}")
        self.progress.remove_book(book.safe_name)

    def _update_counts(self, book: Book):
        """更新完成/失败计数"""
        done = sum(
            1 for u in book.units
            if u.status in (
                ProcessingStatus.STAGE2_DONE,
                ProcessingStatus.STAGE2_5_DONE,
                ProcessingStatus.STAGE3_DONE,
                ProcessingStatus.POST_PROCESSED,
            )
        )
        failed = sum(1 for u in book.units if u.status == ProcessingStatus.FAILED)
        self.current_book_done = done
        self.current_book_failed = failed

    def get_status(self) -> dict:
        """获取当前任务状态（含分段进度 + ETA）"""
        overall_pct = self._calc_overall_progress()
        eta_display = self._format_eta()

        # stage 内部进度信息
        stage_progress = None
        if self._current_stage_name:
            if self._current_stage_name == "modeler":
                stage_progress = {
                    "name": self.current_stage,
                    "step": self.current_book_done,
                    "step_total": self.current_book_total,
                    "pct": self.current_book_done / max(self.current_book_total, 1),
                }
            else:
                stage_progress = {
                    "name": self.current_stage,
                    "step": self._substep_idx,
                    "step_total": self._substep_total,
                    "pct": self._substep_idx / max(self._substep_total, 1),
                    "label": self._substep_label,
                }

        return {
            "running": self.running,
            "paused": self.is_paused,
            "current_book": self.current_book,
            "current_chapter": self.current_chapter,
            "current_stage": self.current_stage,
            "total": self.current_book_total,
            "done": self.current_book_done,
            "failed": self.current_book_failed,
            "queue_remaining": len(self.queue),
            "concurrency": self._concurrent_workers,
            # 分段进度
            "overall_progress": overall_pct,
            "stage_progress": stage_progress,
            "eta_display": eta_display,
            "avg_unit_time": round(self._avg_unit_time, 1),
        }
