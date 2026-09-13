#!/bin/zsh
set -e

SCRIPT_DIR="${0:A:h}"
cd "$SCRIPT_DIR"

if ! command -v node >/dev/null 2>&1; then
  echo "未找到 Node.js，请先安装 Node.js 22 或更高版本。"
  read -k 1 "?按任意键退出..."
  exit 1
fi

if [ ! -d node_modules ]; then
  npm ci --ignore-scripts --prefer-offline --no-audit --no-fund
fi

# 启动后端 API（报价任务/客户档案/数据库管理/账号管理都依赖它，否则前端会报"无法连接服务器"）
if [ -x backend/.venv/bin/uvicorn ]; then
  (cd backend && DATABASE_URL=sqlite:///./data/quote_saitel.db exec ./.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000) &
  API_PID=$!
  trap 'kill "$API_PID" 2>/dev/null || true' EXIT INT TERM
  echo "后端 API 已启动：http://127.0.0.1:8000（数据库 quote_saitel）"
else
  echo "警告：未找到 backend/.venv，后端未启动。请先在 demo-app/backend 下执行："
  echo "  python -m venv .venv && ./.venv/bin/pip install -r requirements-dev.txt"
fi

(sleep 3; open "http://localhost:3000/") &
npm run dev
