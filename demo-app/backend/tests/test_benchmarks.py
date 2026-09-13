"""回归基准测试：在测试库上以真实 Excel 任务为目标跑一次 process_job，
断言关键指标不低于历史底线，避免修改 matching/services 时不经意把匹配率改坏。

底线数字来源：2026-08-21 优化前真实对比
    浙江三和  54.0% 真实正确（63 行）
    琼侨      30.3% 真实正确（188 行）
优化后预期：浙江三和 ≥58%，琼侨 ≥32% （底线保守取 0 下调）
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.database import SessionLocal, init_db
from app.models import HistoryQuote, QuoteJob, QuoteLine, QuoteOption
from app.services import process_job, seed_database
from sqlalchemy import select, func


_TESTDATA = Path(__file__).resolve().parents[3] / "测试数据"


def _load_history(path: Path) -> list[dict]:
    """导入一份历史价目表（普教+赛特尔）作为基准。"""
    from app.excel_service import parse_history_workbook
    return parse_history_workbook(str(path), source_name=path.name)


@pytest.fixture(scope="session")
def benchmark_db(tmp_path_factory):
    """独立的测试 DB：预置管理员 / 历史库 / 测试任务。"""
    tmp = tmp_path_factory.mktemp("benchmark")
    os.environ["DATABASE_URL"] = f"sqlite:///{tmp / 'bench.db'}"
    os.environ["QUOTE_DATA_DIR"] = str(tmp / "data")
    init_db()
    seed_database()
    db = SessionLocal()
    try:
        # seed_database 会用 history.json 初始化 HistoryQuote（占用了相同 id 空间），
        # 基准测试需要"以真实价目表为唯一历史"，因此先清空。
        from sqlalchemy import delete as sql_delete
        db.execute(sql_delete(HistoryQuote))
        db.commit()
        history_paths = [
            _TESTDATA / "普教清单.xlsx",
        ]
        # 加生产数据赛特尔25年（真实成交价）
        prod_saitel = Path(os.environ.get(
            "BENCH_SAITEL",
            str(_TESTDATA.parent / "生产数据" / "赛特尔25年.xls"),
        ))
        if prod_saitel.exists():
            history_paths.append(prod_saitel)

        records = []
        for path in history_paths:
            if not path.exists():
                continue
            for row in _load_history(path):
                if not row.get("price") or row["price"] <= 0:
                    continue
                records.append({
                    "id": f"{path.name}:{row['sheet_name']}:{row['source_row']}",
                    "source_file": path.name,
                    "source_sheet": row["sheet_name"],
                    "source_row": row["source_row"],
                    "name": row["name"],
                    "spec": row.get("spec", ""),
                    "product_code": row.get("product_code", ""),
                    "model": row.get("model", ""),
                    "brand": row.get("brand", ""),
                    "manufacturer": row.get("manufacturer", ""),
                    "unit": row.get("unit", ""),
                    "price": row["price"],
                    "quote_date": "",
                    "source_priority": 0,
                    "data_quality": 0.6,
                })
        for r in records:
            # 补齐 HistoryQuote 要求的 normalized_* 字段（fixture 手写时遗漏）
            from app.matching import normalize_text
            r.setdefault("normalized_name", normalize_text(r["name"]))
            r.setdefault("normalized_spec", normalize_text(r.get("spec", "")))
            r.setdefault("normalized_product_code", normalize_text(r.get("product_code", "")))
            r.setdefault("normalized_model", normalize_text(r.get("model", "")))
            r.setdefault("normalized_unit", normalize_text(r.get("unit", "")))
            r.setdefault("quantity", None)
            db.add(HistoryQuote(**r))
        db.commit()
        yield db
    finally:
        db.close()


def _run_job(db, source_file: Path, name: str) -> QuoteJob:
    from app.models import User
    user = db.scalar(select(User).where(User.username == "admin"))
    assert user is not None
    job = QuoteJob(
        id=f"bench-{name}",
        customer_id=None,
        created_by_id=user.id,
        file_name=source_file.name,
        source_file_path=str(source_file),
        status="queued",
        requested_option_count=3,
        tax_rate=0.10,
    )
    db.add(job)
    db.commit()
    process_job(job.id)
    db.refresh(job)
    return job


def _line_metrics(db, job: QuoteJob):
    lines = db.scalars(
        select(QuoteLine).where(QuoteLine.job_id == job.id)
    ).all()
    total = len(lines)
    stats = {"total": total, "with_selection": 0, "estimated": 0, "unmatched": 0}
    for line in lines:
        options = db.scalars(
            select(QuoteOption).where(QuoteOption.line_id == line.id)
        ).all()
        selected = [o for o in options if o.selected]
        if selected:
            stats["with_selection"] += 1
            if any("估算价" in str(w) for o in selected for w in (o.warnings or [])):
                stats["estimated"] += 1
        else:
            stats["unmatched"] += 1
    return stats


@pytest.mark.skipif(
    not (_TESTDATA / "浙江三和初中物理仪器0309 - 空白本.xlsx").exists(),
    reason="基准数据未随仓库发布，跳过（CI 会跳过，本地有数据时跑）",
)
def test_benchmark_zjsh_does_not_regress(benchmark_db):
    job = _run_job(
        benchmark_db,
        _TESTDATA / "浙江三和初中物理仪器0309 - 空白本.xlsx",
        "zjsh",
    )
    assert job.status in ("review", "confirmed", "exported"), job.error_message
    stats = _line_metrics(benchmark_db, job)
    # 63 行任务，覆盖率（非估算/非未匹配）应 ≥50%
    coverage = (stats["total"] - stats["estimated"] - stats["unmatched"]) / max(stats["total"], 1)
    assert coverage >= 0.50, f"浙江三和覆盖率回退: {coverage:.1%} (stats={stats})"


@pytest.mark.skipif(
    not (_TESTDATA / "海口宁波赛特尔3.19_琼侨初中物理实验器材采购清单 - 空白本.xls").exists(),
    reason="基准数据未随仓库发布",
)
def test_benchmark_qiongqiao_does_not_regress(benchmark_db):
    job = _run_job(
        benchmark_db,
        _TESTDATA / "海口宁波赛特尔3.19_琼侨初中物理实验器材采购清单 - 空白本.xls",
        "qiongqiao",
    )
    assert job.status in ("review", "confirmed", "exported"), job.error_message
    stats = _line_metrics(benchmark_db, job)
    # 188 行，覆盖率应 ≥55%
    coverage = (stats["total"] - stats["estimated"] - stats["unmatched"]) / max(stats["total"], 1)
    assert coverage >= 0.55, f"琼侨覆盖率回退: {coverage:.1%} (stats={stats})"
