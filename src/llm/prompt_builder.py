"""
Prompt 构建器

从配置目录加载 prompt 模板，构建各阶段的 system/user prompt。
替代 v3 prompt_factory.py 的硬编码方式。
"""

import logging
from pathlib import Path
from typing import Optional

from ..core.models import ProcessingUnit
from ..core.enums import ChapterType
from ..infra.config import Config

logger = logging.getLogger("knowledge-forge.llm.prompt")


# 内置默认 system prompt（配置文件不存在时的兜底）
DEFAULT_SYSTEM_ROLE = """你是一位通用知识架构师 (Knowledge Architect)。

你的任务是将书籍章节转化为高质量的结构化笔记，适用于 Obsidian 知识库。

## 思维路径
1. **去噪扫描**: 过滤重复/无关内容，聚焦核心论点
2. **逻辑骨架提取**: 识别因果链、层级关系、对比结构
3. **术语网络构建**: 用 [[双链]] 标注核心概念
4. **洞察提炼**: 提取作者的独到见解，用引用块标注金句
5. **实践转化**: 将理论映射为可执行的行为准则

## 约束
- 禁止复读机式摘抄，必须经过消化重组
- 禁止编造原文中没有的信息
- 禁止用"以此类推""不再赘述"等偷懒表述
- 禁止省略重要细节
- 每个概念首次出现时用 [[双链]] 标注
- 金句必须是原文中的精彩表述，用引用块格式"""


class PromptBuilder:
    """Prompt 构建器"""

    def __init__(self, config: Config):
        self.config = config
        self._prompts_dir = config.root / "config" / "prompts"
        self._templates_dir = config.root / "templates"
        self._cache: dict[str, str] = {}

    def _load_prompt(self, name: str, default: str = "") -> str:
        """从配置目录加载 prompt 模板，支持缓存"""
        if name in self._cache:
            return self._cache[name]
        path = self._prompts_dir / name
        if path.exists():
            content = path.read_text(encoding="utf-8").strip()
            self._cache[name] = content
            return content
        self._cache[name] = default
        return default

    def _load_template(self, name: str) -> str:
        """加载输出模板"""
        if name in self._cache:
            return self._cache[name]
        path = self._templates_dir / name
        if path.exists():
            content = path.read_text(encoding="utf-8").strip()
            self._cache[name] = content
            return content
        self._cache[name] = ""
        return ""

    def build_chapter_prompt(
        self, book_name: str, unit: ProcessingUnit,
        book_mind_context: dict,
    ) -> tuple[str, str]:
        """构建 Round 1 初稿生成 prompt"""
        system_role = self._load_prompt("system_role.md", DEFAULT_SYSTEM_ROLE)

        # 加载输出模板
        template_map = {
            ChapterType.PREFACE: "preface_template.md",
            ChapterType.SHORT: "compact_template.md",
            ChapterType.NORMAL: "full_template.md",
            ChapterType.LONG: "full_template.md",
        }
        template = self._load_template(template_map.get(unit.chapter_type, "full_template.md"))
        fewshot = self._load_template("fewshot_example.md")

        # 任务要求
        task_req = self._get_task_requirements(unit.chapter_type)

        # system prompt
        system_prompt = f"""{system_role}

{task_req}

{f'## 输出模板{chr(10)}{template}' if template else ''}
{f'## 范例{chr(10)}{fewshot}' if fewshot else ''}"""

        # user prompt
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
        """构建定向修补 prompt"""
        system_prompt = """你是一位精准修补专家。

修补原则：
1. 最小改动：只修复缺陷点，不改动已通过的部分
2. 保持风格：与原笔记的语言风格和结构保持一致
3. 基于原文：所有新增内容必须有原文依据
4. 完整输出：输出完整的修补后笔记（不是 diff）"""

        defect_lines = []
        for d in defects:
            severity = d.get("severity", "warning")
            dtype = d.get("type", "unknown")
            msg = d.get("message", "")
            defect_lines.append(f"- [{severity}] {dtype}: {msg}")

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
        """构建全文精修 prompt"""
        system_prompt = """你是一位质量审核专家。请对以下笔记进行五维审核并输出改进版本：

1. 幻觉检测：是否有编造的内容？
2. 遗漏检查：是否遗漏了重要论点？
3. 偷懒检测：是否有"以此类推"等偷懒表述？
4. 格式检查：Markdown 格式是否规范？
5. 信息密度：是否有冗余或低信息量段落？

直接输出改进后的完整笔记。"""

        user_prompt = f"""## 原文
{unit.text[:5000]}

## 初稿笔记
{unit.round1_output}"""

        return system_prompt, user_prompt

    @staticmethod
    def _get_task_requirements(chapter_type: ChapterType) -> str:
        """按章节类型返回差异化任务要求"""
        if chapter_type == ChapterType.PREFACE:
            return """## 任务要求（前言类）
输出 3 个板块：概述 + 知识地图 + 阅读建议。"""

        if chapter_type == ChapterType.SHORT:
            return """## 任务要求（短章节）
输出 5 个板块：核心洞察 + 概念建模 + 金句收录 + 行为准则 + 本篇要义。
要求：≥800字，≥3个双链。"""

        if chapter_type == ChapterType.LONG:
            return """## 任务要求（超长章节）
输出完整 9 大板块，并附加：
1. 每个子论点独立总结
2. 概念定义 ≥5 个
3. 因果链 ≥3 条
4. 行为准则 ≥5 条
要求：≥4000字，≥8个双链，≥5个金句。"""

        # NORMAL
        return """## 任务要求（标准章节）
输出完整 9 大板块：核心洞察 + 概念建模 + 关系图谱 + 因果链 + 本土映射 + 行为准则 + 金句收录 + 延伸思考 + 本篇要义。
要求：≥3000字，≥8个双链，≥5个金句。"""

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
