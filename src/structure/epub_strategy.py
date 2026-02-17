"""
EPUB 原生目录策略

直接解析 EPUB 的 toc.ncx / nav.xhtml 原生目录数据，
零 LLM 调用、零正则匹配，最高优先级策略。
"""

import re
import logging
from typing import Optional

from ..core.models import TOCNode, BookStructure
from ..core.enums import StructureSource

logger = logging.getLogger("knowledge-forge.structure.epub")


class EpubStrategy:
    """EPUB 原生目录解析策略"""

    name = "epub_native"

    def analyze(self, text: str, metadata: dict) -> Optional[BookStructure]:
        epub_toc = metadata.get("epub_toc")
        if not epub_toc:
            logger.info("无 EPUB 原生 TOC 数据")
            return None

        spine_offsets = metadata.get("spine_offsets", {})

        # 将 epub_toc dict 树转为 TOCNode 树
        root = TOCNode(title="ROOT", level=0)
        root.text_start = 0
        root.text_end = len(text)

        for item in epub_toc:
            child = self._build_node(item, level=1, text=text, spine_offsets=spine_offsets)
            if child:
                root.children.append(child)

        if not root.children:
            logger.info("EPUB TOC 转换后无有效节点")
            return None

        # 填充文本范围（基于顺序推断）
        self._fill_text_ranges(root, text)

        structure = BookStructure(
            root=root,
            source=StructureSource.EPUB_NATIVE,
            metadata={k: v for k, v in metadata.items() if k not in ("epub_toc", "spine_offsets")},
        )

        logger.info(
            f"EPUB 原生结构解析完成: "
            f"{len(structure.leaf_nodes())} 个叶子节点"
        )
        return structure

    def _build_node(
        self, item: dict, level: int, text: str, spine_offsets: dict,
    ) -> Optional[TOCNode]:
        """递归将 epub_toc dict 转为 TOCNode"""
        title = item.get("title", "").strip()
        if not title:
            return None

        href = item.get("href", "")
        source_id = href.split("#")[0] if href else ""

        node = TOCNode(
            title=title,
            level=level,
            source_id=source_id,
        )

        # 尝试从 spine_offsets 获取文本位置
        if source_id and source_id in spine_offsets:
            node.text_start = spine_offsets[source_id]

        # 递归子节点
        for child_item in item.get("children", []):
            child = self._build_node(child_item, level + 1, text, spine_offsets)
            if child:
                node.children.append(child)

        return node

    def _fill_text_ranges(self, root: TOCNode, text: str):
        """
        填充所有节点的 text_start/text_end。

        策略：
        1. 有 source_id 对应 spine_offsets 的，已在 _build_node 中设置 text_start
        2. 对于未设置的，通过标题文本匹配定位
        3. text_end = 下一个同级节点的 text_start，最后一个 = 父节点 text_end
        """
        all_nodes = self._flatten_dfs(root)

        # 先尝试用标题在全文中定位
        for node in all_nodes:
            if node.level == 0:
                continue
            if node.text_start == 0 and node.title:
                pos = self._find_title_in_text(text, node.title)
                if pos >= 0:
                    node.text_start = pos

        # 按 text_start 排序同级节点，填充 text_end
        self._fill_ends_recursive(root, len(text))

    def _find_title_in_text(self, text: str, title: str) -> int:
        """在全文中查找标题位置"""
        escaped = re.escape(title)
        flexible = re.sub(r"\\ ", r"\\s+", escaped)
        pattern = re.compile(r"^(?:#{1,4}\s+)?" + flexible, re.MULTILINE)
        m = pattern.search(text)
        return m.start() if m else -1

    def _flatten_dfs(self, node: TOCNode) -> list[TOCNode]:
        """DFS 展平所有节点"""
        result = [node]
        for child in node.children:
            result.extend(self._flatten_dfs(child))
        return result

    def _fill_ends_recursive(self, node: TOCNode, parent_end: int):
        """递归填充每个节点的 text_end"""
        children = node.children
        for i, child in enumerate(children):
            if i + 1 < len(children):
                child.text_end = children[i + 1].text_start
            else:
                child.text_end = parent_end
            # 确保 text_end > text_start
            if child.text_end <= child.text_start:
                child.text_end = parent_end
            self._fill_ends_recursive(child, child.text_end)
