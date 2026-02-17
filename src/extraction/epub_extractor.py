"""
EPUB 文本提取器

v4 核心改进：同时提取文本和原生 TOC 结构（toc.ncx / nav.xhtml）。
返回 ExtractionResult，其 metadata["epub_toc"] 包含树形目录数据。
"""

import re
import logging
from pathlib import Path

from ..core.models import ExtractionResult

try:
    import ebooklib
    from ebooklib import epub
except ImportError:
    ebooklib = None  # type: ignore

try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None  # type: ignore

logger = logging.getLogger("knowledge-forge.extractor.epub")


def _html_to_text(html_content: str) -> str:
    """HTML → 纯文本，保留段落和标题结构"""
    soup = BeautifulSoup(html_content, "html.parser")
    for tag in soup(["script", "style", "nav"]):
        tag.decompose()
    for level in range(1, 7):
        for h in soup.find_all(f"h{level}"):
            h.string = f"\n{'#' * level} {h.get_text().strip()}\n"
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for p in soup.find_all("p"):
        p.insert_before("\n")
        p.insert_after("\n")
    for div in soup.find_all("div"):
        div.insert_after("\n")
    text = soup.get_text()
    text = re.sub(r"\n{3,}", "\n\n", text)
    lines = [line.strip() for line in text.splitlines()]
    return "\n".join(lines).strip()


def _extract_toc_from_book(book) -> list[dict]:
    """
    从 ebooklib Book 对象提取原生 TOC 树。

    ebooklib 的 book.toc 可能是：
    - tuple(Section, list[Link])  — 嵌套结构
    - Link                       — 扁平条目

    返回统一的树形 dict 列表：
    [{"title": str, "href": str, "children": [...]}]
    """
    def _parse_toc_items(items) -> list[dict]:
        result = []
        for item in items:
            if isinstance(item, tuple):
                # (Section/Link, [children])
                section, children = item
                node = {
                    "title": getattr(section, "title", str(section)),
                    "href": getattr(section, "href", ""),
                    "children": _parse_toc_items(children) if children else [],
                }
                result.append(node)
            elif hasattr(item, "title"):
                # epub.Link
                result.append({
                    "title": item.title or "",
                    "href": item.href or "",
                    "children": [],
                })
        return result

    return _parse_toc_items(book.toc)


def _build_spine_map(book) -> dict[str, int]:
    """
    建立 spine item filename → 在全文中的字符偏移映射。

    返回: {"chapter1.xhtml": 0, "chapter2.xhtml": 5432, ...}
    """
    offset_map = {}
    current_offset = 0

    for item in book.get_items_of_type(ebooklib.ITEM_DOCUMENT):
        filename = item.get_name()
        html = item.get_content().decode("utf-8", errors="ignore")
        text = _html_to_text(html)
        offset_map[filename] = current_offset
        # +4 for separator "\n\n---\n\n" between chapters (but we'll use actual text length)
        current_offset += len(text) + 5  # "\n\n---\n\n" = 7, but stripped varies

    return offset_map


class EpubExtractor:
    """EPUB 提取器"""

    supported_extensions = [".epub"]

    def extract(self, file_path: str) -> ExtractionResult:
        if ebooklib is None:
            raise ImportError("需要安装 ebooklib: pip install ebooklib")
        if BeautifulSoup is None:
            raise ImportError("需要安装 beautifulsoup4: pip install beautifulsoup4")

        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"EPUB 文件不存在: {path}")

        logger.info(f"提取 EPUB: {path.name}")
        book = epub.read_epub(str(path))

        # 提取文本
        parts = []
        spine_offsets = {}
        offset = 0
        for item in book.get_items_of_type(ebooklib.ITEM_DOCUMENT):
            html = item.get_content().decode("utf-8", errors="ignore")
            text = _html_to_text(html)
            if text.strip():
                filename = item.get_name()
                spine_offsets[filename] = offset
                parts.append(text)
                offset += len(text) + 5  # separator length

        full_text = "\n\n---\n\n".join(parts)
        full_text = re.sub(r"\n{4,}", "\n\n\n", full_text)

        # 提取原生 TOC
        epub_toc = _extract_toc_from_book(book)

        # 提取元数据
        title = ""
        author = ""
        try:
            titles = book.get_metadata("DC", "title")
            if titles:
                title = titles[0][0]
            creators = book.get_metadata("DC", "creator")
            if creators:
                author = creators[0][0]
        except Exception:
            pass

        metadata = {
            "title": title,
            "author": author,
            "epub_toc": epub_toc,
            "spine_offsets": spine_offsets,
            "total_spine_items": len(parts),
        }

        logger.info(
            f"EPUB 提取完成: {len(full_text)} 字, "
            f"{len(parts)} 个 spine items, "
            f"TOC {len(epub_toc)} 个顶层条目"
        )
        return ExtractionResult(text=full_text.strip(), metadata=metadata)
