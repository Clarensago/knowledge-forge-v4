"""
正则模式策略

从 v3 splitter.py 精简重写：8 种模式检测 + 质量回退。
去除所有补丁逻辑（同名合并/过小吸收/parent 注入），
这些由 ChunkPlanner 在树形结构层面处理。
"""

import re
import logging
from typing import Optional

from ..core.models import TOCNode, BookStructure
from ..core.enums import StructureSource

logger = logging.getLogger("knowledge-forge.structure.regex")


class RegexStrategy:
    """正则标题模式检测策略"""

    name = "regex_detect"

    def __init__(self, config):
        self.patterns = config.regex_patterns
        self.detection = config.regex_detection
        self.min_matches = self.detection.get("min_matches", 3)
        self.skip_chars = self.detection.get("skip_toc_chars", 3000)
        self.fragment_threshold = self.detection.get("fragment_threshold", 0.7)

    def analyze(self, text: str, metadata: dict) -> Optional[BookStructure]:
        # 检测最佳模式
        pattern_name, regex = self._detect_best_pattern(text)
        if not pattern_name:
            logger.info("正则策略：未检测到有效分割模式")
            return None

        logger.info(f"正则策略：检测到模式 {pattern_name}")

        # 按模式分割，构建 TOCNode 树
        root = self._split_to_tree(text, regex, pattern_name)
        if not root or len(root.children) < 2:
            return None

        structure = BookStructure(
            root=root,
            source=StructureSource.REGEX,
            metadata=metadata,
        )
        logger.info(f"正则结构解析完成: {len(structure.leaf_nodes())} 个叶子节点")
        return structure

    def _detect_best_pattern(self, text: str) -> tuple[Optional[str], Optional[str]]:
        """三档优先级检测 + 质量回退"""
        tiers = [
            (["chinese_pian_only", "chinese_chapter", "english_chapter", "english_part"], 2),
            (["markdown_h1", "markdown_h2"], 3),
            (["chinese_numbered", "numeric_dot"], 3),
        ]
        excluded: set[str] = set()

        # 用于检测的文本（跳过目录区）
        detect_text = text[self.skip_chars:] if len(text) > self.skip_chars else text

        while True:
            best_name = None
            best_regex = None

            for tier_names, min_n in tiers:
                best_count = 0
                for name in tier_names:
                    if name in excluded or name not in self.patterns:
                        continue
                    pat_str = self.patterns[name].get("pattern", "") if isinstance(self.patterns[name], dict) else self.patterns[name]
                    matches = re.findall(pat_str, detect_text, re.MULTILINE)
                    if len(matches) >= min_n and len(matches) > best_count:
                        best_count = len(matches)
                        best_name = name
                        best_regex = pat_str
                if best_name:
                    break

            if not best_name:
                return None, None

            # 质量校验：用完整文本试分割
            test_root = self._split_to_tree(text, best_regex, best_name)
            if test_root:
                leaves = test_root.all_leaves()
                real_leaves = [l for l in leaves if l.title != "前言与目录"]
                if real_leaves:
                    tiny = sum(1 for l in real_leaves if l.word_count < 500)
                    if tiny / len(real_leaves) > self.fragment_threshold:
                        logger.warning(
                            f"模式 {best_name} 碎片率过高 "
                            f"({tiny}/{len(real_leaves)}), 尝试下一模式"
                        )
                        excluded.add(best_name)
                        continue
            return best_name, best_regex

    def _split_to_tree(
        self, text: str, regex: str, pattern_name: str,
    ) -> Optional[TOCNode]:
        """用正则分割文本，直接构建 TOCNode 树"""
        non_capture = re.sub(r"\((?!\?)", "(?:", regex)
        parts = re.split(f"({non_capture})", text, flags=re.MULTILINE)

        root = TOCNode(title="ROOT", level=0, text_start=0, text_end=len(text))
        offset = 0

        # 前言
        if parts[0].strip():
            preamble = TOCNode(
                title="前言与目录", level=1,
                text_start=0, text_end=len(parts[0]),
            )
            root.children.append(preamble)
        offset = len(parts[0])

        for i in range(1, len(parts), 2):
            raw_title = parts[i].strip()
            content = parts[i + 1] if i + 1 < len(parts) else ""
            start = offset
            offset += len(parts[i]) + len(content)

            title = self._clean_title(raw_title)
            node = TOCNode(
                title=title,
                level=1,
                text_start=start,
                text_end=offset,
            )
            root.children.append(node)

        return root if root.children else None

    @staticmethod
    def _clean_title(raw: str) -> str:
        """清洗标题"""
        title = raw.strip()
        title = re.sub(r"^#{1,4}\s+", "", title)
        title = re.sub(r"[\s.·…]+\d{1,4}\s*$", "", title).strip()
        title = re.sub(r"\s+", " ", title)
        return title[:80] if title else "untitled"
