"""
后处理器（PostProcessor）

对已完成 Stage 2+ 的处理单元，提供两种后处理模式：
1. 整合压缩：将多个章节合并为 N 篇结构化笔记
2. 单篇优化：逐篇优化格式与结构，不合并内容

从 v3 移植并适配 v4 领域模型（ProcessingUnit / FileStore / ProgressManager）。
"""

import re
import logging
import threading
from typing import Optional
from datetime import date
from concurrent.futures import ThreadPoolExecutor, as_completed

from ..core.models import Book
from ..core.enums import ProcessingStatus

logger = logging.getLogger("knowledge-forge.postprocess")

# 合并内容的最大字符数限制（避免超出 LLM 上下文窗口）
MAX_AGGREGATE_CHARS = 60000
# 单篇优化的最大字符数
MAX_SINGLE_CHARS = 30000


class PostProcessor:
    """
    后处理器 — 对已完成章节进行合并梳理或单篇优化

    通过构造函数注入基础设施依赖。
    """

    def __init__(self, llm_client, file_store, progress_manager, concurrency: int = 1):
        self.llm_client = llm_client
        self.file_store = file_store
        self.progress_manager = progress_manager
        self.concurrency = max(1, concurrency)

    def get_eligible_units(self, book: Book) -> list:
        """获取可用于后处理的处理单元列表。"""
        eligible = []
        done_statuses = {
            ProcessingStatus.STAGE2_DONE,
            ProcessingStatus.STAGE2_5_DONE,
            ProcessingStatus.STAGE3_DONE,
            ProcessingStatus.POST_PROCESSED,
        }

        for unit in book.units:
            if unit.status in done_statuses:
                has_output = self._find_unit_file(book.safe_name, unit.id) is not None
                eligible.append({
                    "index": unit.id,
                    "title": unit.title,
                    "status": unit.status.value,
                    "has_output": has_output,
                    "char_count": unit.word_count,
                })

        return eligible

    # ──────────────────────────────────────────────
    # 模式一：整合压缩
    # ──────────────────────────────────────────────

    def merge_chapters(
        self,
        book: Book,
        unit_ids: list,
        output_title: Optional[str] = None,
        output_index: Optional[str] = None,
        instructions: str = "",
        target_count: Optional[int] = None,
        temperature: float = 0.3,
        stream: bool = True,
    ) -> dict:
        """
        整合压缩选中的多个处理单元为 target_count 篇。

        Args:
            target_count: 目标篇数。None 则自动预判（3~7 篇）。
        """
        if len(unit_ids) < 1:
            return {"success": False, "output_file": "", "message": "至少需要选择 1 个章节"}

        # 1. 聚合内容
        logger.info(f"[PostProcessor] 聚合 {len(unit_ids)} 个章节内容...")
        contents, units_info = self._aggregate_content(book, unit_ids)

        if not contents:
            return {"success": False, "output_file": "", "message": "未找到任何章节内容"}

        # 2. 长度检查
        total_chars = sum(len(c) for c in contents)
        if total_chars > MAX_AGGREGATE_CHARS:
            return {
                "success": False,
                "output_file": "",
                "message": f"合并内容过长（{total_chars} 字），超过限制 {MAX_AGGREGATE_CHARS} 字。请减少选择的章节数量。",
            }

        # 3. 自动预判目标篇数
        if target_count is None:
            target_count = self._estimate_target_count(total_chars)
        target_count = max(1, min(target_count, len(contents)))
        logger.info(f"[PostProcessor] 总字数 {total_chars}，目标压缩为 {target_count} 篇")

        # 4. 确定输出标题和编号
        if not output_title:
            output_title = units_info[0]["title"] if units_info else "合并笔记"
            output_title = re.sub(r'（\d+/\d+）$', '', output_title).strip()

        if not output_index:
            output_index = units_info[0]["index"] if units_info else "PP"

        # 5. 构建 Prompt 并调用 LLM
        system_prompt = self._build_merge_system_prompt(book.name, target_count)
        user_prompt = self._build_merge_user_prompt(
            contents, units_info, output_title, instructions, target_count
        )

        max_tokens = self._calc_max_tokens(total_chars, target_count)
        logger.info(f"[PostProcessor] 调用 LLM，max_tokens={max_tokens}...")

        try:
            result = self.llm_client.generate(
                prompt=user_prompt,
                system_message=system_prompt,
                temperature=temperature,
                max_tokens=max_tokens,
                stream=stream,
            )
        except Exception as e:
            logger.error(f"[PostProcessor] LLM 调用失败: {e}")
            return {"success": False, "output_file": "", "message": f"LLM 调用失败: {e}"}

        if not result or len(result.strip()) < 200:
            return {"success": False, "output_file": "", "message": "LLM 产出内容过短，请重试"}

        # 6. 写入文件
        if target_count == 1:
            final_content = self._build_output_md(
                result, output_title, output_index, book.name, units_info, "merged"
            )
            output_filename = self.file_store.sanitize_filename(output_index, f"（整合）{output_title}")
            self.file_store.write_output_md(book.safe_name, output_filename, final_content)
            logger.info(f"[PostProcessor] 产出已写入: {output_filename}")
        else:
            output_filename = self._split_and_write(
                result, output_title, output_index, book, units_info, target_count
            )

        # 7. 更新状态
        for info in units_info:
            unit = book.get_unit_by_id(info["index"])
            if unit:
                unit.status = ProcessingStatus.POST_PROCESSED
        self.progress_manager.save_book(book)

        return {
            "success": True,
            "output_file": output_filename,
            "message": f"整合压缩完成（{len(contents)} → {target_count} 篇），产出 {len(result)} 字",
        }

    # ──────────────────────────────────────────────
    # 模式二：单篇优化
    # ──────────────────────────────────────────────

    def optimize_single(
        self,
        book: Book,
        unit_ids: list,
        instructions: str = "",
        temperature: float = 0.3,
        stream: bool = True,
    ) -> dict:
        """逐篇优化格式与结构，不合并内容。支持并发。"""
        if len(unit_ids) < 1:
            return {"success": False, "output_file": "", "message": "至少需要选择 1 个章节"}

        tasks = []
        skip_count = 0
        for uid in sorted(unit_ids):
            unit = book.get_unit_by_id(uid)
            if not unit:
                skip_count += 1
                continue
            filename = self._find_unit_file(book.safe_name, uid)
            if not filename:
                skip_count += 1
                continue
            tasks.append((uid, unit, filename))

        if not tasks:
            return {"success": False, "output_file": "", "message": "没有找到可优化的章节"}

        success_count = 0
        fail_count = skip_count
        output_files = []
        _count_lock = threading.Lock()

        def _optimize_one(uid, unit, filename):
            nonlocal success_count, fail_count
            try:
                md_content = self.file_store.read_outbox_md(book.safe_name, filename)
                md_content = self._strip_frontmatter(md_content)
            except FileNotFoundError:
                with _count_lock:
                    fail_count += 1
                return None

            if len(md_content) > MAX_SINGLE_CHARS:
                logger.warning(f"[PostProcessor] 章节 {uid} 内容过长({len(md_content)}字)，跳过")
                with _count_lock:
                    fail_count += 1
                return None

            logger.info(f"[PostProcessor] 单篇优化: [{uid}] {unit.title}")
            system_prompt = self._build_single_system_prompt(book.name)
            user_prompt = self._build_single_user_prompt(md_content, unit.title, instructions)
            max_tokens = min(16384, max(4096, len(md_content) // 2))

            try:
                result = self.llm_client.generate(
                    prompt=user_prompt,
                    system_message=system_prompt,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    stream=stream,
                    task_label=f"优化-{uid}",
                )
            except Exception as e:
                logger.error(f"[PostProcessor] 章节 {uid} LLM 调用失败: {e}")
                with _count_lock:
                    fail_count += 1
                return None

            if not result or len(result.strip()) < 100:
                with _count_lock:
                    fail_count += 1
                return None

            title_clean = re.sub(r'（\d+/\d+）$', '', unit.title).strip()
            final_content = self._build_output_md(
                result, title_clean, uid, book.name,
                [{"index": uid, "title": unit.title}], "optimized"
            )
            self.file_store.write_output_md(book.safe_name, filename, final_content)
            unit.status = ProcessingStatus.POST_PROCESSED

            with _count_lock:
                success_count += 1
                output_files.append(filename)
            logger.info(f"[PostProcessor] 已优化: {filename}")
            return filename

        # 并发执行
        if self.concurrency > 1 and len(tasks) > 1:
            logger.info(f"[PostProcessor] 并发单篇优化 {len(tasks)} 篇 (workers={self.concurrency})")
            with ThreadPoolExecutor(max_workers=self.concurrency, thread_name_prefix="pp") as executor:
                futures = {
                    executor.submit(_optimize_one, uid, u, fn): uid
                    for uid, u, fn in tasks
                }
                for future in as_completed(futures):
                    try:
                        future.result()
                    except Exception as e:
                        logger.error(f"[PostProcessor] 并发异常: {e}")
        else:
            for uid, u, fn in tasks:
                _optimize_one(uid, u, fn)

        self.progress_manager.save_book(book)

        msg = f"单篇优化完成：{success_count} 篇成功"
        if fail_count:
            msg += f"，{fail_count} 篇失败"

        return {
            "success": success_count > 0,
            "output_file": ", ".join(output_files[:3]) + ("..." if len(output_files) > 3 else ""),
            "message": msg,
        }

    # ──────────────────────────────────────────────
    # 内部工具方法
    # ──────────────────────────────────────────────

    @staticmethod
    def _estimate_target_count(total_chars: int) -> int:
        """根据总字数自动预判目标篇数（每篇目标 8000~12000 字）。"""
        ideal = total_chars / 10000
        count = max(3, min(7, round(ideal)))
        return count

    @staticmethod
    def _calc_max_tokens(total_chars: int, target_count: int) -> int:
        """根据内容量和目标篇数动态计算 max_tokens。"""
        target_chars = target_count * 10000
        estimated_tokens = int(target_chars * 1.5)
        return max(8192, min(32768, estimated_tokens))

    def _split_and_write(
        self, llm_output: str, base_title: str, base_index: str,
        book: Book, units_info: list, target_count: int
    ) -> str:
        """将多篇输出按分隔符拆分并写入多个文件。"""
        parts = re.split(r'={3,}\s*PART\s*\d+\s*={3,}', llm_output)
        parts = [p.strip() for p in parts if p.strip()]

        if len(parts) < 2:
            parts = re.split(r'\n(?=# )', llm_output.strip())
            parts = [p.strip() for p in parts if p.strip()]

        if len(parts) < 1:
            parts = [llm_output]

        output_files = []
        for i, part_content in enumerate(parts, 1):
            part_index = f"{base_index}-P{i}"
            part_title = f"{base_title}（{i}/{len(parts)}）"

            title_match = re.match(r'^#\s+(.+)', part_content)
            if title_match:
                part_title = title_match.group(1).strip()

            final_content = self._build_output_md(
                part_content, part_title, part_index, book.name, units_info, "merged"
            )
            output_filename = self.file_store.sanitize_filename(part_index, f"（整合）{part_title}")
            self.file_store.write_output_md(book.safe_name, output_filename, final_content)
            output_files.append(output_filename)
            logger.info(f"[PostProcessor] 写入分篇 {i}/{len(parts)}: {output_filename}")

        return ", ".join(output_files[:3]) + ("..." if len(output_files) > 3 else "")

    def _aggregate_content(self, book: Book, unit_ids: list) -> tuple:
        """聚合选中处理单元的 outbox MD 内容。"""
        contents = []
        units_info = []

        for uid in sorted(unit_ids):
            unit = book.get_unit_by_id(uid)
            if not unit:
                logger.warning(f"[PostProcessor] 处理单元 {uid} 未找到，跳过")
                continue

            filename = self._find_unit_file(book.safe_name, uid)
            if not filename:
                logger.warning(f"[PostProcessor] 处理单元 {uid} 无 outbox 文件，跳过")
                continue

            try:
                md_content = self.file_store.read_outbox_md(book.safe_name, filename)
                md_content = self._strip_frontmatter(md_content)
                contents.append(md_content)
                units_info.append({
                    "index": uid,
                    "title": unit.title,
                    "filename": filename,
                })
            except FileNotFoundError:
                logger.warning(f"[PostProcessor] 读取失败: {filename}")

        return contents, units_info

    def _find_unit_file(self, safe_name: str, unit_id: str) -> Optional[str]:
        """在 outbox 中查找某处理单元的 MD 文件"""
        files = self.file_store.list_outbox_files(safe_name)
        prefix = f"{unit_id}_"
        for f in files:
            if f.startswith(prefix):
                return f
        return None

    @staticmethod
    def _strip_frontmatter(md_content: str) -> str:
        """去除 Markdown 的 YAML frontmatter"""
        if md_content.startswith("---"):
            end = md_content.find("---", 3)
            if end != -1:
                return md_content[end + 3:].strip()
        return md_content

    # ──────────────────────────────────────────────
    # Prompt 构建
    # ──────────────────────────────────────────────

    @staticmethod
    def _build_merge_system_prompt(book_name: str, target_count: int) -> str:
        short_name = book_name.split('(')[0].strip()
        if target_count == 1:
            count_instruction = "合并为 **1 篇** 完整的综合笔记"
        else:
            count_instruction = f"整合为 **{target_count} 篇** 结构清晰的笔记，用 `=== PART N ===` 作为分篇分隔符"

        return f"""你是一位「知识架构师 (Knowledge Architect)」，正在对《{short_name}》的多篇笔记进行整合压缩。

你的任务是将多个独立笔记片段{count_instruction}。

整合原则：
1. **内容去重**：相同知识点只保留最完整版本
2. **逻辑重组**：按主题/论点重新组织，确保连贯性和层次感
3. **细节保留**：必须保留具体的方药、穴位、临床案例、诊断细节等实操内容，不可过度抽象
4. **格式统一**：Markdown 格式，标题层级清晰，善用表格和列表
5. **术语一致**：全文专业术语统一
6. **篇幅充实**：每篇目标 8000-12000 字，宁可长一些也不要丢失重要细节

输出格式：
- 每篇以 `# 篇名` 开头
- 包含清晰的二级/三级标题
- 核心概念 **加粗**
- 每篇末尾添加 `## 核心要点速览`
{"- 多篇之间用 `=== PART N ===` 分隔（N 从 1 开始）" if target_count > 1 else ""}"""

    @staticmethod
    def _build_merge_user_prompt(
        contents: list,
        units_info: list,
        output_title: str,
        instructions: str,
        target_count: int,
    ) -> str:
        parts = []
        parts.append(f"请将以下 {len(contents)} 篇笔记整合压缩为 {target_count} 篇完整笔记。")
        parts.append(f"主题：{output_title}")
        parts.append(f"要求：保留所有具体的方药、穴位、临床案例和诊断细节，不要过度抽象化。\n")

        if instructions:
            parts.append(f"额外要求：{instructions}\n")

        parts.append("---\n")

        for i, (content, info) in enumerate(zip(contents, units_info), 1):
            parts.append(f"### 笔记 {i}：{info['title']}")
            parts.append(content)
            parts.append("\n---\n")

        return "\n".join(parts)

    @staticmethod
    def _build_single_system_prompt(book_name: str) -> str:
        short_name = book_name.split('(')[0].strip()
        return f"""你是一位「知识架构师 (Knowledge Architect)」，正在对《{short_name}》的一篇笔记进行格式优化。

你的任务是优化这篇笔记的格式和结构，使其更加清晰易读，但**不删减任何实质内容**。

优化原则：
1. **不删减内容**：所有知识点、案例、方药、细节必须完整保留
2. **结构优化**：重新组织标题层级，使逻辑更清晰
3. **格式规范**：统一 Markdown 格式，善用表格、列表、加粗
4. **去除冗余**：仅去除重复的套话、过渡语等无信息量内容
5. **术语统一**：确保专业术语前后一致

输出：
- 以 `# 篇名` 开头
- 清晰的二级/三级标题结构
- 核心概念 **加粗**
- 末尾添加 `## 核心要点速览`"""

    @staticmethod
    def _build_single_user_prompt(content: str, title: str, instructions: str) -> str:
        parts = []
        parts.append(f"请优化以下笔记的格式和结构（不删减内容）。")
        parts.append(f"篇名：{title}\n")

        if instructions:
            parts.append(f"额外要求：{instructions}\n")

        parts.append("---\n")
        parts.append(content)

        return "\n".join(parts)

    @staticmethod
    def _build_output_md(
        llm_output: str,
        title: str,
        index: str,
        book_name: str,
        units_info: list,
        output_type: str = "merged",
    ) -> str:
        """构建最终的 Markdown 文件（含 frontmatter）"""
        source_indices = ", ".join(info["index"] for info in units_info)
        source_titles = ", ".join(info["title"] for info in units_info)
        today = date.today().isoformat()

        type_label = "整合" if output_type == "merged" else "优化"

        frontmatter = f"""---
chapter: "{index}"
title: "（{type_label}）{title}"
book: "{book_name.split('(')[0].strip()}"
source_chapters: "{source_indices}"
source_titles: "{source_titles}"
date: "{today}"
status: "post_processed"
type: "{output_type}"
---

"""
        return frontmatter + llm_output
