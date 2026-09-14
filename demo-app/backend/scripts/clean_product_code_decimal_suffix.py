"""一次性迁移：清洗 history_quotes 中 xls 数值格遗留的 ".0" 产品编码后缀。

xls 数值格把编码读成浮点数（27001.0），入库后 normalize_text 变 270010，
exact-code 匹配对这些行整体失效。本脚本把"纯整数+.0"形态的 product_code
还原为整数码，并同步更新 normalized_product_code（= normalize_text(新编码)）。

用法（在 backend/ 目录下）：
    ./.venv/bin/python scripts/clean_product_code_decimal_suffix.py [db_path]
默认 db_path = data/quote_saitel.db。执行前自动用 SQLite backup API 备份到
<db>.bak-YYYYMMDD（已存在则追加 -HHMMSS）。
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

from app.matching import normalize_text  # noqa: E402


def needs_clean(code: str) -> bool:
    return bool(code) and code.endswith(".0") and code[:-2].isdigit()


def backup(db_path: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d")
    backup_path = db_path.with_name(f"{db_path.name}.bak-{stamp}")
    if backup_path.exists():
        backup_path = db_path.with_name(f"{db_path.name}.bak-{datetime.now().strftime('%Y%m%d-%H%M%S')}")
    source = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    target = sqlite3.connect(backup_path)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()
    return backup_path


def main() -> None:
    db_path = Path(sys.argv[1]) if len(sys.argv) > 1 else BACKEND_DIR / "data" / "quote_saitel.db"
    if not db_path.exists():
        raise SystemExit(f"数据库不存在: {db_path}")

    backup_path = backup(db_path)
    print(f"已备份: {backup_path}")

    connection = sqlite3.connect(db_path)
    try:
        rows = connection.execute("SELECT id, product_code FROM history_quotes").fetchall()
        targets = [(row_id, code) for row_id, code in rows if needs_clean(str(code or ""))]
        with connection:
            for row_id, code in targets:
                cleaned = code[:-2]
                connection.execute(
                    "UPDATE history_quotes SET product_code = ?, normalized_product_code = ? WHERE id = ?",
                    (cleaned, normalize_text(cleaned), row_id),
                )
        remaining = connection.execute(
            "SELECT COUNT(*) FROM history_quotes WHERE product_code LIKE '%.0'"
        ).fetchone()[0]
        print(f"清洗完成: {len(targets)} 行 product_code 去掉了 .0 后缀；残留 .0 后缀行数: {remaining}")
    finally:
        connection.close()


if __name__ == "__main__":
    main()
