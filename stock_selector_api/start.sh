#!/usr/bin/env bash
# ============================================================
# 开发/测试用启动脚本（本地调试，无需 systemd）
# 用法: ./start.sh            # 前台启动
#       ./start.sh --dev      # 热重载启动
# ============================================================
set -euo pipefail
cd "$(dirname "$0")/backend"

BIN=""
if [ -x ../.venv/bin/uvicorn ]; then
  BIN=../.venv/bin/uvicorn
elif command -v uvicorn >/dev/null 2>&1; then
  BIN=uvicorn
else
  echo "未找到 uvicorn，请先创建虚拟环境并安装依赖：" >&2
  echo "  python3 -m venv ../.venv && ../.venv/bin/pip install -e '..[dev]'" >&2
  exit 1
fi

if [ "${1:-}" = "--dev" ]; then
  exec "$BIN" main:app --host 0.0.0.0 --port 8000 --reload
else
  exec "$BIN" main:app --host 0.0.0.0 --port 8000
fi
