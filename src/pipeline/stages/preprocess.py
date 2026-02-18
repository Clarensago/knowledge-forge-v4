"""
Stage 0: 预处理阶段

调用 Extractor → StructureAnalyzer → ChunkPlanner，
将结果写入 PipelineContext 和 staging 目录。
"""

import logging

from ..context import PipelineContext
from ...core.enums import ProcessingStatus
from ...extraction.epub_extractor import EpubExtractor
from ...extraction.pdf_extractor import PdfExtractor
from ...structure.analyzer import StructureAnalyzer
from ...structure.chunk_planner import ChunkPlanner
from ...infra.config import Config
from ...infra.file_store import FileStore
from ...infra.progress import ProgressManager
from ...llm.client import LLMClient

logger = logging.getLogger("knowledge-forge.stage0")

# 提取器注册表
EXTRACTORS = {
    ".epub": EpubExtractor,
    ".pdf": PdfExtractor,
}


class PreprocessStage:
    """Stage 0: 预处理"""

    name = "preprocess"

    def __init__(
        self, config: Config, llm_client: LLMClient,
        file_store: FileStore, progress: ProgressManager,
    ):
        self.config = config
        self.llm = llm_client
        self.file_store = file_store
        self.progress = progress

    def process(self, context: PipelineContext):
        book = context.book
        source = context.source_file

        # 1. 文本提取
        ext = source.rsplit(".", 1)[-1].lower()
        ext = f".{ext}"
        if ext not in EXTRACTORS:
            raise ValueError(f"不支持的文件格式: {ext}")

        extractor = EXTRACTORS[ext]()
        logger.info(f"提取文本: {source}")
        result = extractor.extract(source)
        context.extraction_result = result
        context.full_text = result.text

        logger.info(f"文本提取完成: {len(result.text)} 字")

        if context.check_stop():
            return

        # 2. 结构解析
        analyzer = StructureAnalyzer(self.config, self.llm)
        structure = analyzer.analyze(result.text, result)
        context.structure = structure

        if structure:
            book.structure = structure
            self.file_store.write_structure(book.safe_name, structure.to_dict())
            logger.info(f"结构解析完成: {structure.source.value}, "
                        f"{len(structure.leaf_nodes())} 个叶子节点")
        else:
            logger.warning("结构解析失败，将使用兜底分块")

        if context.check_stop():
            return

        # 3. 分块规划
        planner = ChunkPlanner(self.config)
        units = planner.plan(structure, result.text) if structure else planner._fallback_split(result.text)
        context.units = units
        book.units = units

        if context.check_stop():
            return

        # 4. 写入 staging
        for unit in units:
            filename = f"{unit.id}_{unit.title}.txt"
            filename = FileStore.sanitize_filename(unit.id, unit.title, ".txt")
            self.file_store.write_unit_text(book.safe_name, filename, unit.text)

        # 5. 更新状态
        book.status = ProcessingStatus.STAGE0_DONE
        self.progress.save_book(book)
        logger.info(f"Stage 0 完成: {len(units)} 个处理单元")
