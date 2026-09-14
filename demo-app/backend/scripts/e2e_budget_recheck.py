"""端到端验证（一次性）：用《预算清单 - 副本(1).xlsx》在临时库全量建任务，
逐条核对错误清单 10 个案例的默认价，并与 9/11 旧导出（预算清单_报价_赛特尔25年.xlsx）
做全行回归对比。

用法（在 backend/ 目录下）：
    ./.venv/bin/python scripts/e2e_budget_recheck.py
不触碰生产库：先把 data/quote_saitel.db 用 SQLite backup API 复制到临时目录再跑。
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import time
import uuid
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
INQUIRY = Path("/Users/zcy/codes/历史采购报价查询/智能报价系统_完整代码包/生产数据/预算清单 - 副本(1).xlsx")
OLD_EXPORT = Path("/Users/zcy/codes/历史采购报价查询/智能报价系统_完整代码包/生产数据/预算清单_报价_赛特尔25年.xlsx")
TARGET_ROWS = [301, 305, 400, 401, 402, 444, 445, 446, 490, 597, 671, 672]

work = Path(tempfile.mkdtemp(prefix="e2e-budget-"))
db_copy = work / "e2e.db"
# WAL 模式下用 backup API 复制（直接 cp 可能丢 wal 内容）
src = sqlite3.connect(f"file:{BACKEND / 'data' / 'quote_saitel.db'}?mode=ro", uri=True)
dst = sqlite3.connect(db_copy)
src.backup(dst)
dst.close()
src.close()

os.environ["DATABASE_URL"] = f"sqlite:///{db_copy}"
os.environ["QUOTE_DATA_DIR"] = str(work / "data")
sys.path.insert(0, str(BACKEND))

from openpyxl import load_workbook  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.database import SessionLocal, init_db  # noqa: E402
from app.models import QuoteJob, QuoteLine, User  # noqa: E402
from app.services import process_job  # noqa: E402


def old_prices() -> dict[int, tuple]:
    """旧导出: row -> (价格, 校核, 来源sheet)"""
    wb = load_workbook(OLD_EXPORT, data_only=True)
    ws = wb["Sheet1"]
    result = {}
    for row in range(3, ws.max_row + 1):
        result[row] = (ws.cell(row, 6).value, ws.cell(row, 10).value, ws.cell(row, 8).value)
    wb.close()
    return result


def main() -> None:
    init_db()
    db = SessionLocal()
    try:
        admin = db.scalar(select(User).where(User.username == "admin"))
        job_id = str(uuid.uuid4())
        db.add(
            QuoteJob(
                id=job_id,
                customer_id=None,
                created_by_id=admin.id,
                file_name=INQUIRY.name,
                source_file_path=str(INQUIRY),
                status="queued",
                requested_option_count=3,
                tax_rate=0.10,
            )
        )
        db.commit()
        started = time.time()
        process_job(job_id)
        elapsed = time.time() - started

        lines = db.scalars(select(QuoteLine).where(QuoteLine.job_id == job_id)).all()
        by_row = {line.source_row: line for line in lines}
        old = old_prices()

        print(f"任务完成: {len(lines)} 行, 耗时 {elapsed:.1f}s, 临时库 {db_copy}")
        print("\n== 错误清单 10 案例（前 -> 后）==")
        for row in TARGET_ROWS:
            line = by_row.get(row)
            old_price, old_check, old_sheet = old.get(row, (None, None, None))
            if line is None:
                print(f"行{row}: 未解析到询价行")
                continue
            selected = sorted((o for o in line.options if o.selected), key=lambda o: o.rank)
            if selected:
                primary = selected[0]
                record = primary.history_quote
                rec_desc = (
                    f"{record.name}|{record.spec[:20]}|{record.source_sheet}|码{record.product_code}"
                    if record
                    else "估算/手工"
                )
                price = primary.final_price
            else:
                rec_desc, price = "无默认选中", None
            warnings = "；".join(line.warnings or [])[:60]
            print(
                f"行{row} {line.name}: 前={old_price}({old_check}) 后={price}  [{rec_desc}]\n"
                f"    警告: {warnings}"
            )

        print("\n== 全行回归对比（旧导出 赛特尔报价 列 vs 新默认价）==")
        same = changed = new_priced = lost = 0
        flagged = []
        for row, line in sorted(by_row.items()):
            old_price, old_check, _old_sheet = old.get(row, (None, None, None))
            selected = sorted((o for o in line.options if o.selected), key=lambda o: o.rank)
            new_price = selected[0].final_price if selected else None
            if old_price is None and new_price is None:
                continue
            if old_price is None and new_price is not None:
                new_priced += 1
                continue
            if old_price is not None and new_price is None:
                lost += 1
                flagged.append((row, line.name, old_price, new_price, "旧有价新无价"))
                continue
            if abs(float(old_price) - float(new_price)) < 0.005:
                same += 1
            else:
                changed += 1
                ratio = float(new_price) / max(float(old_price), 1e-9)
                if old_check == "OK" or ratio > 3 or ratio < 1 / 3:
                    flagged.append((row, line.name, old_price, new_price, f"校核={old_check} 比值={ratio:.2f}"))
        print(f"同价: {same}, 改价: {changed}, 旧无价新有价: {new_priced}, 旧有价新无价: {lost}")
        print(f"需人工关注的变动（旧校核OK 或 价差>3倍）: {len(flagged)} 行")
        for row, name, old_price, new_price, reason in flagged:
            print(f"  行{row} {name}: {old_price} -> {new_price} ({reason})")
    finally:
        db.close()


if __name__ == "__main__":
    main()
