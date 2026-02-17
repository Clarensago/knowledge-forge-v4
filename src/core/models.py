"""
核心领域模型

v4 的核心改进：引入 TOCNode 树形结构 + ProcessingUnit 替代 v3 扁平 Chapter。
Book 仍为聚合根，但内部从 list[Chapter] 改为 BookStructure + list[ProcessingUnit]。
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional
from datetime import datetime

from .enums import (
    ChapterType, RouteLevel, ProcessingStatus, StructureSource,
)


# ━━━━━━━━━━━━━━━━━━━━ 结构层模型 ━━━━━━━━━━━━━━━━━━━━


@dataclass
class TOCNode:
    """
    目录树节点 — 书的骨架单元

    一棵 TOCNode 树完整描述书的逻辑层级：
      root(level=0) → 篇(level=1) → 章(level=2) → 节(level=3)

    text_start / text_end 指向全文中的字符偏移，由 StructureAnalyzer 填充。
    """
    title: str
    level: int = 0                          # 0=root, 1=篇/卷, 2=章, 3=节
    children: list[TOCNode] = field(default_factory=list)
    text_start: int = 0                     # 在全文中的起始字符位置
    text_end: int = 0                       # 结束位置
    source_id: str = ""                     # EPUB spine item id（可选）

    @property
    def word_count(self) -> int:
        return self.text_end - self.text_start

    @property
    def is_leaf(self) -> bool:
        return len(self.children) == 0

    def all_leaves(self) -> list[TOCNode]:
        """递归收集所有叶子节点"""
        if self.is_leaf:
            return [self]
        leaves = []
        for child in self.children:
            leaves.extend(child.all_leaves())
        return leaves

    def breadcrumb(self) -> list[str]:
        """从当前节点向上回溯祖先标题链（不含 root）"""
        # 需要 parent 引用，由 BookStructure._link_parents 建立
        trail = []
        node = self
        while node and node.level > 0:
            trail.append(node.title)
            node = getattr(node, "_parent", None)
        trail.reverse()
        return trail

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "level": self.level,
            "text_start": self.text_start,
            "text_end": self.text_end,
            "source_id": self.source_id,
            "children": [c.to_dict() for c in self.children],
        }

    @classmethod
    def from_dict(cls, data: dict) -> TOCNode:
        node = cls(
            title=data["title"],
            level=data.get("level", 0),
            text_start=data.get("text_start", 0),
            text_end=data.get("text_end", 0),
            source_id=data.get("source_id", ""),
        )
        for child_data in data.get("children", []):
            node.children.append(cls.from_dict(child_data))
        return node


@dataclass
class BookStructure:
    """
    书的完整骨架 — StructureAnalyzer 的产出物

    持有 TOCNode 根节点 + 来源标记。
    提供叶子节点遍历、breadcrumb 查询等便捷方法。
    """
    root: TOCNode
    source: StructureSource = StructureSource.REGEX
    metadata: dict = field(default_factory=dict)

    def __post_init__(self):
        self._link_parents(self.root, None)

    def _link_parents(self, node: TOCNode, parent: Optional[TOCNode]):
        """建立 parent 反向引用（不序列化）"""
        node._parent = parent  # type: ignore[attr-defined]
        for child in node.children:
            self._link_parents(child, node)

    def leaf_nodes(self) -> list[TOCNode]:
        """所有叶子节点的扁平列表"""
        return self.root.all_leaves()

    def breadcrumb_for(self, node: TOCNode) -> list[str]:
        """获取某节点的完整路径"""
        return node.breadcrumb()

    def total_word_count(self) -> int:
        return self.root.word_count

    def to_dict(self) -> dict:
        return {
            "source": self.source.value,
            "metadata": self.metadata,
            "tree": self.root.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict) -> BookStructure:
        return cls(
            root=TOCNode.from_dict(data["tree"]),
            source=StructureSource(data.get("source", "regex")),
            metadata=data.get("metadata", {}),
        )


# ━━━━━━━━━━━━━━━━━━━━ 提取层模型 ━━━━━━━━━━━━━━━━━━━━


@dataclass
class ExtractionResult:
    """
    文本提取结果 — Extractor 的产出物

    text: 全文纯文本
    metadata: 提取过程中收集的元数据
      - epub_toc: list[dict]  EPUB 原生 TOC 数据（可选）
      - title: str  书名
      - author: str  作者
    """
    text: str
    metadata: dict = field(default_factory=dict)


# ━━━━━━━━━━━━━━━━━━━━ 处理层模型 ━━━━━━━━━━━━━━━━━━━━


@dataclass
class ProcessingUnit:
    """
    LLM 处理单元 — ChunkPlanner 的产出物

    替代 v3 的扁平 Chapter。每个 unit 携带完整的层级上下文，
    即使是超长章节被切割为多个 unit，也知道自己属于哪篇哪章。
    """
    id: str                                  # "01-01", "02-03" 等
    text: str = ""
    word_count: int = 0
    breadcrumb: list[str] = field(default_factory=list)  # 层级路径
    toc_node: Optional[TOCNode] = None       # 回溯树节点（不序列化）
    is_partial: bool = False                 # 是否为超长章节的子切片
    part_index: int = 0                      # 子切片序号（0-based）
    total_parts: int = 1                     # 子切片总数

    # 处理状态
    chapter_type: ChapterType = ChapterType.NORMAL
    status: ProcessingStatus = ProcessingStatus.PENDING
    round1_output: str = ""
    round2_output: str = ""
    quality_score: float = 0.0
    retry_count: int = 0
    error_message: str = ""

    @property
    def final_output(self) -> str:
        return self.round2_output or self.round1_output

    @property
    def is_processable(self) -> bool:
        return (
            self.status not in (
                ProcessingStatus.STAGE2_DONE,
                ProcessingStatus.STAGE2_5_DONE,
                ProcessingStatus.STAGE3_DONE,
                ProcessingStatus.FAILED,
            )
            and self.retry_count < 3
        )

    @property
    def title(self) -> str:
        """最深层级的标题"""
        return self.breadcrumb[-1] if self.breadcrumb else self.id

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "word_count": self.word_count,
            "breadcrumb": self.breadcrumb,
            "is_partial": self.is_partial,
            "part_index": self.part_index,
            "total_parts": self.total_parts,
            "chapter_type": self.chapter_type.value,
            "status": self.status.value,
            "quality_score": self.quality_score,
            "retry_count": self.retry_count,
            "error_message": self.error_message,
        }

    @classmethod
    def from_dict(cls, data: dict) -> ProcessingUnit:
        return cls(
            id=data.get("id", ""),
            word_count=data.get("word_count", 0),
            breadcrumb=data.get("breadcrumb", []),
            is_partial=data.get("is_partial", False),
            part_index=data.get("part_index", 0),
            total_parts=data.get("total_parts", 1),
            chapter_type=ChapterType(data.get("chapter_type", "normal")),
            status=ProcessingStatus(data.get("status", "pending")),
            quality_score=data.get("quality_score", 0.0),
            retry_count=data.get("retry_count", 0),
            error_message=data.get("error_message", ""),
        )


# ━━━━━━━━━━━━━━━━━━━━ 认知层模型 ━━━━━━━━━━━━━━━━━━━━


@dataclass
class ChapterMeta:
    """章节元数据 — 由 Stage 1 (BookMind) 标注"""
    theme: str = ""
    difficulty: str = "intermediate"
    chapter_type: ChapterType = ChapterType.NORMAL
    summary: str = ""
    key_concepts: list[str] = field(default_factory=list)
    related_chapters: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "theme": self.theme,
            "difficulty": self.difficulty,
            "chapter_type": self.chapter_type.value,
            "summary": self.summary,
            "key_concepts": self.key_concepts,
            "related_chapters": self.related_chapters,
        }

    @classmethod
    def from_dict(cls, data: dict) -> ChapterMeta:
        return cls(
            theme=data.get("theme", ""),
            difficulty=data.get("difficulty", "intermediate"),
            chapter_type=ChapterType(data.get("chapter_type", "normal")),
            summary=data.get("summary", ""),
            key_concepts=data.get("key_concepts", []),
            related_chapters=data.get("related_chapters", []),
        )


@dataclass
class ProcessingStrategy:
    """处理策略 — 控制全局处理行为"""
    temperature: float = 0.2
    template_name: str = "full"
    special_instructions: str = ""
    skip_round2: bool = False

    def to_dict(self) -> dict:
        return {
            "temperature": self.temperature,
            "template_name": self.template_name,
            "special_instructions": self.special_instructions,
            "skip_round2": self.skip_round2,
        }

    @classmethod
    def from_dict(cls, data: dict) -> ProcessingStrategy:
        return cls(
            temperature=data.get("temperature", 0.2),
            template_name=data.get("template_name", "full"),
            special_instructions=data.get("special_instructions", ""),
            skip_round2=data.get("skip_round2", False),
        )


@dataclass
class ChapterRoute:
    """自适应路由结果 — 控制单章 LLM 资源分配"""
    route_level: RouteLevel = RouteLevel.STANDARD
    max_tokens: int = 8192
    temperature: float = 0.2
    skip_round2: bool = False

    def to_dict(self) -> dict:
        return {
            "route_level": self.route_level.value,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "skip_round2": self.skip_round2,
        }

    @classmethod
    def from_dict(cls, data: dict) -> ChapterRoute:
        return cls(
            route_level=RouteLevel(data.get("route_level", "standard")),
            max_tokens=data.get("max_tokens", 8192),
            temperature=data.get("temperature", 0.2),
            skip_round2=data.get("skip_round2", False),
        )


@dataclass
class BookMind:
    """
    全书认知地图 — Stage 1 核心产出

    由多轮 AI 对话构建，贯穿 Stage 2/3 作为全局上下文。
    """
    synopsis: str = ""
    chapter_metas: dict[str, ChapterMeta] = field(default_factory=dict)
    concepts: dict[str, str] = field(default_factory=dict)
    relations: list[dict] = field(default_factory=list)
    strategy: ProcessingStrategy = field(default_factory=ProcessingStrategy)

    def to_dict(self) -> dict:
        return {
            "synopsis": self.synopsis,
            "chapter_metas": {k: v.to_dict() for k, v in self.chapter_metas.items()},
            "concepts": self.concepts,
            "relations": self.relations,
            "strategy": self.strategy.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict) -> BookMind:
        chapter_metas = {
            k: ChapterMeta.from_dict(v)
            for k, v in data.get("chapter_metas", {}).items()
        }
        return cls(
            synopsis=data.get("synopsis", ""),
            chapter_metas=chapter_metas,
            concepts=data.get("concepts", {}),
            relations=data.get("relations", []),
            strategy=ProcessingStrategy.from_dict(data.get("strategy", {})),
        )

    def get_unit_context(
        self,
        unit_id: str,
        window: int = 1,
        semantic_neighbors: Optional[list[str]] = None,
    ) -> dict:
        """获取处理单元的上下文信息，注入 Stage 2 Prompt"""
        sorted_keys = sorted(self.chapter_metas.keys())
        try:
            idx = sorted_keys.index(unit_id)
        except ValueError:
            idx = -1

        neighbor_summaries = {}
        if semantic_neighbors:
            for key in semantic_neighbors:
                if key != unit_id and key in self.chapter_metas:
                    neighbor_summaries[key] = self.chapter_metas[key].summary
        elif idx >= 0:
            for offset in range(-window, window + 1):
                ni = idx + offset
                if 0 <= ni < len(sorted_keys) and ni != idx:
                    key = sorted_keys[ni]
                    neighbor_summaries[key] = self.chapter_metas[key].summary

        current_meta = self.chapter_metas.get(unit_id)
        related_concepts = {}
        if current_meta:
            for name in current_meta.key_concepts:
                if name in self.concepts:
                    related_concepts[name] = self.concepts[name]

        return {
            "synopsis": self.synopsis,
            "neighbor_summaries": neighbor_summaries,
            "related_concepts": related_concepts,
            "chapter_meta": current_meta.to_dict() if current_meta else None,
        }


# ━━━━━━━━━━━━━━━━━━━━ 聚合根 ━━━━━━━━━━━━━━━━━━━━


@dataclass
class Book:
    """
    书籍聚合根 — 一本书的完整加工生命周期

    v4 核心变化：内部持有 BookStructure（树形骨架）+ list[ProcessingUnit]。
    """
    name: str
    source_file: str
    safe_name: str = ""
    structure: Optional[BookStructure] = None
    units: list[ProcessingUnit] = field(default_factory=list)
    book_mind: Optional[BookMind] = None
    status: ProcessingStatus = ProcessingStatus.PENDING
    created_at: str = ""
    updated_at: str = ""

    def __post_init__(self):
        if not self.created_at:
            self.created_at = datetime.now().isoformat()
        if not self.updated_at:
            self.updated_at = self.created_at

    def get_pending_units(
        self, target_status: ProcessingStatus = ProcessingStatus.PENDING,
    ) -> list[ProcessingUnit]:
        return [u for u in self.units if u.status == target_status]

    def get_unit_by_id(self, unit_id: str) -> Optional[ProcessingUnit]:
        for u in self.units:
            if u.id == unit_id:
                return u
        return None

    def update_timestamp(self):
        self.updated_at = datetime.now().isoformat()

    def to_progress_dict(self) -> dict:
        result = {
            "name": self.name,
            "source_file": self.source_file,
            "safe_name": self.safe_name,
            "status": self.status.value,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "units": {u.id: u.to_dict() for u in self.units},
        }
        if self.structure:
            result["structure"] = self.structure.to_dict()
        return result

    @classmethod
    def from_progress_dict(cls, data: dict) -> Book:
        units = [
            ProcessingUnit.from_dict(u_data)
            for _, u_data in sorted(data.get("units", {}).items())
        ]
        structure = None
        if "structure" in data:
            structure = BookStructure.from_dict(data["structure"])
        return cls(
            name=data.get("name", ""),
            source_file=data.get("source_file", ""),
            safe_name=data.get("safe_name", ""),
            units=units,
            structure=structure,
            status=ProcessingStatus(data.get("status", "pending")),
            created_at=data.get("created_at", ""),
            updated_at=data.get("updated_at", ""),
        )
