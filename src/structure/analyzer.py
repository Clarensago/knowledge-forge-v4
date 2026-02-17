"""
StructureAnalyzer — 结构解析调度器

按优先级调用三路策略（epub > llm > regex），返回第一个成功的 BookStructure。
"""

import logging
from typing import Optional

from ..core.models import BookStructure, ExtractionResult
from .epub_strategy import EpubStrategy
from .llm_strategy import LLMStrategy
from .regex_strategy import RegexStrategy

logger = logging.getLogger("knowledge-forge.structure.analyzer")


class StructureAnalyzer:
    """结构解析调度器"""

    def __init__(self, config, llm_client=None):
        self.config = config
        self.strategies = self._build_strategies(llm_client)

    def _build_strategies(self, llm_client) -> list:
        """按配置优先级构建策略列表"""
        priority = self.config.strategy_priority
        registry = {
            "epub_native": lambda: EpubStrategy(),
            "llm_toc": lambda: LLMStrategy(llm_client, self.config),
            "regex_detect": lambda: RegexStrategy(self.config),
        }
        strategies = []
        for name in priority:
            if name in registry:
                strategies.append((name, registry[name]()))
        return strategies

    def analyze(
        self, text: str, extraction_result: ExtractionResult,
    ) -> Optional[BookStructure]:
        """
        分析文本结构，返回 BookStructure。

        按策略优先级依次尝试，第一个成功的结果即为最终结果。
        """
        metadata = extraction_result.metadata

        for name, strategy in self.strategies:
            logger.info(f"尝试结构解析策略: {name}")
            try:
                result = strategy.analyze(text, metadata)
                if result and len(result.leaf_nodes()) >= 2:
                    logger.info(
                        f"结构解析成功 ({name}): "
                        f"{len(result.leaf_nodes())} 个叶子节点"
                    )
                    return result
                elif result:
                    logger.info(f"策略 {name} 结果不足（{len(result.leaf_nodes())} 叶子），跳过")
            except Exception as e:
                logger.warning(f"策略 {name} 异常: {e}")
                continue

        logger.warning("所有结构解析策略均失败")
        return None
