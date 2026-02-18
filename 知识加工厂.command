#!/bin/bash
# ============================================================
# 知识加工厂 v4 — 双击启动
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
    echo "按回车键退出..."
    read
    exit 1
fi

# 检查依赖
if ! $PY -c "import flask" &>/dev/null || ! $PY -c "import yaml" &>/dev/null; then
    echo "📦 首次运行，安装依赖中..."
    $PY -m pip install -r requirements.txt -q
    echo ""
fi

# 自动打开浏览器（延迟1秒等服务启动）
(sleep 1 && open "http://localhost:8080") &

# 启动 Web UI
echo "🚀 启动知识加工厂..."
echo ""
$PY -m src.main web
