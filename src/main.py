#!/usr/bin/env python3
"""
知识加工厂 v4 — CLI 入口

用法：
    python -m src.main run                    # 处理 inbox 中所有文件
    python -m src.main run --book "书名"      # 只处理指定书籍
    python -m src.main run --model deepseek-reasoner  # 指定模型
    python -m src.main run --no-dual-round    # 禁用 Round 2 自检精修
    python -m src.main run --no-stream        # 禁用流式输出
    python -m src.main status                 # 查看当前状态
    python -m src.main reset "书名"           # 重置某本书进度
    python -m src.main web                    # 启动 Web UI
"""

import sys
import argparse
import logging
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent

logger = logging.getLogger("knowledge-forge.main")


def cmd_run(args, config, file_store, progress):
    """运行 CLI 处理管线"""
    from .llm.client import LLMClient
    from .pipeline.engine import PipelineEngine
    from .pipeline.stages.preprocess import PreprocessStage
    from .pipeline.stages.mind_builder import MindBuilderStage
    from .pipeline.stages.modeler import ModelerStage
    from .pipeline.stages.assembler import AssemblerStage
    from .pipeline.stages.retrospector import RetrospectorStage

    # API Key
    api_key = config.load_api_key()
    if not api_key:
        print("未找到 API Key。请在 config/config.json 中设置，或创建 .api_key 文件。")
        sys.exit(1)

    # LLM 客户端
    llm = LLMClient(
        api_key=api_key,
        base_url=config.llm_base_url,
        model=config.llm_model,
        timeout=config.llm_timeout,
    )

    # 健康检查
    print("\n检查 API 连通性...")
    if not llm.check_health():
        print("API 连接失败，请检查网络和 API Key。")
        sys.exit(1)
    print("API 连接正常\n")

    # 组装管线
    engine = PipelineEngine(config, llm, file_store, progress)
    engine.register_stage("preprocess", PreprocessStage(config, file_store))
    engine.register_stage("mind_builder", MindBuilderStage(config, llm, file_store, progress))
    engine.register_stage("modeler", ModelerStage(config, llm, file_store, progress))
    engine.register_stage("assembler", AssemblerStage(config, file_store))
    engine.register_stage("retrospector", RetrospectorStage(config, llm, file_store))

    # 扫描 inbox
    inbox = Path(config.get("paths.inbox", "inbox"))
    if not inbox.is_absolute():
        inbox = PROJECT_ROOT / inbox

    files = sorted(
        p for p in inbox.iterdir()
        if p.suffix.lower() in (".epub", ".pdf", ".txt") and not p.name.startswith(".")
    ) if inbox.exists() else []

    if args.book:
        files = [f for f in files if args.book.lower() in f.name.lower()]

    if not files:
        print("未找到待处理的书籍文件。")
        if args.book:
            print(f"  搜索关键词: {args.book}")
        print(f"  扫描目录: {inbox}")
        sys.exit(0)

    print(f"发现 {len(files)} 本书，开始处理...\n")

    for i, source in enumerate(files, 1):
        print(f"[{i}/{len(files)}] {source.name}")
        try:
            success = engine.run(str(source))
            if not success:
                print(f"  处理未完成: {source.name}\n")
        except KeyboardInterrupt:
            print("\n\n用户中断，进度已保存。")
            sys.exit(0)
        except Exception as e:
            logger.error(f"处理失败: {source.name} — {e}", exc_info=True)
            print(f"  处理失败: {e}\n")

    print("\n全部处理完成。")


def cmd_status(progress):
    """查看处理状态"""
    print("\n知识加工厂 v4 — 处理状态")
    print("=" * 50)
    books = progress.list_books()
    if not books:
        print("  （暂无已处理的书籍）")
        return

    for safe_name in books:
        book = progress.get_book(safe_name)
        if book:
            summary = progress.get_book_status_summary(book)
            print(f"\n  {book.name}")
            print(f"    状态: {book.status.value}")
            if summary.get("total", 0) > 0:
                print(f"    处理单元: {summary['total']} 个")
                for status, count in summary.get("by_status", {}).items():
                    if count > 0:
                        print(f"      {status}: {count}")


def cmd_reset(args, progress):
    """重置书籍进度"""
    target = args.book_name
    books = progress.list_books()
    matched = [b for b in books if target.lower() in b.lower()]

    if not matched:
        print(f"未找到匹配的书籍: {target}")
        return

    for safe_name in matched:
        book = progress.get_book(safe_name)
        if book:
            progress.reset_book(book)
            print(f"已重置: {book.name}")


def cmd_web(args):
    """启动 Web UI"""
    from .web.app import run_web
    host = getattr(args, "host", "0.0.0.0")
    port = getattr(args, "port", 8080)
    run_web(host=host, port=port, project_root=str(PROJECT_ROOT))


def main():
    parser = argparse.ArgumentParser(
        description="知识加工厂 v4 — 全自动书籍深度结构化知识加工系统",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    subparsers = parser.add_subparsers(dest="command", help="可用命令")

    # run 命令
    run_parser = subparsers.add_parser("run", help="运行处理管线")
    run_parser.add_argument("--book", type=str, help="指定处理某本书（部分匹配）")
    run_parser.add_argument("--model", type=str, help="指定 LLM 模型")
    run_parser.add_argument("--no-dual-round", action="store_true", help="禁用 Round 2 自检精修")
    run_parser.add_argument("--no-stream", action="store_true", help="禁用流式输出")

    # status 命令
    subparsers.add_parser("status", help="查看当前处理状态")

    # reset 命令
    reset_parser = subparsers.add_parser("reset", help="重置某本书的进度")
    reset_parser.add_argument("book_name", type=str, help="书名或文件名（部分匹配）")

    # web 命令
    web_parser = subparsers.add_parser("web", help="启动 Web UI")
    web_parser.add_argument("--host", type=str, default="0.0.0.0", help="绑定地址")
    web_parser.add_argument("--port", type=int, default=8080, help="端口号")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(0)

    # 初始化基础设施
    from .infra.logger import setup_logging
    from .infra.config import Config
    from .infra.file_store import FileStore
    from .infra.progress import ProgressManager

    setup_logging(str(PROJECT_ROOT))
    config = Config(str(PROJECT_ROOT))

    # CLI 参数覆盖
    if args.command == "run":
        overrides = {}
        if args.model:
            overrides["llm.model"] = args.model
        if args.no_dual_round:
            overrides["processing.dual_round"] = False
        if args.no_stream:
            overrides["processing.stream"] = False
        if overrides:
            config.apply_cli_overrides(**overrides)

    file_store = FileStore(str(PROJECT_ROOT))
    progress = ProgressManager(str(PROJECT_ROOT))

    # 分发命令
    if args.command == "run":
        cmd_run(args, config, file_store, progress)
    elif args.command == "status":
        cmd_status(progress)
    elif args.command == "reset":
        cmd_reset(args, progress)
    elif args.command == "web":
        cmd_web(args)


if __name__ == "__main__":
    main()
