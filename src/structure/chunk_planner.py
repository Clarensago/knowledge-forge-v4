"""
ChunkPlanner — 结构感知分块器

遍历 BookStructure 叶子节点，根据 chunk 配置将逻辑章节切为 ProcessingUnit[]。
每个 unit 携带完整 breadcrumb 层级上下文。
"""

import re
import logging
from typing import Optional

from ..core.models import TOCNode, BookStructure, ProcessingUnit
from ..core.enums import ChapterType

logger = logging.getLogger("knowledge-forge.structure.planner")


class ChunkPlanner:
    """结构感知分块器"""

    def __init__(self, config):
        chunk_cfg = config.chunk_config
        self.min_size = chunk_cfg.get("min_size", 500)
        self.max_size = chunk_cfg.get("max_size", 8000)
        self.preferred_size = chunk_cfg.get("preferred_size", 6000)
        self.merge_threshold = chunk_cfg.get("merge_threshold", 300)

    def plan(self, structure: BookStructure, full_text: str) -> list[ProcessingUnit]:
        """
        将 BookStructure 转为 ProcessingUnit 列表。

        流程：
        1. 获取所有叶子节点
        2. 过短的向相邻合并
        3. 过长的按段落/子标题切割
        4. 每个 unit 携带 breadcrumb
        """
        leaves = structure.leaf_nodes()
        if not leaves:
            return self._fallback_split(full_text)

        # 提取每个叶子的文本
        raw_units = []
        for i, node in enumerate(leaves):
            text = full_text[node.text_start:node.text_end].strip()
            if not text:
                continue
            breadcrumb = node.breadcrumb()
            raw_units.append({
                "node": node,
                "text": text,
                "breadcrumb": breadcrumb,
            })

        if not raw_units:
            return self._fallback_split(full_text)

        # 合并过短节点
        merged = self._merge_small(raw_units)

        # 切割过长节点 + 生成最终 ProcessingUnit
        units = []
        unit_num = 0

        for item in merged:
            text = item["text"]
            breadcrumb = item["breadcrumb"]
            node = item["node"]
            wc = len(text)

            if wc <= self.max_size:
                unit_num += 1
                units.append(ProcessingUnit(
                    id=f"{unit_num:02d}",
                    text=text,
                    word_count=wc,
                    breadcrumb=breadcrumb,
                    toc_node=node,
                    chapter_type=self._classify(wc),
                ))
            else:
                # 超长：二次切割
                sub_chunks = self._split_long(text, node.title)
                total_parts = len(sub_chunks)
                for j, (chunk_text, sub_title) in enumerate(sub_chunks):
                    unit_num += 1
                    bc = breadcrumb.copy()
                    if sub_title and sub_title != breadcrumb[-1] if breadcrumb else True:
                        bc.append(sub_title)
                    units.append(ProcessingUnit(
                        id=f"{unit_num:02d}",
                        text=chunk_text,
                        word_count=len(chunk_text),
                        breadcrumb=bc,
                        toc_node=node,
                        is_partial=True,
                        part_index=j,
                        total_parts=total_parts,
                        chapter_type=self._classify(len(chunk_text)),
                    ))

        logger.info(f"分块规划完成: {len(units)} 个处理单元")
        for u in units:
            logger.debug(f"  [{u.id}] {' > '.join(u.breadcrumb)} ({u.word_count}字)")
        return units

    def _merge_small(self, items: list[dict]) -> list[dict]:
        """合并过短节点到相邻节点"""
        if not items:
            return items

        merged = []
        for item in items:
            wc = len(item["text"])
            if merged and wc < self.merge_threshold:
                merged[-1]["text"] += "\n\n" + item["text"]
            elif merged and len(merged[-1]["text"]) < self.merge_threshold:
                merged[-1]["text"] += "\n\n" + item["text"]
                merged[-1]["breadcrumb"] = item["breadcrumb"]
                merged[-1]["node"] = item["node"]
            else:
                merged.append(item)
        return merged

    def _split_long(self, text: str, base_title: str) -> list[tuple[str, str]]:
        """
        切割超长文本。

        优先按子标题切割，否则按段落均匀切割。
        返回: [(chunk_text, sub_title), ...]
        """
        # 尝试子标题切割
        sub_patterns = [
            r"\n(第[一二三四五六七八九十百千零○〇0-9]+节\s*[^\n]+)",
            r"\n(#{2,4}\s+[^\n]+)",
            r"\n(\d+\.\d+[.、]?\s+[^\n]+)",
        ]
        for pat in sub_patterns:
            parts = re.split(pat, text)
            if len(parts) >= 3:
                return self._assemble_sub_parts(parts)

        # 兜底：段落均匀切割
        return self._split_by_paragraphs(text)

    def _assemble_sub_parts(self, parts: list[str]) -> list[tuple[str, str]]:
        """将 re.split 结果组装为 (text, title) 对"""
        result = []
        if parts[0].strip():
            result.append((parts[0].strip(), ""))
        for i in range(1, len(parts), 2):
            heading = parts[i].strip()
            body = parts[i + 1].strip() if i + 1 < len(parts) else ""
            chunk = f"{heading}\n{body}" if body else heading
            clean_h = re.sub(r"^#{1,4}\s+", "", heading).strip()
            result.append((chunk, clean_h[:50]))

        # 合并过小块
        merged = []
        for text, title in result:
            if merged and len(text) < self.min_size:
                prev_text, prev_title = merged[-1]
                merged[-1] = (prev_text + "\n\n" + text, prev_title or title)
            else:
                merged.append((text, title))

        # 再次切割仍然过长的块
        final = []
        for text, title in merged:
            if len(text) > self.max_size:
                for chunk_text, _ in self._split_by_paragraphs(text):
                    final.append((chunk_text, title))
            else:
                final.append((text, title))
        return final

    def _split_by_paragraphs(self, text: str) -> list[tuple[str, str]]:
        """按段落边界均匀切割"""
        paragraphs = text.split("\n\n")
        result = []
        current = []
        current_size = 0

        for para in paragraphs:
            size = len(para)
            if current_size + size > self.preferred_size and current:
                result.append(("\n\n".join(current).strip(), ""))
                current = [para]
                current_size = size
            else:
                current.append(para)
                current_size += size

        if current:
            result.append(("\n\n".join(current).strip(), ""))
        return result

    def _classify(self, word_count: int) -> ChapterType:
        """根据字数分类章节类型"""
        if word_count < 500:
            return ChapterType.PREFACE
        if word_count < 2000:
            return ChapterType.SHORT
        if word_count > 15000:
            return ChapterType.LONG
        return ChapterType.NORMAL

    def _fallback_split(self, text: str) -> list[ProcessingUnit]:
        """兜底：无结构时按段落均匀切割"""
        chunks = self._split_by_paragraphs(text)
        units = []
        for i, (chunk_text, _) in enumerate(chunks, 1):
            units.append(ProcessingUnit(
                id=f"{i:02d}",
                text=chunk_text,
                word_count=len(chunk_text),
                breadcrumb=[f"片段 {i}"],
                chapter_type=self._classify(len(chunk_text)),
            ))
        return units
