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

(sleep 3; open "http://localhost:3000/") &
npm run dev
