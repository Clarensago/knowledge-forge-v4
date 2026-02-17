"""
枚举定义

所有业务枚举集中管理，避免散落各处。
"""

from enum import Enum


class ChapterType(Enum):
    """章节类型 — 决定使用哪套模板和 Prompt 策略"""
    PREFACE = "preface"      # 前言/目录/序言 → preface_template
    SHORT = "short"          # 极短章节 < 2000字 → compact_template
    NORMAL = "normal"        # 正常章节 2000-15000字 → full_template
    LONG = "long"            # 超长章节 > 15000字 → full_template + 深度分析


class RouteLevel(Enum):
    """自适应路由级别 — 按章节难度/长度差异化分配 LLM 资源"""
    LIGHT = "light"          # <4000字 + basic
    STANDARD = "standard"    # 默认
    HEAVY = "heavy"          # >8000字 + advanced


class ProcessingStatus(Enum):
    """处理状态 — 支持断点续作的状态机"""
    PENDING = "pending"
    STAGE0_DONE = "stage0_done"
    STAGE1_DONE = "stage1_done"
    STAGE2_ROUND1 = "stage2_round1"
    STAGE2_DONE = "stage2_done"
    STAGE2_5_DONE = "stage2_5_done"
    STAGE3_DONE = "stage3_done"
    FAILED = "failed"


class StructureSource(Enum):
    """结构解析来源 — 标识 BookStructure 是如何得到的"""
    EPUB_NATIVE = "epub_native"   # EPUB toc.ncx / nav.xhtml
    LLM_TOC = "llm_toc"          # LLM 解析目录页
    REGEX = "regex"              # 正则标题模式检测
    MANUAL = "manual"            # 手动指定
