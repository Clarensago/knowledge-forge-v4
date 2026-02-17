"""
PDF 文本提取器

从 v3 迁移：PyMuPDF 提取 + 页眉页脚检测 + 断句合并。
"""

import re
import logging
from pathlib import Path
from collections import Counter

from ..core.models import ExtractionResult

try:
    import fitz  # PyMuPDF
except ImportError:
    fitz = None  # type: ignore

logger = logging.getLogger("knowledge-forge.extractor.pdf")


def _detect_repeated_lines(pages: list[str], threshold: float = 0.5) -> set[str]:
    """检测页眉/页脚（跨页重复短行）"""
    if len(pages) < 3:
        return set()
    counter: Counter = Counter()
    for page in pages:
        lines = page.strip().splitlines()
        candidates = {
            l.strip() for l in (lines[:3] + lines[-3:])
            if l.strip() and len(l.strip()) < 80
        }
        for c in candidates:
            counter[c] += 1
    return {l for l, n in counter.items() if n / len(pages) >= threshold}


def _merge_broken_sentences(text: str) -> str:
    """合并跨页断句"""
    lines = text.splitlines()
    merged = []
    for i, line in enumerate(lines):
        if (
            line.strip()
            and not re.search(r"[。！？；.!?;:\n]$", line.strip())
            and i + 1 < len(lines)
            and lines[i + 1].strip()
            and not lines[i + 1].strip().startswith(("#", ">", "-", "*", "|"))
        ):
            merged.append(line.rstrip())
        else:
            merged.append(line)
    return "\n".join(merged)


class PdfExtractor:
    """PDF 提取器"""

    supported_extensions = [".pdf"]

    def extract(self, file_path: str) -> ExtractionResult:
        if fitz is None:
            raise ImportError("需要安装 PyMuPDF: pip install PyMuPDF")

        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"PDF 文件不存在: {path}")

        logger.info(f"提取 PDF: {path.name}")
        doc = fitz.open(str(path))
        pages = [page.get_text("text") for page in doc]
        doc.close()

        repeated = _detect_repeated_lines(pages)

        cleaned = []
        for page in pages:
            lines = [
                l for l in page.splitlines()
                if l.strip() not in repeated and not re.match(r"^\d{1,4}$", l.strip())
            ]
            cleaned.append("\n".join(lines))

        text = "\n\n".join(cleaned)
        text = _merge_broken_sentences(text)
        text = re.sub(r"\n{4,}", "\n\n\n", text)

        logger.info(f"PDF 提取完成: {len(text)} 字, {len(pages)} 页")
        return ExtractionResult(
            text=text.strip(),
            metadata={"total_pages": len(pages)},
        )
