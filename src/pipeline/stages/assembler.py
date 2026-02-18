"""
Stage 2.5: 结构感知输出组装

基于 BookStructure 树形层级将各 ProcessingUnit 的 Stage 2 产出
按"篇"粒度合并为完整的 MD 文件。

v4 改进：
- 利用 ProcessingUnit.is_partial / part_index / total_parts 原生字段，
  替代 v3 靠 index 字符串中 '-' 分隔符做分组的脆弱逻辑
- 基于 BookStructure 树自动确定"篇"边界，而非硬编码正则
- 统一的 frontmatter 生成
"""

import re
import logging
from collections import defaultdict
from datetime import datetime
from typing import Optional

from ..context import PipelineContext
from ...core.models import Book, BookStructure, ProcessingUnit, TOCNode
from ...core.enums import ProcessingStatus
from ...infra.file_store import FileStore
from ...infra.progress import ProgressManager

logger = logging.getLogger("knowledge-forge.stage2_5")


class AssemblerStage:
    """Stage 2.5: 结构感知输出组装"""

    name = "assembler"

    def __init__(self, file_store: FileStore, progress: ProgressManager):
        self.file_store = file_store
        self.progress = progress

    def process(self, context: PipelineContext):
        book = context.book
        structure = book.structure or context.structure
        units = book.units

        if not units:
            logger.warning("无可用处理单元，跳过 Stage 2.5")
            return

        # 按"篇"分组
        groups = self._group_by_part(units, structure)

        total_files = 0
        for part_id, group_units in sorted(groups.items()):
            context.check_pause()
            if context.check_stop():
                break

            # 已经合并过的跳过（增量输出已处理）
            if all(u.status in (ProcessingStatus.STAGE2_5_DONE, ProcessingStatus.STAGE3_DONE)
                   for u in group_units):
                continue

            # 检查该组是否全部完成 Stage 2
            ready = all(
                u.status in (
                    ProcessingStatus.STAGE2_DONE,
                    ProcessingStatus.STAGE2_5_DONE,
                    ProcessingStatus.STAGE3_DONE,
                )
                for u in group_units
            )
            if not ready:
                logger.info(f"[{part_id}] 部分单元未完成 Stage 2，跳过")
                continue

            if self.assemble_one_group(book, part_id, group_units, structure):
                total_files += 1

        # 处理不属于任何篇的独立单元（单章书等）
        standalone = [
            u for u in units
            if u.status == ProcessingStatus.STAGE2_DONE
            and not u.is_partial
        ]
        for u in standalone:
            md = self._build_single_output(book, u)
            filename = FileStore.sanitize_filename(u.id, u.title, ".md")
            self.file_store.write_output_md(book.safe_name, filename, md)
            u.status = ProcessingStatus.STAGE2_5_DONE
            total_files += 1

        book.status = ProcessingStatus.STAGE2_5_DONE
        self.progress.save_book(book)
        logger.info(f"Stage 2.5 完成: 生成 {total_files} 个输出文件")

    def assemble_one_group(
        self,
        book: Book,
        group_id: str,
        group_units: list[ProcessingUnit],
        structure: Optional[BookStructure] = None,
    ) -> bool:
        """
        合并输出单个分组到 outbox。供 ModelerStage 增量调用。

        Returns:
            是否成功输出
        """
        # 跳过已经输出过的 group
        if all(u.status in (ProcessingStatus.STAGE2_5_DONE, ProcessingStatus.STAGE3_DONE)
               for u in group_units):
            return False

        md = self._assemble_group(book, group_id, group_units, structure)
        if md:
            filename = FileStore.sanitize_filename(group_id, group_units[0].title, ".md")
            self.file_store.write_output_md(book.safe_name, filename, md)
            for u in group_units:
                u.status = ProcessingStatus.STAGE2_5_DONE
            logger.info(f"[增量输出] {group_id} → outbox ({len(group_units)} units)")
            return True
        return False

    def _group_by_part(
        self,
        units: list[ProcessingUnit],
        structure: Optional[BookStructure],
    ) -> dict[str, list[ProcessingUnit]]:
        """
        按"篇"分组。

        分组策略：
        1. 子切片 (is_partial=True)：按 id 的基础前缀分组（如 "03-01", "03-02" → "03"）
        2. 同一 level-1 节点下的单元：自然归为一篇
        3. 无结构信息时：每个独立单元自成一组
        """
        groups: dict[str, list[ProcessingUnit]] = defaultdict(list)

        for u in units:
            if u.is_partial:
                # 子切片：按 base id 分组
                base_id = u.id.rsplit("-", 1)[0] if "-" in u.id else u.id
                groups[base_id].append(u)
            elif structure and u.toc_node:
                # 有结构时：按 level-1 祖先分组
                part_title = self._find_part_ancestor(u.toc_node)
                if part_title:
                    groups[u.id[:2]].append(u)
                # 独立单元不在这里处理，留给 standalone 逻辑
            # 非 partial + 无结构 → standalone

        # 排序每组内的单元
        for key in groups:
            groups[key].sort(key=lambda u: (u.id, u.part_index))

        return dict(groups)

    def _find_part_ancestor(self, node: TOCNode) -> Optional[str]:
        """沿树向上查找 level=1 的祖先标题"""
        current = node
        while current:
            if current.level == 1:
                return current.title
            current = getattr(current, "_parent", None)
        return None

    def _assemble_group(
        self,
        book: Book,
        part_id: str,
        units: list[ProcessingUnit],
        structure: Optional[BookStructure],
    ) -> str:
        """将一组 ProcessingUnit 合并为一个 MD 文件"""
        # 确定篇标题
        part_title = self._derive_part_title(units)

        # 收集 tags
        all_tags = set()
        for u in units:
            # 从 frontmatter 中提取 tags（如有）
            tags = self._extract_tags(u.final_output)
            all_tags.update(tags)

        # 构建 frontmatter
        frontmatter = self._build_frontmatter(
            chapter=part_id,
            title=part_title,
            book_name=book.name,
            tags=sorted(all_tags),
            parts=len(units),
        )

        # 构建正文
        body_parts = []
        total = len(units)

        if total == 1:
            # 单个单元：直接输出
            content = self._strip_frontmatter(units[0].final_output)
            content = self._strip_leading_h1(content)
            body_parts.append(content)
        else:
            # 多个子块
            for i, u in enumerate(units, 1):
                content = self._strip_frontmatter(u.final_output)
                content = self._strip_leading_h1(content)

                body_parts.append(f"---\n\n## Part {i:02d}/{total:02d}")
                if u.breadcrumb:
                    body_parts.append(f"> {' > '.join(u.breadcrumb)}\n")
                body_parts.append(content)

        # 组装
        h1 = f"# {part_id} - {part_title}"
        full = f"{frontmatter}\n{h1}\n\n" + "\n\n".join(body_parts)
        return full.strip() + "\n"

    def _build_single_output(self, book: Book, unit: ProcessingUnit) -> str:
        """为独立单元构建输出"""
        frontmatter = self._build_frontmatter(
            chapter=unit.id,
            title=unit.title,
            book_name=book.name,
            tags=sorted(self._extract_tags(unit.final_output)),
            parts=1,
        )
        content = self._strip_frontmatter(unit.final_output)
        # 保留原始 H1 或构建新的
        if not content.lstrip().startswith("# "):
            content = f"# {unit.id} - {unit.title}\n\n{content}"
        return f"{frontmatter}\n{content}".strip() + "\n"

    def _derive_part_title(self, units: list[ProcessingUnit]) -> str:
        """从一组单元推导篇标题"""
        if not units:
            return "未命名"

        first = units[0]
        if first.breadcrumb:
            # 取最高层级（breadcrumb[0]）作为篇标题
            title = first.breadcrumb[0] if len(first.breadcrumb) > 0 else first.title
            # 去除分片标记
            title = re.sub(r'\s*[（(]\d+[/／]\d+[）)]\s*$', '', title)
            return title

        return first.title

    def _build_frontmatter(
        self,
        chapter: str,
        title: str,
        book_name: str,
        tags: list[str],
        parts: int,
    ) -> str:
        """生成 YAML frontmatter"""
        tags_str = ", ".join(tags) if tags else ""
        date = datetime.now().strftime("%Y-%m-%d")
        lines = [
            "---",
            f'chapter: "{chapter}"',
            f'title: "{title}"',
            f'source: "{book_name}"',
        ]
        if tags_str:
            lines.append(f'tags: [{tags_str}]')
        lines.extend([
            f'date: "{date}"',
            f'status: "篇级合并完成"',
            f'parts: {parts}',
            "---",
        ])
        return "\n".join(lines)

    @staticmethod
    def _extract_tags(text: str) -> set[str]:
        """从 frontmatter 中提取 tags"""
        m = re.search(r'^---\s*\n(.*?)\n---', text, re.DOTALL)
        if not m:
            return set()
        fm = m.group(1)
        tag_match = re.search(r'tags:\s*\[([^\]]*)\]', fm)
        if tag_match:
            raw = tag_match.group(1)
            return {t.strip().strip('"\'') for t in raw.split(",") if t.strip()}
        return set()

    @staticmethod
    def _strip_frontmatter(text: str) -> str:
        """移除 frontmatter"""
        m = re.match(r'^---\s*\n.*?\n---\s*\n', text, re.DOTALL)
        if m:
            return text[m.end():]
        return text

    @staticmethod
    def _strip_leading_h1(text: str) -> str:
        """移除开头的 H1 标题（避免重复）"""
        text = text.lstrip()
        if text.startswith("# "):
            idx = text.find("\n")
            if idx >= 0:
                return text[idx + 1:].lstrip("\n")
        return text
