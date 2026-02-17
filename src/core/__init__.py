"""核心领域模型"""

from .enums import (
    ChapterType,
    RouteLevel,
    ProcessingStatus,
    StructureSource,
)
from .models import (
    TOCNode,
    BookStructure,
    ExtractionResult,
    ProcessingUnit,
    ChapterMeta,
    ProcessingStrategy,
    ChapterRoute,
    BookMind,
    Book,
)
from .protocols import (
    StructureStrategy,
    Stage,
    Extractor,
    LLMClientProtocol,
)

__all__ = [
    "ChapterType", "RouteLevel", "ProcessingStatus", "StructureSource",
    "TOCNode", "BookStructure", "ExtractionResult", "ProcessingUnit",
    "ChapterMeta", "ProcessingStrategy", "ChapterRoute", "BookMind", "Book",
    "StructureStrategy", "Stage", "Extractor", "LLMClientProtocol",
]
