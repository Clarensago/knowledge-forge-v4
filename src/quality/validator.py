"""
输出质量校验器

复用 v3 validator.py 核心逻辑：6 项验证 + 5 维评分 + 结构化缺陷检测。
"""

import re
import logging
from typing import Optional

from ..core.enums import ChapterType

logger = logging.getLogger("knowledge-forge.quality")

# 板块标记
SECTION_MARKERS = {
    ChapterType.PREFACE: ["概述", "知识地图"],
    ChapterType.SHORT: ["核心洞察", "概念建模", "金句"],
    ChapterType.NORMAL: ["核心洞察", "概念建模", "因果链", "行为准则", "金句"],
    ChapterType.LONG: ["核心洞察", "概念建模", "因果链", "行为准则", "金句"],
}

LAZY_PATTERNS = re.compile(
    r"以此类推|不再赘述|详见原文|此处省略|如前所述|同理可得",
)

VALIDATION_CONFIG = {
    ChapterType.PREFACE: {"min_length": 500, "required_sections_min": 1, "min_links": 2},
    ChapterType.SHORT: {"min_length": 800, "required_sections_min": 2, "min_links": 3},
    ChapterType.NORMAL: {"min_length": 1500, "required_sections_min": 3, "min_links": 5},
    ChapterType.LONG: {"min_length": 2000, "required_sections_min": 4, "min_links": 8},
}


def validate_output(
    text: str, chapter_type: ChapterType,
    min_length: int = None, required_sections_min: int = None,
) -> tuple[bool, str]:
    """通过/拒绝验证"""
    cfg = VALIDATION_CONFIG.get(chapter_type, VALIDATION_CONFIG[ChapterType.NORMAL])
    ml = min_length or cfg["min_length"]
    rsm = required_sections_min or cfg["required_sections_min"]

    if len(text) < ml:
        return False, f"长度不足: {len(text)} < {ml}"

    markers = SECTION_MARKERS.get(chapter_type, SECTION_MARKERS[ChapterType.NORMAL])
    found = sum(1 for m in markers if m in text)
    if found < rsm:
        return False, f"结构不完整: {found}/{len(markers)} 板块 (需 ≥{rsm})"

    if not re.search(r"^#{1,3}\s+", text, re.MULTILINE):
        return False, "无 Markdown 标题"

    placeholders = len(re.findall(r"\{[^}]+\}", text))
    if placeholders > 10:
        return False, f"占位符过多: {placeholders}"

    lazy_count = len(LAZY_PATTERNS.findall(text))
    if lazy_count > 5:
        return False, f"偷懒表述过多: {lazy_count}"

    return True, "通过"


def get_quality_score(text: str, chapter_type: ChapterType) -> float:
    """5 维评分（0.0~1.0）"""
    score = 0.0
    text_len = len(text)

    # 长度（0.25）
    if text_len >= 3000:
        score += 0.25
    elif text_len >= 1500:
        score += 0.15
    elif text_len >= 500:
        score += 0.05

    # 结构完整性（0.35）
    markers = SECTION_MARKERS.get(chapter_type, SECTION_MARKERS[ChapterType.NORMAL])
    found = sum(1 for m in markers if m in text)
    score += 0.35 * (found / max(len(markers), 1))

    # Markdown 格式（0.15）
    fmt_elements = [
        r"^#\s+", r"^##\s+", r"^###\s+",
        r"^[-*]\s+", r"\*\*[^*]+\*\*",
        r"\|.*\|", r"^```", r"^>",
    ]
    fmt_count = sum(1 for p in fmt_elements if re.search(p, text, re.MULTILINE))
    score += 0.15 * min(fmt_count / 5, 1.0)

    # 双链（0.15）
    links = len(re.findall(r"\[\[[^\]]+\]\]", text))
    if links >= 8:
        score += 0.15
    elif links >= 5:
        score += 0.12
    elif links >= 3:
        score += 0.08
    elif links >= 1:
        score += 0.04

    # 金句（0.10）
    quotes = len(re.findall(r"^>", text, re.MULTILINE))
    if quotes >= 5:
        score += 0.10
    elif quotes >= 3:
        score += 0.07
    elif quotes >= 1:
        score += 0.03

    return round(min(score, 1.0), 3)


def detailed_validate(text: str, chapter_type: ChapterType) -> list[dict]:
    """结构化缺陷检测"""
    cfg = VALIDATION_CONFIG.get(chapter_type, VALIDATION_CONFIG[ChapterType.NORMAL])
    defects = []

    if len(text) < cfg["min_length"]:
        defects.append({
            "type": "length_short",
            "severity": "error",
            "message": f"输出过短: {len(text)} < {cfg['min_length']}",
        })

    markers = SECTION_MARKERS.get(chapter_type, SECTION_MARKERS[ChapterType.NORMAL])
    for m in markers:
        if m not in text:
            defects.append({
                "type": "missing_section",
                "severity": "warning",
                "message": f"缺少板块: {m}",
            })

    links = len(re.findall(r"\[\[[^\]]+\]\]", text))
    if links < cfg["min_links"]:
        defects.append({
            "type": "low_links",
            "severity": "warning",
            "message": f"双链不足: {links} < {cfg['min_links']}",
        })

    for match in LAZY_PATTERNS.finditer(text):
        defects.append({
            "type": "lazy_content",
            "severity": "warning",
            "message": f"偷懒表述: '{match.group()}'",
        })

    return defects
