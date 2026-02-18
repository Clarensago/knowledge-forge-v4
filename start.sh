#!/bin/bash
# ============================================================
# 知识加工厂 v4 — 一键启动
#
# 用法：
#   ./start.sh        → 启动 Web UI（推荐）
#   ./start.sh cli    → 命令行模式直接处理
# ============================================================

cd "$(dirname "$0")"

# 杀掉已有的旧进程
OLD_PID=$(lsof -ti:8080 2>/dev/null)
if [ -n "$OLD_PID" ]; then
    echo "🔄 关闭旧服务 (PID: $OLD_PID)..."
    kill $OLD_PID 2>/dev/null
    sleep 0.5
fi

# 检查 Python
if command -v python3 &>/dev/null; then
    PY=python3
elif command -v python &>/dev/null; then
    PY=python
else
    echo "❌ 未找到 Python，请先安装 Python 3.11+"
    exit 1
fi

# 检查依赖
if ! $PY -c "import flask" &>/dev/null || ! $PY -c "import yaml" &>/dev/null; then
    echo "📦 安装依赖..."
    $PY -m pip install -r requirements.txt -q
    echo ""
fi

if [ "$1" = "cli" ]; then
    # CLI 模式
    FILE_COUNT=$(find inbox -type f \( -name "*.pdf" -o -name "*.epub" -o -name "*.txt" \) 2>/dev/null | wc -l | tr -d ' ')
    if [ "$FILE_COUNT" = "0" ]; then
        echo "📂 inbox/ 目录为空，请先放入书籍文件"
        exit 0
    fi
    echo "📚 发现 ${FILE_COUNT} 本书，开始处理..."
    $PY -m src.main run
else
    # Web UI 模式（默认）
    echo "🚀 启动 Web UI: http://localhost:8080"
    (sleep 1 && open "http://localhost:8080") &
    $PY -m src.main web
fi
