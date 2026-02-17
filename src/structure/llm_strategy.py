"""
LLM 目录页策略

从文本前部提取目录区域，调用 LLM 解析为结构化 JSON，转为 BookStructure。
核心逻辑复用 v3 toc_parser.py。
"""

import re
import json
import logging
from typing import Optional

from ..core.models import TOCNode, BookStructure
from ..core.enums import StructureSource

logger = logging.getLogger("knowledge-forge.structure.llm")

# 目录关键词
TOC_START_RE = re.compile(
    r"^(目\s*录|CONTENTS|TABLE\s+OF\s+CONTENTS|Contents)\s*$",
    re.IGNORECASE | re.MULTILINE,
)

# 章节标题行特征
CHAPTER_LINE_RE = re.compile(
    r"^\s*("
    r"(?:PART|Part|part)\s+[\dⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩ]+"
    r"|(?:CHAPTER|Chapter|chapter)\s+\d+"
    r"|第[一二三四五六七八九十百千零○〇\d]+\s*[篇章节卷回部编]"
    r"|[一二三四五六七八九十]+[、.．]\s*"
    r"|\d{1,3}[.、．]\s+"
    r")[^\n]*$",
    re.MULTILINE,
)


class LLMStrategy:
    """LLM 目录页解析策略"""

    name = "llm_toc"

    def __init__(self, llm_client, config):
        self.llm = llm_client
        toc_cfg = config.toc_parsing
        self.scan_chars = toc_cfg.get("scan_chars", 6000)
        self.max_chars = toc_cfg.get("max_chars", 4000)
        self.min_entries = toc_cfg.get("min_entries", 3)
        self.body_signal_len = toc_cfg.get("body_signal_len", 120)

    def analyze(self, text: str, metadata: dict) -> Optional[BookStructure]:
        if not self.llm:
            logger.info("无 LLM 客户端，跳过 LLM 策略")
            return None

        # 提取目录区域
        toc_region = self._extract_toc_region(text)
        if not toc_region:
            logger.info("未识别到目录区域")
            return None

        logger.info(f"识别到目录区域（{len(toc_region)} 字）")

        # LLM 解析
        entries = self._parse_with_llm(toc_region)
        if not entries or self._count_entries(entries) < self.min_entries:
            logger.warning("LLM 解析目录失败或条目不足")
            return None

        # 转为 TOCNode 树
        root = TOCNode(title="ROOT", level=0, text_start=0, text_end=len(text))
        for entry in entries:
            node = self._entry_to_node(entry)
            if node:
                root.children.append(node)

        # 用标题在全文中定位文本位置
        self._locate_in_text(root, text)

        structure = BookStructure(
            root=root,
            source=StructureSource.LLM_TOC,
            metadata=metadata,
        )
        logger.info(f"LLM 结构解析完成: {len(structure.leaf_nodes())} 个叶子节点")
        return structure

    def _extract_toc_region(self, text: str) -> Optional[str]:
        head = text[:self.scan_chars]

        # 策略 1: 关键词定位
        m = TOC_START_RE.search(head)
        if m:
            region = self._scan_lines(head[m.start():])
            if region and len(region.strip().splitlines()) >= self.min_entries:
                return region[:self.max_chars]

        # 策略 2: 密集章节标题行
        matches = list(CHAPTER_LINE_RE.finditer(head))
        if len(matches) >= self.min_entries:
            start = matches[0].start()
            end = matches[-1].end()
            rest = head[end:]
            extra = rest.find("\n\n")
            if extra > 0:
                end += extra
            region = head[start:end].strip()
            lines = [l.strip() for l in region.splitlines() if l.strip()]
            short = [l for l in lines if len(l) < self.body_signal_len]
            if len(short) / max(len(lines), 1) > 0.7:
                return region[:self.max_chars]

        return None

    def _scan_lines(self, text: str) -> Optional[str]:
        lines = text.splitlines()
        toc_lines = []
        long_streak = 0
        for line in lines:
            stripped = line.strip()
            if re.match(r"^#{1,4}\s+", stripped) and toc_lines:
                break
            if len(stripped) > self.body_signal_len:
                long_streak += 1
                if long_streak >= 2:
                    if toc_lines:
                        toc_lines.pop()
                    break
            else:
                long_streak = 0
            toc_lines.append(line)
        result = "\n".join(toc_lines).strip()
        return result or None

    def _parse_with_llm(self, toc_text: str) -> Optional[list]:
        prompt = f"""以下是一本书的目录区域原文：

```
{toc_text}
```

请解析该目录，输出一个 JSON 对象，格式如下：

```json
{{
  "entries": [
    {{
      "level": 1,
      "title": "PART Ⅰ 作家和故事艺术",
      "children": [
        {{"level": 2, "title": "CHAPTER 01 故事问题", "children": []}}
      ]
    }}
  ]
}}
```

规则：
1. level=1 最粗（篇/卷/PART），level=2 中间（章/CHAPTER），level=3 更细（节/小节）
2. 单层结构所有条目 level=1，children=[]
3. 忽略前言/序言/目录/附录/版权
4. 保留原始标题
5. 只输出 JSON"""

        try:
            response = self.llm.generate(
                prompt=prompt,
                temperature=0.1,
                max_tokens=2048,
                stream=False,
                system_message="你是书籍结构分析专家。严格按 JSON 格式输出。",
                task_label="TOC解析",
            )
            cleaned = response.strip()
            cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
            cleaned = re.sub(r"\s*```\s*$", "", cleaned)
            data = json.loads(cleaned)
            return data.get("entries", [])
        except Exception as e:
            logger.error(f"LLM TOC 解析失败: {e}")
            return None

    def _count_entries(self, entries: list) -> int:
        count = 0
        for e in entries:
            count += 1
            count += self._count_entries(e.get("children", []))
        return count

    def _entry_to_node(self, entry: dict) -> Optional[TOCNode]:
        title = entry.get("title", "").strip()
        if not title:
            return None
        node = TOCNode(title=title, level=entry.get("level", 1))
        for child in entry.get("children", []):
            child_node = self._entry_to_node(child)
            if child_node:
                node.children.append(child_node)
        return node

    def _locate_in_text(self, root: TOCNode, text: str):
        """在全文中定位所有叶子节点的文本位置"""
        all_nodes = []
        self._collect_leaves_dfs(root, all_nodes)

        for node in all_nodes:
            if node.level == 0:
                continue
            pos = self._find_title(text, node.title)
            if pos >= 0:
                node.text_start = pos

        # 填充 text_end
        self._fill_ends(root, len(text))

    def _find_title(self, text: str, title: str) -> int:
        escaped = re.escape(title)
        flexible = re.sub(r"\\ ", r"\\s+", escaped)
        pattern = re.compile(r"^(?:#{1,4}\s+)?" + flexible, re.MULTILINE)
        m = pattern.search(text)
        if m:
            return m.start()
        # 退化匹配
        short = re.sub(
            r"^(?:CHAPTER|Chapter|PART|Part|第[一二三四五六七八九十百千零○〇\d]+[篇章节卷回部编])\s*\d*\s*",
            "", title,
        ).strip()
        if short and len(short) >= 2:
            m2 = re.search(re.escape(short), text)
            if m2:
                return m2.start()
        return -1

    def _collect_leaves_dfs(self, node: TOCNode, result: list):
        result.append(node)
        for child in node.children:
            self._collect_leaves_dfs(child, result)

    def _fill_ends(self, node: TOCNode, parent_end: int):
        for i, child in enumerate(node.children):
            child.text_end = (
                node.children[i + 1].text_start
                if i + 1 < len(node.children)
                else parent_end
            )
            if child.text_end <= child.text_start:
                child.text_end = parent_end
            self._fill_ends(child, child.text_end)
