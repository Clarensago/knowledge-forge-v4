"""
Stage 1: 全书通读 — BookMind 构建

4 轮 AI 对话构建全书认知地图（复用 v3 核心逻辑）：
  Round 1: 全书脉络 + 处理策略
  Round 2: 逐章元数据标注
  Round 3: 概念词典（≤50 条）
  Round 4: 章间关联矩阵
Round 3+4 可并行执行。
"""

import json
import re
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Optional, Union

from ..context import PipelineContext
from ...core.models import BookMind, ChapterMeta, ProcessingStrategy, ProcessingUnit
from ...core.enums import ProcessingStatus, ChapterType
from ...infra.config import Config
from ...infra.file_store import FileStore
from ...infra.progress import ProgressManager
from ...llm.client import LLMClient

logger = logging.getLogger("knowledge-forge.stage1")


class MindBuilderStage:
    """Stage 1: BookMind 构建"""

    name = "mind_builder"

    def __init__(self, config: Config, llm_client: LLMClient,
                 file_store: FileStore, progress: ProgressManager):
        self.config = config
        self.llm = llm_client
        self.file_store = file_store
        self.progress = progress

    def process(self, context: PipelineContext):
        book = context.book
        units = context.units or book.units

        # 尝试加载已有 BookMind
        existing = self.file_store.read_book_mind(book.safe_name)
        if existing:
            context.book_mind = BookMind.from_dict(existing)
            book.book_mind = context.book_mind
            logger.info("已加载现有 BookMind，跳过 Stage 1")
            book.status = ProcessingStatus.STAGE1_DONE
            self.progress.save_book(book)
            return

        overview = self._build_overview(units)

        # Round 1: 全书脉络
        logger.info("Stage 1 Round 1/4: 全书脉络...")
        synopsis, strategy = self._round1(overview)

        # Round 2: 逐章标注
        context.check_pause()
        logger.info("Stage 1 Round 2/4: 逐章标注...")
        chapter_metas = self._round2(synopsis, overview, units)

        # Round 3+4: 概念词典 + 关联矩阵（可并行）
        context.check_pause()
        metas_summary = self._build_metas_summary(chapter_metas)

        concepts = {}
        relations = []

        logger.info("Stage 1 Round 3-4/4: 概念词典 + 关联矩阵...")
        with ThreadPoolExecutor(max_workers=2) as pool:
            f3 = pool.submit(self._round3, synopsis, metas_summary)
            f4 = pool.submit(self._round4, synopsis, metas_summary)
            concepts = f3.result() or {}
            relations = f4.result() or []

        # 组装 BookMind
        book_mind = BookMind(
            synopsis=synopsis,
            chapter_metas=chapter_metas,
            concepts=concepts,
            relations=relations,
            strategy=strategy,
        )

        # 持久化
        self.file_store.write_book_mind(book.safe_name, book_mind.to_dict())
        context.book_mind = book_mind
        book.book_mind = book_mind
        book.status = ProcessingStatus.STAGE1_DONE
        self.progress.save_book(book)
        logger.info(f"Stage 1 完成: {len(chapter_metas)} 章标注, "
                    f"{len(concepts)} 概念, {len(relations)} 关联")

    def _build_overview(self, units: list[ProcessingUnit]) -> str:
        """构建章节概览（每章标题+前300字）"""
        parts = []
        for u in units:
            preview = u.text[:300].replace("\n", " ")
            title = " > ".join(u.breadcrumb) if u.breadcrumb else u.id
            parts.append(f"[{u.id}] {title}\n{preview}...")
        return "\n\n".join(parts)

    def _build_metas_summary(self, metas: dict[str, ChapterMeta]) -> str:
        lines = []
        for k, m in sorted(metas.items()):
            lines.append(f"[{k}] {m.theme} ({m.difficulty}) — {m.summary[:80]}")
        return "\n".join(lines)

    def _round1(self, overview: str) -> tuple[str, ProcessingStrategy]:
        prompt = f"""你是一位全书通读专家。请阅读以下书籍的章节概览，然后：

1. 撰写一段 500 字左右的「全书脉络」(synopsis)，概括全书的核心论点、结构逻辑和知识体系
2. 给出处理策略建议（JSON 格式）

章节概览：
{overview}

请按以下 JSON 格式输出：
```json
{{
  "synopsis": "全书脉络文本...",
  "strategy": {{
    "temperature": 0.2,
    "template_name": "full",
    "special_instructions": "（如有需要补充的特殊指令）",
    "skip_round2": false
  }}
}}
```"""
        response = self.llm.generate(
            prompt=prompt, temperature=0.3, max_tokens=2000,
            stream=True, task_label="S1-R1",
        )
        data = self._parse_json(response) or {}
        synopsis = data.get("synopsis", response[:500])
        strategy = ProcessingStrategy.from_dict(data.get("strategy", {}))
        return synopsis, strategy

    def _round2(self, synopsis: str, overview: str,
                units: list[ProcessingUnit]) -> dict[str, ChapterMeta]:
        unit_list = "\n".join(
            f"[{u.id}] {' > '.join(u.breadcrumb)} ({u.word_count}字)"
            for u in units
        )
        prompt = f"""基于全书脉络和章节概览，为每个章节生成元数据标注。

全书脉络：
{synopsis}

章节列表：
{unit_list}

章节概览：
{overview[:8000]}

请按以下 JSON 格式输出：
```json
{{
  "01": {{
    "theme": "主题标签",
    "difficulty": "basic/intermediate/advanced",
    "chapter_type": "preface/short/normal/long",
    "summary": "100字以内摘要",
    "key_concepts": ["概念1", "概念2"],
    "related_chapters": ["02", "05"]
  }}
}}
```"""
        response = self.llm.generate(
            prompt=prompt, temperature=0.2, max_tokens=8000,
            stream=True, task_label="S1-R2",
        )
        data = self._parse_json(response) or {}
        metas = {}
        for k, v in data.items():
            if isinstance(v, dict):
                metas[k] = ChapterMeta.from_dict(v)
        return metas

    def _round3(self, synopsis: str, metas_summary: str) -> dict[str, str]:
        prompt = f"""基于全书脉络和章节概览，构建全书概念词典。

全书脉络：{synopsis}

章节概览：{metas_summary}

请输出一个 JSON 对象，key 为概念名，value 为 50 字以内的定义。最多 50 个条目。
只输出 JSON。"""
        response = self.llm.generate(
            prompt=prompt, temperature=0.2, max_tokens=4000,
            stream=True, task_label="S1-R3",
        )
        return self._parse_json(response) or {}

    def _round4(self, synopsis: str, metas_summary: str) -> list[dict]:
        prompt = f"""基于全书脉络和章节概览，识别章节间的关联关系。

全书脉络：{synopsis}

章节概览：{metas_summary}

请输出一个 JSON 数组，每个元素格式：
{{"from": "01", "to": "03", "relation": "递进/因果/对比/补充", "description": "简述关联"}}

只输出 JSON 数组，最多 30 条。"""
        response = self.llm.generate(
            prompt=prompt, temperature=0.3, max_tokens=4000,
            stream=True, task_label="S1-R4",
        )
        data = self._parse_json(response)
        return data if isinstance(data, list) else []

    @staticmethod
    def _parse_json(text: str) -> Optional[Union[dict, list]]:
        """三级 JSON 提取"""
        text = text.strip()
        # 直接解析
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
        # ```json 块
        m = re.search(r"```(?:json)?\s*\n(.*?)\n```", text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(1))
            except json.JSONDecodeError:
                pass
        # 找首尾括号
        for open_c, close_c in [("{", "}"), ("[", "]")]:
            start = text.find(open_c)
            end = text.rfind(close_c)
            if start >= 0 and end > start:
                try:
                    return json.loads(text[start:end + 1])
                except json.JSONDecodeError:
                    pass
        return None
