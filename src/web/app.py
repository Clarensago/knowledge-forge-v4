"""
Flask Web 应用 — 路由层

从 v3 web.py 拆出纯路由定义，API 接口保持向后兼容。
HTML 模板抽离为独立文件 templates/index.html。
"""

import logging
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

from ..core.enums import ProcessingStatus
from ..infra.file_store import FileStore
from ..postprocess.post_processor import PostProcessor
from .engine import TaskEngine

logger = logging.getLogger("knowledge-forge.web.app")


def create_app(project_root: str = None) -> Flask:
    """创建 Flask 应用"""
    if project_root is None:
        project_root = str(Path(__file__).parent.parent.parent)

    template_dir = str(Path(__file__).parent / "templates")
    app = Flask(__name__, template_folder=template_dir)

    engine = TaskEngine(project_root)
    engine.init_services()

    # 挂载到 app 上供外部访问
    app.engine = engine

    # ── 页面 ──

    @app.route("/")
    def index():
        html_path = Path(__file__).parent / "templates" / "index.html"
        return html_path.read_text(encoding="utf-8")

    # ── API ──

    @app.route("/api/inbox")
    def api_inbox():
        """获取 inbox 文件列表"""
        files = engine.file_store.scan_inbox()
        result = []
        for f in files:
            safe_name = FileStore.sanitize_dirname(f.name)
            book = engine.progress.get_book(safe_name)
            status = "pending"
            units_total = 0
            units_done = 0
            if book:
                status = book.status.value
                units_total = len(book.units)
                units_done = sum(
                    1 for u in book.units
                    if u.status in (
                        ProcessingStatus.STAGE2_DONE,
                        ProcessingStatus.STAGE2_5_DONE,
                        ProcessingStatus.STAGE3_DONE,
                        ProcessingStatus.POST_PROCESSED,
                    )
                )
            result.append({
                "name": f.name,
                "size": f"{f.stat().st_size / 1024 / 1024:.1f} MB",
                "status": status,
                "chapters_total": units_total,
                "chapters_done": units_done,
            })
        return jsonify(result)

    @app.route("/api/status")
    def api_status():
        """获取当前任务状态"""
        return jsonify(engine.get_status())

    @app.route("/api/logs")
    def api_logs():
        """获取日志（增量）"""
        since = request.args.get("since", 0, type=int)
        with engine._lock:
            lines = engine.log_lines[since:]
        return jsonify({"lines": lines, "total": len(engine.log_lines)})

    @app.route("/api/start", methods=["POST"])
    def api_start():
        """启动处理"""
        data = request.get_json() or {}
        queue = data.get("queue", [])
        if not queue:
            files = engine.file_store.scan_inbox()
            queue = [f.name for f in files]
        if not queue:
            return jsonify({"ok": False, "msg": "inbox 为空"})
        try:
            ok = engine.start(queue)
        except RuntimeError as e:
            return jsonify({"ok": False, "msg": str(e)})
        return jsonify({"ok": ok, "msg": "已启动" if ok else "任务正在运行中"})

    @app.route("/api/pause", methods=["POST"])
    def api_pause():
        engine.pause()
        return jsonify({"ok": True})

    @app.route("/api/resume", methods=["POST"])
    def api_resume():
        engine.resume()
        return jsonify({"ok": True})

    @app.route("/api/abort", methods=["POST"])
    def api_abort():
        """放弃当前书籍"""
        engine.abort_current()
        return jsonify({"ok": True})

    @app.route("/api/stop", methods=["POST"])
    def api_stop():
        """停止全部"""
        engine.stop_all()
        return jsonify({"ok": True})

    @app.route("/api/concurrency", methods=["POST"])
    def api_concurrency():
        """调整并发数"""
        data = request.get_json() or {}
        n = data.get("value", 3)
        engine.set_concurrency(int(n))
        return jsonify({"ok": True, "concurrency": engine._concurrent_workers})

    @app.route("/api/reset/<path:book_name>", methods=["POST"])
    def api_reset(book_name):
        """重置某本书"""
        safe_name = FileStore.sanitize_dirname(book_name)
        book = engine.progress.get_book(safe_name)
        if book:
            engine.file_store.archive_book(safe_name)
            engine.progress.remove_book(safe_name)
            return jsonify({"ok": True, "msg": f"已重置并存档: {book_name}"})
        return jsonify({"ok": False, "msg": "未找到该书"})

    @app.route("/api/books")
    def api_books():
        """获取所有已处理书籍"""
        result = []
        for safe_name in engine.progress.list_books():
            book = engine.progress.get_book(safe_name)
            if book:
                total = len(book.units)
                done = sum(
                    1 for u in book.units
                    if u.status in (
                        ProcessingStatus.STAGE2_DONE,
                        ProcessingStatus.STAGE2_5_DONE,
                        ProcessingStatus.STAGE3_DONE,
                        ProcessingStatus.POST_PROCESSED,
                    )
                )
                outbox_files = engine.file_store.list_outbox_files(safe_name)
                result.append({
                    "name": book.name,
                    "safe_name": safe_name,
                    "status": book.status.value,
                    "total": total,
                    "done": done,
                    "outbox_count": len(outbox_files),
                    "updated_at": book.updated_at,
                })
        return jsonify(result)

    @app.route("/api/chapters/<path:book_name>")
    def api_chapters(book_name):
        """获取某本书的处理单元列表"""
        safe_name = FileStore.sanitize_dirname(book_name)
        book = engine.progress.get_book(safe_name)
        if not book:
            return jsonify({"ok": False, "msg": "未找到该书", "chapters": []})
        chapters = []
        for u in book.units:
            if u.status in (
                ProcessingStatus.STAGE2_DONE,
                ProcessingStatus.STAGE2_5_DONE,
                ProcessingStatus.STAGE3_DONE,
                ProcessingStatus.POST_PROCESSED,
            ):
                has_output = bool(u.final_output)
                if not has_output:
                    try:
                        outbox_files = engine.file_store.list_outbox_files(safe_name)
                        has_output = any(f.startswith(u.id) for f in outbox_files)
                    except Exception:
                        pass
                chapters.append({
                    "index": u.id,
                    "title": u.title,
                    "status": u.status.value,
                    "char_count": len(u.final_output) if u.final_output else 0,
                    "has_output": has_output,
                })
        return jsonify({"ok": True, "book_name": book.name, "chapters": chapters})

    # ── 后处理器 API ──

    @app.route("/api/post-process", methods=["POST"])
    def api_post_process():
        """执行后处理（整合压缩 / 单篇优化）"""
        data = request.get_json() or {}
        book_name = data.get("book_name", "")
        indices = data.get("indices", [])
        output_title = data.get("output_title", "")
        instructions = data.get("instructions", "")
        mode = data.get("mode", "merge")  # "merge" | "single"
        target_count = data.get("target_count")  # int or None

        if not book_name or not indices:
            return jsonify({"ok": False, "msg": "缺少参数：book_name 和 indices"})

        safe_name = FileStore.sanitize_dirname(book_name)
        book = engine.progress.get_book(safe_name)
        if not book:
            return jsonify({"ok": False, "msg": "未找到该书"})

        try:
            engine._ensure_llm()
        except RuntimeError as e:
            return jsonify({"ok": False, "msg": str(e)})

        pp = PostProcessor(
            engine.llm, engine.file_store, engine.progress,
            concurrency=engine._concurrent_workers,
        )

        if mode == "single":
            result = pp.optimize_single(
                book=book,
                unit_ids=indices,
                instructions=instructions,
            )
        else:
            result = pp.merge_chapters(
                book=book,
                unit_ids=indices,
                output_title=output_title or None,
                instructions=instructions,
                target_count=target_count,
            )

        return jsonify({
            "ok": result["success"],
            "msg": result["message"],
            "output_file": result.get("output_file", ""),
        })

    @app.route("/api/config", methods=["GET"])
    def api_config_get():
        """获取配置"""
        key = engine.config.load_api_key()
        has_key = bool(key)
        masked = ""
        if key:
            masked = key[:6] + "****" + key[-4:] if len(key) > 10 else "****"
        return jsonify({
            "has_api_key": has_key,
            "api_key_masked": masked,
            "model": engine.config.llm_model,
            "api_base_url": engine.config.llm_base_url,
            "concurrency": engine._concurrent_workers,
        })

    @app.route("/api/config", methods=["POST"])
    def api_config_post():
        """保存 API Key"""
        data = request.get_json() or {}
        api_key = data.get("api_key", "").strip()
        if not api_key:
            return jsonify({"ok": False, "msg": "API Key 不能为空"})
        if not api_key.startswith("sk-") or len(api_key) < 10:
            return jsonify({"ok": False, "msg": "无效的 API Key 格式"})
        engine.config.save_api_key(api_key)
        engine.llm = None
        return jsonify({"ok": True, "msg": "API Key 已保存"})

    return app


def run_web(host: str = "0.0.0.0", port: int = 5000, project_root: str = None):
    """启动 Web 服务"""
    app = create_app(project_root)
    logger.info(f"Web UI 启动: http://{host}:{port}")
    app.run(host=host, port=port, debug=False)
