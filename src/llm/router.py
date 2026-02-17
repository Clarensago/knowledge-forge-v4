"""
自适应路由器

根据章节字数和难度分配 LLM 资源（复用 v3 逻辑）。
"""

import logging

from ..core.models import ProcessingUnit, ChapterMeta, ChapterRoute
from ..core.enums import ChapterType, RouteLevel
from ..infra.config import Config

logger = logging.getLogger("knowledge-forge.llm.router")


class ChapterRouter:
    """自适应路由器"""

    def __init__(self, config: Config):
        self.config = config

    def route(self, unit: ProcessingUnit, meta: ChapterMeta = None) -> ChapterRoute:
        if not self.config.adaptive_routing_enabled:
            return ChapterRoute()

        # 前言类：轻量 + 跳过 Round 2
        if unit.chapter_type == ChapterType.PREFACE:
            return ChapterRoute(
                route_level=RouteLevel.LIGHT,
                max_tokens=4096,
                temperature=0.15,
                skip_round2=True,
            )

        difficulty = meta.difficulty if meta else "intermediate"
        wc = unit.word_count
        level = self._determine_level(wc, difficulty)

        route_params = {
            RouteLevel.LIGHT: (4096, 0.15),
            RouteLevel.STANDARD: (8192, 0.2),
            RouteLevel.HEAVY: (16384, 0.25),
        }
        max_tokens, temp = route_params[level]

        route = ChapterRoute(
            route_level=level,
            max_tokens=max_tokens,
            temperature=temp,
        )
        logger.debug(
            f"路由: {unit.chapter_type.value} ({wc}字, {difficulty}) "
            f"→ {level.value} (max_tokens={max_tokens}, temp={temp})"
        )
        return route

    @staticmethod
    def _determine_level(word_count: int, difficulty: str) -> RouteLevel:
        if word_count < 4000 and difficulty == "basic":
            return RouteLevel.LIGHT
        if word_count > 8000 and difficulty == "advanced":
            return RouteLevel.HEAVY
        return RouteLevel.STANDARD
