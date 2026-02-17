"""
Stage 3: 全书回溯生成器

收集全部处理单元的 MD 产出 + BookMind，生成四类全书级产品：
1. 全书导读（综合评述）
2. 知识图谱（概念关系 Mermaid 图）
3. 学习路径（推荐阅读序列）
4. 概念速查手册（纯本地，不调用 LLM）

v4 改进：
- 产品 1/2/3 并行生成（输入互不依赖）
- 概念速查可独立提前调用（generate_glossary_standalone）
- 利用 ProcessingUnit.breadcrumb 构建更结构化的全书摘要
"""

import json
import re
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from ..context import PipelineContext
from ...core.models import Book, BookMind, ProcessingUnit
from ...core.enums import ProcessingStatus
from ...infra.config import Config
from ...infra.file_store import FileStore
from ...infra.progress import ProgressManager
from ...llm.client import LLMClient

logger = logging.getLogger("knowledge-forge.stage3")


class RetrospectorStage:
    """Stage 3: 全书回溯生成器"""

    name = "retrospector"

    def __init__(self, config: Config, llm_client: LLMClient,
                 file_store: FileStore, progress: ProgressManager):
        self.config = config
        self.llm = llm_client
        self.file_store = file_store
        self.progress = progress

    def process(self, context: PipelineContext):
        book = context.book
        book_mind = context.book_mind or book.book_mind
        units = book.units

        if not book_mind:
            logger.error("BookMind 未构建，跳过 Stage 3")
            return

        logger.info(f"{'=' * 50}")
        logger.info(f"Stage 3 全书回溯: {book.name}")
        logger.info(f"{'=' * 50}")

        # 收集所有单元的最终产出
        unit_outputs = self._collect_outputs(book)
        if not unit_outputs:
            logger.error("没有可用的章节产出，跳过 Stage 3")
            return

        # 构建全书摘要
        chapters_digest = self._build_digest(unit_outputs)

        context.check_pause()

        # 并行生成三个 LLM 产品 + 一个本地产品
        guide = None
        graph = None
        path = None

        logger.info("生成全书级产品（导读/图谱/路径 并行）...")
        with ThreadPoolExecutor(max_workers=3) as pool:
            f_guide = pool.submit(
                self._generate_guide, book.name, book_mind, chapters_digest)
            f_graph = pool.submit(
                self._generate_knowledge_graph, book.name, book_mind)
            f_path = pool.submit(
                self._generate_learning_path, book.name, book_mind, chapters_digest)

            guide = f_guide.result()
            graph = f_graph.result()
            path = f_path.result()

        # 概念速查（纯本地）
        glossary = self.generate_glossary_standalone(book.name, book_mind)

        # 写出
        products = [
            ("00_全书导读.md", guide),
            ("00_知识图谱.md", graph),
            ("00_学习路径.md", path),
            ("00_概念速查.md", glossary),
        ]
        count = 0
        for filename, content in products:
            if content:
                self.file_store.write_output_md(book.safe_name, filename, content)
                count += 1

        book.status = ProcessingStatus.STAGE3_DONE
        self.progress.save_book(book)
        logger.info(f"Stage 3 完成: {count}/4 个全书级产品已生成")

    def _collect_outputs(self, book: Book) -> dict[str, dict]:
        """收集所有处理单元的最终产出"""
        outputs = {}
        for u in book.units:
            if u.final_output:
                outputs[u.id] = {
                    "title": u.title,
                    "breadcrumb": u.breadcrumb,
                    "output": u.final_output,
                }
            else:
                # 从 outbox 尝试读取
                try:
                    md_files = self.file_store.list_outbox_files(book.safe_name)
                    for fname in md_files:
                        if fname.startswith(u.id):
                            content = self.file_store.read_outbox_md(
                                book.safe_name, fname)
                            outputs[u.id] = {
                                "title": u.title,
                                "breadcrumb": u.breadcrumb,
                                "output": content,
                            }
                            break
                except Exception:
                    pass
        return outputs

    def _build_digest(self, unit_outputs: dict[str, dict]) -> str:
        """从各单元产出中提取核心洞察构建全书摘要"""
        parts = []
        for idx, data in sorted(unit_outputs.items()):
            output = data["output"]
            # 提取核心洞察段落
            insight_match = re.search(
                r'## (?:🎯\s*)?核心洞察(.*?)(?=\n## |\n---|\Z)',
                output, re.DOTALL,
            )
            insight = insight_match.group(1).strip()[:500] if insight_match else output[:300]

            breadcrumb_str = " > ".join(data["breadcrumb"]) if data["breadcrumb"] else data["title"]
            parts.append(f"[{idx}] {breadcrumb_str}:\n{insight}")

        return "\n\n".join(parts)

    def _generate_guide(
        self, book_name: str, book_mind: BookMind, chapters_digest: str,
    ) -> Optional[str]:
        """生成全书导读"""
        prompt = f"""请为《{book_name}》生成一份全书导读。

## 全书脉络
{book_mind.synopsis}

## 各章核心洞察
{chapters_digest}

## 输出要求
1. 使用 Markdown 格式
2. 标题: # {book_name} - 全书导读
3. 包含以下部分:
   - **书籍概述**: 核心主题、写作背景、目标读者（200字）
   - **核心价值**: 本书最重要的 3-5 个知识贡献
   - **知识体系**: 全书知识结构和内在逻辑（用列表或层级展示）
   - **阅读指南**: 不同读者的推荐阅读路径
   - **章节索引**: 每章用 Obsidian 双链 [[章节标题]] 链接，附一句话概要
4. 用 Obsidian 双链标注所有核心概念
5. 全文不少于 2000 字

请直接输出 Markdown 内容。"""

        try:
            return self.llm.generate(
                prompt=prompt,
                temperature=0.3,
                max_tokens=8000,
                stream=self.config.stream,
                system_message="你是一位知识管理专家，擅长为书籍撰写导读和索引。",
            )
        except Exception as e:
            logger.error(f"全书导读生成失败: {e}")
            return None

    def _generate_knowledge_graph(
        self, book_name: str, book_mind: BookMind,
    ) -> Optional[str]:
        """生成知识图谱"""
        concepts_str = json.dumps(book_mind.concepts, ensure_ascii=False, indent=1)
        relations_str = json.dumps(book_mind.relations, ensure_ascii=False, indent=1)

        prompt = f"""请为《{book_name}》生成一份知识图谱文档。

## 核心概念词典
{concepts_str}

## 章间关联
{relations_str}

## 全书脉络
{book_mind.synopsis}

## 输出要求
1. 标题: # {book_name} - 知识图谱
2. **概念关系图**: 用 Mermaid graph 语法画出核心概念之间的关系（不超过 30 个节点）
3. **概念分类**: 将概念按领域/层级分组
4. **关键关系说明**: 对最重要的 5-10 组概念关系进行文字解读
5. 所有概念用 Obsidian 双链标注

请直接输出 Markdown 内容。"""

        try:
            return self.llm.generate(
                prompt=prompt,
                temperature=0.2,
                max_tokens=6000,
                stream=self.config.stream,
                system_message="你是一位知识图谱专家，擅长用 Mermaid 语法构建概念关系图。",
            )
        except Exception as e:
            logger.error(f"知识图谱生成失败: {e}")
            return None

    def _generate_learning_path(
        self, book_name: str, book_mind: BookMind, chapters_digest: str,
    ) -> Optional[str]:
        """生成学习路径"""
        chapter_info = []
        for idx, meta in sorted(book_mind.chapter_metas.items()):
            chapter_info.append(
                f"[{idx}] 主题:{meta.theme} 难度:{meta.difficulty} "
                f"类型:{meta.chapter_type.value}"
            )
        chapters_str = "\n".join(chapter_info)

        prompt = f"""请为《{book_name}》生成学习路径推荐。

## 全书脉络
{book_mind.synopsis}

## 章节信息
{chapters_str}

## 输出要求
1. 标题: # {book_name} - 学习路径
2. **入门路径**: 零基础读者的推荐阅读序列（5-8 章），含理由
3. **进阶路径**: 有基础读者的推荐阅读序列
4. **精读路径**: 深度研究者的推荐阅读序列
5. **速查路径**: 查阅特定主题时应该翻阅哪些章节
6. 每条路径的章节用 Obsidian 双链标注
7. 附上章节间的前置知识关系说明

请直接输出 Markdown 内容。"""

        try:
            return self.llm.generate(
                prompt=prompt,
                temperature=0.3,
                max_tokens=4000,
                stream=self.config.stream,
                system_message="你是一位教育设计专家，擅长设计学习路径和课程序列。",
            )
        except Exception as e:
            logger.error(f"学习路径生成失败: {e}")
            return None

    def generate_glossary_standalone(
        self, book_name: str, book_mind: BookMind,
    ) -> Optional[str]:
        """
        生成概念速查手册 — 纯本地逻辑，不调用 LLM。

        可在 Stage 2 完成后提前独立调用。
        """
        if not book_mind.concepts:
            logger.info("无核心概念，跳过概念速查生成")
            return None

        lines = [f"# {book_name} - 概念速查手册\n"]
        lines.append(
            f"> 本手册收录了《{book_name}》中的 "
            f"{len(book_mind.concepts)} 个核心概念。\n"
        )
        lines.append("---\n")

        sorted_concepts = sorted(book_mind.concepts.items(), key=lambda x: x[0])

        current_initial = ""
        for name, definition in sorted_concepts:
            initial = name[0].upper() if name else "#"
            if initial != current_initial:
                current_initial = initial
                lines.append(f"\n## {current_initial}\n")

            # 查找概念出现的章节
            related = []
            for idx, meta in book_mind.chapter_metas.items():
                if name in meta.key_concepts:
                    related.append(idx)

            ref = ""
            if related:
                ref = " | 出现章节: " + ", ".join(sorted(related))

            lines.append(f"### [[{name}]]\n{definition}{ref}\n")

        return "\n".join(lines)
