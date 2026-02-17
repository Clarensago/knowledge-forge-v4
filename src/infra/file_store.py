"""
文件存储服务

封装 inbox / staging / outbox 三区的读写操作。
"""

import re
import json
import shutil
import logging
from pathlib import Path
from typing import Optional
from datetime import datetime

logger = logging.getLogger("knowledge-forge.store")


class FileStore:
    """文件系统存储服务 — 管理三区目录"""

    def __init__(self, project_root: str):
        self.root = Path(project_root)
        self.inbox_dir = self.root / "inbox"
        self.staging_dir = self.root / "staging"
        self.outbox_dir = self.root / "outbox"
        for d in (self.inbox_dir, self.staging_dir, self.outbox_dir):
            d.mkdir(parents=True, exist_ok=True)

    # ── inbox ──

    def scan_inbox(self) -> list[Path]:
        supported = {".pdf", ".epub", ".txt"}
        files = [
            f for f in self.inbox_dir.iterdir()
            if f.is_file() and f.suffix.lower() in supported
        ]
        files.sort(key=lambda x: x.name)
        return files

    # ── staging ──

    def get_staging_dir(self, safe_name: str) -> Path:
        d = self.staging_dir / safe_name
        d.mkdir(parents=True, exist_ok=True)
        return d

    def write_unit_text(self, safe_name: str, filename: str, text: str):
        filepath = self.get_staging_dir(safe_name) / filename
        filepath.write_text(text, encoding="utf-8")
        logger.debug(f"写入 staging: {filepath.name} ({len(text)} 字)")

    def read_unit_text(self, safe_name: str, filename: str) -> str:
        filepath = self.staging_dir / safe_name / filename
        if not filepath.exists():
            raise FileNotFoundError(f"文件不存在: {filepath}")
        return filepath.read_text(encoding="utf-8")

    def list_staging_files(self, safe_name: str, suffix: str = ".txt") -> list[str]:
        d = self.staging_dir / safe_name
        if not d.exists():
            return []
        return sorted(f.name for f in d.iterdir() if f.suffix == suffix)

    def write_book_mind(self, safe_name: str, data: dict):
        filepath = self.get_staging_dir(safe_name) / "book_mind.json"
        filepath.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8",
        )
        logger.info(f"BookMind 已保存 ({filepath.stat().st_size / 1024:.1f} KB)")

    def read_book_mind(self, safe_name: str) -> Optional[dict]:
        filepath = self.staging_dir / safe_name / "book_mind.json"
        if not filepath.exists():
            return None
        return json.loads(filepath.read_text(encoding="utf-8"))

    def write_structure(self, safe_name: str, data: dict):
        """持久化 BookStructure 到 staging"""
        filepath = self.get_staging_dir(safe_name) / "structure.json"
        filepath.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8",
        )
        logger.info(f"BookStructure 已保存: {filepath.name}")

    def read_structure(self, safe_name: str) -> Optional[dict]:
        filepath = self.staging_dir / safe_name / "structure.json"
        if not filepath.exists():
            return None
        return json.loads(filepath.read_text(encoding="utf-8"))

    # ── outbox ──

    def get_outbox_dir(self, safe_name: str) -> Path:
        d = self.outbox_dir / safe_name
        d.mkdir(parents=True, exist_ok=True)
        return d

    def write_output_md(self, safe_name: str, filename: str, content: str):
        filepath = self.get_outbox_dir(safe_name) / filename
        filepath.write_text(content, encoding="utf-8")
        logger.info(f"产出: {filepath.name} ({len(content)} 字)")

    def list_outbox_files(self, safe_name: str) -> list[str]:
        d = self.outbox_dir / safe_name
        if not d.exists():
            return []
        return sorted(f.name for f in d.iterdir() if f.suffix == ".md")

    def read_outbox_md(self, safe_name: str, filename: str) -> str:
        filepath = self.outbox_dir / safe_name / filename
        if not filepath.exists():
            raise FileNotFoundError(f"文件不存在: {filepath}")
        return filepath.read_text(encoding="utf-8")

    # ── 工具方法 ──

    def clean_staging(self, safe_name: str):
        d = self.staging_dir / safe_name
        if d.exists():
            shutil.rmtree(d)
            logger.info(f"已清空 staging: {safe_name}")

    def clean_outbox(self, safe_name: str):
        d = self.outbox_dir / safe_name
        if d.exists():
            shutil.rmtree(d)
            logger.info(f"已清空 outbox: {safe_name}")

    def archive_book(self, safe_name: str) -> str:
        archive_dir = self.root / "archive"
        archive_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        dest = archive_dir / f"{safe_name}_{ts}"
        dest.mkdir(parents=True, exist_ok=True)
        for area, src_dir in [("outbox", self.outbox_dir), ("staging", self.staging_dir)]:
            src = src_dir / safe_name
            if src.exists():
                shutil.move(str(src), str(dest / area))
        logger.info(f"已存档: {safe_name} → {dest}")
        return str(dest)

    @staticmethod
    def sanitize_dirname(name: str) -> str:
        name = re.sub(r'\.(pdf|epub|txt)$', '', name, flags=re.IGNORECASE)
        name = re.sub(r'[<>:"/\\|?*]', '_', name)
        name = re.sub(r'\s+', ' ', name).strip()
        while len(name.encode("utf-8")) > 200:
            name = name[:-1]
        return name or "unknown_book"

    @staticmethod
    def sanitize_filename(index: str, title: str, ext: str = ".md") -> str:
        title = re.sub(r'^#{1,4}\s+', '', title)
        if ' > ' in title and len(title) > 30:
            title = title.rsplit(' > ', 1)[-1]
        title = re.sub(
            r'[\\/:*?<>|""\u201c\u201d\u2018\u2019\u300c\u300d\u300a\u300b\n\r\t]',
            '', title,
        )
        title = title.replace('\u3000', ' ')
        title = re.sub(r'\s+', ' ', title).strip()
        title = title.strip('_ ，,。.、：:；;！!？?—…·')
        prefix = f"{index}_"
        max_bytes = 180 - len(prefix.encode('utf-8')) - len(ext.encode('utf-8'))
        while len(title.encode('utf-8')) > max_bytes:
            title = title[:-1]
        title = title.rstrip('_ ')
        return f"{prefix}{title or 'untitled'}{ext}"
