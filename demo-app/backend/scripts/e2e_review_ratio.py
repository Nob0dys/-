"""端到端验证（一次性）：复核比例控制。

用《20260911科学仪器.xlsx》在临时库（迁移后真实库的副本）全量跑 process_job：
1. 前后状态分布对比（"前"= 库中同文件旧任务的行状态）；
2. hard-manual 行清单（无候选/无默认选中/BLOCK/VIP毛利核对）；
3. 被降级进复核的代表行（分数最低若干）供人工抽查；
4. 不变量：同一文件分别以 10% 和 100% 目标各跑一个任务，
   两任务的逐行 selected 方案集合必须完全一致（状态重构不影响匹配/选中）。

用法（在 backend/ 目录下）：
    ./.venv/bin/python scripts/e2e_review_ratio.py
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
INQUIRY = Path("/Users/zcy/codes/历史采购报价查询/智能报价系统_完整代码包/生产数据/20260911科学仪器.xlsx")

work = Path(tempfile.mkdtemp(prefix="e2e-review-"))
db_copy = work / "e2e.db"
src = sqlite3.connect(f"file:{BACKEND / 'data' / 'quote_saitel.db'}?mode=ro", uri=True)
dst = sqlite3.connect(db_copy)
src.backup(dst)
dst.close()
src.close()

os.environ["DATABASE_URL"] = f"sqlite:///{db_copy}"
os.environ["QUOTE_DATA_DIR"] = str(work / "data")
sys.path.insert(0, str(BACKEND))

from sqlalchemy import select  # noqa: E402

from app.database import SessionLocal, init_db  # noqa: E402
from app.models import QuoteJob, QuoteLine, User  # noqa: E402
import app.services as services  # noqa: E402


def run_job(db, label: str) -> str:
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
    services.process_job(job_id)
    print(f"[{label}] 任务 {job_id} 完成，耗时 {time.time() - started:.1f}s")
    return job_id


def distribution(db, job_id: str) -> dict:
    rows = db.execute(
        select(QuoteLine.status, QuoteLine.confidence, QuoteLine.recommended_score)
        .where(QuoteLine.job_id == job_id)
    ).all()
    dist: dict[str, int] = {}
    for status, _confidence, _score in rows:
        dist[status] = dist.get(status, 0) + 1
    return dist, rows


def selected_map(db, job_id: str) -> dict:
    """(sheet,row) -> tuple(sorted (history_quote_id, final_price) for selected options)"""
    from app.models import QuoteOption

    lines = db.scalars(select(QuoteLine).where(QuoteLine.job_id == job_id)).all()
    result = {}
    for line in lines:
        selected = [
            (option.history_quote_id, float(option.final_price))
            for option in line.options
            if option.selected
        ]
        result[(line.sheet_name, line.source_row)] = tuple(sorted(selected))
    return result


def main() -> None:
    init_db()
    db = SessionLocal()
    try:
        # "前"：库中同文件的旧任务（旧代码+迁移前数据跑出的状态分布）
        old_job = db.scalar(
            select(QuoteJob)
            .where(QuoteJob.file_name == INQUIRY.name, QuoteJob.total_lines > 0)
            .order_by(QuoteJob.created_at.desc())
            .limit(1)
        )
        if old_job:
            old_dist, _ = distribution(db, old_job.id)
            print(f"修复前（旧任务 {old_job.id[:8]}…）: {old_dist}")

        job_id = run_job(db, "目标10%")
        new_dist, rows = distribution(db, job_id)
        job = db.get(QuoteJob, job_id)
        print(f"修复后（目标10%）: {new_dist} | job 计数: matched={job.matched_lines} review={job.review_lines} unmatched={job.unmatched_lines}")

        # hard / 降级行清单
        lines = db.scalars(select(QuoteLine).where(QuoteLine.job_id == job_id)).all()
        hard_rows, demoted_rows = [], []
        for line in lines:
            if line.status not in ("review", "unmatched"):
                continue
            warnings = [str(w) for w in (line.warnings or [])]
            has_block = any(w.startswith("BLOCK:") for w in warnings)
            no_price_option = not any(o.selected and o.history_quote_id for o in line.options)
            if line.status == "unmatched" or has_block:
                hard_rows.append(line)
            else:
                demoted_rows.append(line)
        print(f"\n== hard-manual 行（{len(hard_rows)} 行，其中无匹配 {sum(1 for l in hard_rows if l.status == 'unmatched')}）==")
        for line in sorted(hard_rows, key=lambda l: l.source_row)[:30]:
            print(f"  行{line.source_row} {line.name} [{line.status}] score={line.recommended_score:.0f} 警告: {'；'.join((line.warnings or [])[:2])[:80]}")
        demoted_rows.sort(key=lambda l: l.recommended_score)
        print(f"\n== 降级进复核的行（{len(demoted_rows)} 行，按分数升序列前 25）==")
        for line in demoted_rows[:25]:
            print(f"  行{line.source_row} {line.name} score={line.recommended_score:.1f} 警告: {'；'.join((line.warnings or [])[:2])[:80]}")

        # 不变量：100% 目标（全部软行降级）下 selected 方案集合必须不变
        services.REVIEW_TARGET_PERCENT = 100.0
        job_id_all = run_job(db, "目标100%")
        all_dist, _ = distribution(db, job_id_all)
        print(f"\n对照（目标100%）: {all_dist}")
        assert selected_map(db, job_id) == selected_map(db, job_id_all), "selected 方案集合在两种目标下不一致！"
        print("不变量通过：10% 与 100% 目标下逐行 selected 方案集合完全一致（状态重构不影响匹配/选中）")
    finally:
        db.close()


if __name__ == "__main__":
    main()
