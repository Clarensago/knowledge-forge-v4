"""
Prompt 构建器

从配置目录加载 prompt 模板，构建各阶段的 system/user prompt。
v4 核心设计：所有 prompt 模板外置到 config/prompts/ 目录，支持热修改。
"""

import re
import logging
from pathlib import Path
from typing import Optional

from ..core.models import ProcessingUnit
from ..core.enums import ChapterType
from ..infra.config import Config

logger = logging.getLogger("knowledge-forge.llm.prompt")


# 内置兜底（config/prompts/ 不存在时使用）
_FALLBACK_SYSTEM_ROLE = "你是一位通用知识架构师 (Knowledge Architect)，将书籍章节转化为 Obsidian 结构化笔记。"
_FALLBACK_FIX_SYSTEM = "你是一位精准修补专家。最小改动、保持风格、基于原文、完整输出。"
_FALLBACK_REFINE_SYSTEM = "你是一位质量审核专家。审核幻觉/遗漏/偷懒/格式/信息密度，直接输出改进版。"


class PromptBuilder:
    """
    配置驱动的 Prompt 构建器

    加载优先级：config/prompts/{name} 文件 > 硬编码兜底。
    多段 prompt 文件用 "---" 分隔，通过 ## section_name 标记段名。
    """

    def __init__(self, config: Config):
        self.config = config
        self._prompts_dir = config.root / "config" / "prompts"
        self._templates_dir = config.root / "templates"
        self._cache: dict[str, str] = {}
        self._sections_cache: dict[str, dict[str, str]] = {}

    # ━━━ 加载基础设施 ━━━

    def _load_prompt(self, name: str, default: str = "") -> str:
        """从 config/prompts/ 加载整个文件，带缓存"""
        if name in self._cache:
            return self._cache[name]
        path = self._prompts_dir / name
        if path.exists():
            content = path.read_text(encoding="utf-8").strip()
            self._cache[name] = content
            logger.debug(f"已加载 prompt: {name}")
            return content
        if default:
            logger.debug(f"prompt {name} 不存在，使用兜底")
        self._cache[name] = default
        return default

    def _load_sections(self, name: str) -> dict[str, str]:
        """
        加载多段 prompt 文件，按 "## section_name" 拆分。
        返回 {section_name: content} 字典。
        
        section_name 必须是 ASCII 标识符格式（字母+下划线+数字），
        中文标题（如 ## 全书脉络）被视为内容而非段名。
        
        文件格式：
          ## section_name_1
          content...
          
          ## section_name_2
          content...
        """
        if name in self._sections_cache:
            return self._sections_cache[name]

        raw = self._load_prompt(name)
        sections = {}
        if not raw:
            self._sections_cache[name] = sections
            return sections

        # 先去掉文件开头的 # 标题行
        lines = raw.split("\n")
        if lines and lines[0].startswith("# "):
            lines = lines[1:]
        raw_clean = "\n".join(lines)

        # 只在 ## ascii_identifier 处拆分（不拆分中文标题）
        parts = re.split(r'\n(?=## [a-zA-Z_]\w*\s*\n)', raw_clean)
        for part in parts:
            part = part.strip()
            if not part:
                continue
            # 去除 --- 分隔符
            part = re.sub(r'^---\s*\n?', '', part).strip()
            if not part:
                continue
            m = re.match(r'^##\s+([a-zA-Z_]\w*)\s*\n', part)
            if m:
                section_name = m.group(1)
                content = part[m.end():].strip()
                # 去除尾部 ---
                content = re.sub(r'\n---\s*$', '', content).strip()
                sections[section_name] = content

        self._sections_cache[name] = sections
        return sections

    def _load_template(self, name: str) -> str:
        """从 templates/ 加载输出模板"""
        cache_key = f"tpl:{name}"
        if cache_key in self._cache:
            return self._cache[cache_key]
        path = self._templates_dir / name
        if path.exists():
            content = path.read_text(encoding="utf-8").strip()
            self._cache[cache_key] = content
            return content
        self._cache[cache_key] = ""
        return ""

    def get_section(self, filename: str, section: str, default: str = "") -> str:
        """公共接口：获取某文件的某个段落（供 Stage 1/3 等直接调用）"""
        sections = self._load_sections(filename)
        return sections.get(section, default)

    # ━━━ Stage 2 Prompt 构建 ━━━

    def build_chapter_prompt(
        self, book_name: str, unit: ProcessingUnit,
        book_mind_context: dict,
    ) -> tuple[str, str]:
        """构建 Round 1 初稿生成 prompt"""
        system_role = self._load_prompt("system_role.md", _FALLBACK_SYSTEM_ROLE)

        template_map = {
            ChapterType.PREFACE: "preface_template.md",
            ChapterType.SHORT: "compact_template.md",
            ChapterType.NORMAL: "full_template.md",
            ChapterType.LONG: "full_template.md",
        }
        template = self._load_template(template_map.get(unit.chapter_type, "full_template.md"))
        fewshot = self._load_template("fewshot_example.md")

        task_req = self._get_task_requirements(unit.chapter_type)

        system_prompt = f"""{system_role}

{task_req}

{f'## 输出模板{chr(10)}{template}' if template else ''}
{f'## 范例{chr(10)}{fewshot}' if fewshot else ''}"""

        breadcrumb = " > ".join(unit.breadcrumb) if unit.breadcrumb else unit.id
        context_section = self._build_context_section(book_mind_context)

        user_prompt = f"""## 基本信息
- 书名: {book_name}
- 章节: [{unit.id}] {breadcrumb}
- 字数: {unit.word_count}
- 类型: {unit.chapter_type.value}

{context_section}

## 章节原文
{unit.text}"""

        return system_prompt, user_prompt

    def build_fix_prompt(
        self, unit: ProcessingUnit, defects: list[dict],
    ) -> tuple[str, str]:
        """构建定向修补 prompt（从 config/prompts/stage2_refine.md 加载）"""
        sections = self._load_sections("stage2_refine.md")
        system_prompt = sections.get("targeted_fix_system", _FALLBACK_FIX_SYSTEM)

        defect_lines = []
        for d in defects:
            severity = d.get("severity", "warning")
            dtype = d.get("type", "unknown")
            msg = d.get("message", "")
            defect_lines.append(f"- [{severity}] {dtype}: {msg}")

        user_template = sections.get("targeted_fix_user", "")
        if user_template and "{defect_list}" in user_template:
            user_prompt = user_template.format(
                defect_list=chr(10).join(defect_lines),
                original_text=unit.text[:3000],
                round1_output=unit.round1_output,
            )
        else:
            user_prompt = f"""## 缺陷清单
{chr(10).join(defect_lines)}

## 原文
{unit.text[:3000]}

## 待修补笔记
{unit.round1_output}"""

        return system_prompt, user_prompt

    def build_refine_prompt(
        self, unit: ProcessingUnit,
    ) -> tuple[str, str]:
        """构建全文精修 prompt（从 config/prompts/stage2_refine.md 加载）"""
        sections = self._load_sections("stage2_refine.md")
        system_prompt = sections.get("refine_system", _FALLBACK_REFINE_SYSTEM)

        user_template = sections.get("refine_user", "")
        if user_template and "{original_text}" in user_template:
            user_prompt = user_template.format(
                original_text=unit.text[:5000],
                round1_output=unit.round1_output,
            )
        else:
            user_prompt = f"""## 原文
{unit.text[:5000]}

## 初稿笔记
{unit.round1_output}"""

        return system_prompt, user_prompt

    def _get_task_requirements(self, chapter_type: ChapterType) -> str:
        """按章节类型返回差异化任务要求（从配置加载，硬编码兜底）"""
        sections = self._load_sections("stage2_refine.md")

        type_key_map = {
            ChapterType.PREFACE: "task_requirements_preface",
            ChapterType.SHORT: "task_requirements_short",
            ChapterType.NORMAL: "task_requirements_normal",
            ChapterType.LONG: "task_requirements_long",
        }
        key = type_key_map.get(chapter_type, "task_requirements_normal")
        loaded = sections.get(key, "")
        if loaded:
            return loaded

        # 兜底
        fallbacks = {
            ChapterType.PREFACE: "## 任务要求（前言类）\n输出 3 个板块：概述 + 知识地图 + 阅读建议。",
            ChapterType.SHORT: "## 任务要求（短章节）\n输出 5 个板块，≥800字，≥3个双链。",
            ChapterType.LONG: "## 任务要求（超长章节）\n输出 9 大板块，≥4000字，≥8个双链。",
        }
        return fallbacks.get(chapter_type,
            "## 任务要求（标准章节）\n输出 9 大板块，≥3000字，≥8个双链，≥5个金句。")

    @staticmethod
    def _build_context_section(ctx: dict) -> str:
        """构建 BookMind 上下文注入段"""
        parts = []
        if ctx.get("synopsis"):
            parts.append(f"## 全书脉络\n{ctx['synopsis']}")
        if ctx.get("neighbor_summaries"):
            lines = [f"- [{k}] {v}" for k, v in ctx["neighbor_summaries"].items()]
            parts.append(f"## 邻章摘要\n" + "\n".join(lines))
        if ctx.get("related_concepts"):
            lines = [f"- **{k}**: {v}" for k, v in ctx["related_concepts"].items()]
            parts.append(f"## 相关概念\n" + "\n".join(lines))
        if ctx.get("chapter_meta"):
            m = ctx["chapter_meta"]
            parts.append(f"## 本章定位\n- 主题: {m.get('theme','')}\n- 难度: {m.get('difficulty','')}")
        return "\n\n".join(parts) if parts else ""
