"""
进度管理器

管理 progress.json，支持 Book 级和 ProcessingUnit 级状态追踪。
线程安全，原子写入。
"""

import json
import os
import logging
import tempfile
import threading
from pathlib import Path
from datetime import datetime
from typing import Optional

from ..core.models import Book, ProcessingUnit
from ..core.enums import ProcessingStatus

logger = logging.getLogger("knowledge-forge.progress")


class ProgressManager:
    """进度管理器 — 持久化 progress.json（线程安全）"""

    def __init__(self, project_root: str):
        self.progress_file = Path(project_root) / "progress.json"
        self._lock = threading.Lock()
        self._data = self._load()

    def _load(self) -> dict:
        if self.progress_file.exists():
            try:
                return json.loads(self.progress_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, IOError) as e:
                logger.warning(f"进度文件损坏，将重建: {e}")
        return {"books": {}, "last_updated": ""}

    def save(self):
        """原子写入进度文件"""
        self._data["last_updated"] = datetime.now().isoformat()
        content = json.dumps(self._data, ensure_ascii=False, indent=2)
        try:
            fd, tmp = tempfile.mkstemp(
                dir=str(self.progress_file.parent),
                prefix=".progress_", suffix=".tmp",
            )
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
            os.replace(tmp, str(self.progress_file))
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            self.progress_file.write_text(content, encoding="utf-8")

    # ── Book 级操作 ──

    def get_book(self, safe_name: str) -> Optional[Book]:
        with self._lock:
            data = self._data.get("books", {}).get(safe_name)
            if not data:
                return None
            return Book.from_progress_dict(data)

    def save_book(self, book: Book):
        book.update_timestamp()
        with self._lock:
            self._data.setdefault("books", {})[book.safe_name] = book.to_progress_dict()
            self.save()
        logger.debug(f"进度已保存: {book.name} [{book.status.value}]")

    def list_books(self) -> list[str]:
        with self._lock:
            return list(self._data.get("books", {}).keys())

    def remove_book(self, safe_name: str):
        with self._lock:
            if safe_name in self._data.get("books", {}):
                del self._data["books"][safe_name]
                self.save()
                logger.info(f"已从进度中移除: {safe_name}")

    # ── Unit 级操作 ──

    def update_unit_status(
        self, book: Book, unit_id: str, status: ProcessingStatus, **kwargs,
    ):
        unit = book.get_unit_by_id(unit_id)
        if unit:
            unit.status = status
            for key, value in kwargs.items():
                if hasattr(unit, key):
                    setattr(unit, key, value)
            self.save_book(book)

    # ── 查询 ──

    def get_book_status_summary(self, book: Book) -> dict:
        summary = {s.value: 0 for s in ProcessingStatus}
        for u in book.units:
            summary[u.status.value] += 1
        return {
            "total": len(book.units),
            "book_status": book.status.value,
            "units": summary,
        }

    def is_stage_complete(self, book: Book, stage: str) -> bool:
        stage_map = {
            "stage0": ProcessingStatus.STAGE0_DONE,
            "stage1": ProcessingStatus.STAGE1_DONE,
            "stage2": ProcessingStatus.STAGE2_DONE,
            "stage2_5": ProcessingStatus.STAGE2_5_DONE,
            "stage3": ProcessingStatus.STAGE3_DONE,
        }
        target = stage_map.get(stage)
        if not target:
            return False
        status_list = list(ProcessingStatus)
        return status_list.index(book.status) >= status_list.index(target)

    def reset_book(self, book: Book):
        book.status = ProcessingStatus.PENDING
        for u in book.units:
            u.status = ProcessingStatus.PENDING
            u.retry_count = 0
            u.error_message = ""
            u.round1_output = ""
            u.round2_output = ""
            u.quality_score = 0.0
        self.save_book(book)
        logger.info(f"已重置: {book.name} ({len(book.units)} 单元)")
