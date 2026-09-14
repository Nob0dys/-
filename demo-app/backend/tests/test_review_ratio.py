"""复核比例控制（QUOTE_REVIEW_TARGET_PERCENT）测试。

fixture 命名统一带"配额"前缀、编码用 900xx 段，与种子历史库隔离。
场景（30 行）：24 干净行 + 3 severe（量程冲突）+ 1 低分行 + 1 BLOCK 行 + 1 无候选行，
目标 10% → target=3，hard=2（BLOCK+无候选），budget=1 → 恰有 1 个 severe 行被降级。
"""

from io import BytesIO

from fastapi.testclient import TestClient
from openpyxl import Workbook

from app.database import SessionLocal, init_db
from app.main import app
from app.matching import normalize_text
from app.models import HistoryQuote


CLEAN_ROWS = list(range(2, 26))          # 24 行
SEVERE_ROWS = list(range(26, 29))        # 3 行
LOW_SCORE_ROW = 29
BLOCK_ROW = 30
NO_CANDIDATE_ROW = 31


def _insert(records: list[dict]) -> None:
    init_db()
    db = SessionLocal()
    try:
        for rec in records:
            db.add(
                HistoryQuote(
                    id=rec["id"],
                    source_file="赛特尔25年.xls",
                    source_sheet="初中物理",
                    source_row=0,
                    name=rec["name"],
                    normalized_name=normalize_text(rec["name"]),
                    spec=rec.get("spec", ""),
                    normalized_spec=normalize_text(rec.get("spec", "")),
                    product_code=rec.get("product_code", ""),
                    normalized_product_code=normalize_text(rec.get("product_code", "")),
                    unit=rec.get("unit", "个"),
                    normalized_unit=normalize_text(rec.get("unit", "个")),
                    price=rec.get("price", 10.0),
                    data_quality=0.5,
                )
            )
        db.commit()
    finally:
        db.close()


def _workbook(rows: list[list]) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Sheet1"
    sheet.append(["编号", "名称", "技术要求", "单位", "数量"])
    for row in rows:
        sheet.append(row)
    stream = BytesIO()
    workbook.save(stream)
    return stream.getvalue()


def _login(client: TestClient):
    response = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    assert response.status_code == 200, response.text


def _run_job(client: TestClient, rows: list[list]) -> dict[int, dict]:
    response = client.post(
        "/api/quote-jobs",
        data={"requested_option_count": 3},
        files={"file": ("询价.xlsx", _workbook(rows),
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    )
    assert response.status_code == 202, response.text
    job_id = response.json()["id"]
    lines = client.get(f"/api/quote-jobs/{job_id}/lines", params={"page_size": 100}).json()
    assert lines["total"] == len(rows), lines
    return {item["source_row"]: item for item in lines["items"]}


def _full_scenario() -> list[list]:
    rows: list[list] = []
    for i, source_row in enumerate(CLEAN_ROWS):
        code = f"900{i + 1:02d}"
        rows.append([code, f"配额洁净仪器{i + 1:02d}号", "规格100mm", "个", 1])
    for i in range(3):
        code = f"9003{i + 1}"
        rows.append([code, f"配额冲突仪器{i + 1}号", "量程500mm", "个", 1])
    rows.append(["", "配额低分仪器", "普通型，无特殊要求", "个", 1])
    rows.append(["90039", "配额阻断泵", "", "千克", 1])
    rows.append(["", "配额不存在仪器ZZZ", "特殊定制abc", "台", 1])
    return rows


def _full_fixture() -> None:
    records = [
        {
            "id": f"quota:clean:{i + 1}",
            "name": f"配额洁净仪器{i + 1:02d}号",
            "spec": "规格100mm",
            "product_code": f"900{i + 1:02d}",
            "price": 10.0,
        }
        for i in range(len(CLEAN_ROWS))
    ]
    records += [
        {
            "id": f"quota:severe:{i + 1}",
            "name": f"配额冲突仪器{i + 1}号",
            "spec": "量程100mm",
            "product_code": f"9003{i + 1}",
            "price": 10.0,
        }
        for i in range(3)
    ]
    records += [
        {"id": "quota:low", "name": "配额低分仪器", "spec": "精度0.1mm，量程100mm", "price": 5.0},
        # 单位 升 vs 询价 千克 → BLOCK: 单位不可直接换算
        {"id": "quota:block", "name": "配额阻断泵", "spec": "", "unit": "升", "price": 10.0, "product_code": "90039"},
    ]
    _insert(records)


def test_review_budget_caps_soft_demotions():
    """10% 目标下：review+unmatched ≤ ceil(30*10%)；降级先 severe 后低分；
    hard 行（BLOCK/无候选）恒为 review/unmatched；suggested 行全部高置信且有默认选中。"""
    _full_fixture()
    with TestClient(app) as client:
        _login(client)
        lines = _run_job(client, _full_scenario())
        assert len(lines) == 30

        by_status = {"suggested": [], "review": [], "unmatched": []}
        for row, item in lines.items():
            by_status[item["status"]].append(row)
        review_rows = set(by_status["review"])
        unmatched_rows = set(by_status["unmatched"])

        # 总量：hard(2) + 降级(1) = 3 = target
        assert len(review_rows) + len(unmatched_rows) == 3
        # hard 行恒人工：BLOCK → review，无候选 → unmatched
        assert BLOCK_ROW in review_rows
        assert unmatched_rows == {NO_CANDIDATE_ROW}
        # 降级的那 1 行必须来自 severe 组（severe 优先于低分）
        soft_demoted = review_rows - {BLOCK_ROW}
        assert len(soft_demoted) == 1
        assert soft_demoted.pop() in SEVERE_ROWS
        # 低分但无 severe 警告的行在 severe 组未耗尽时不降级
        assert lines[LOW_SCORE_ROW]["status"] == "suggested"
        # suggested 行全部高置信且有默认选中方案
        for row in by_status["suggested"]:
            assert lines[row]["confidence"] == "high"
            assert lines[row]["selected_option_count"] >= 1
        # severe 组其余两行仍为 suggested（额度已用完）
        remaining_severe = set(SEVERE_ROWS) - review_rows
        for row in remaining_severe:
            assert lines[row]["status"] == "suggested"


def test_hard_lines_not_squeezed_below_target():
    """hard 行数超过 10% 额度时如实超过：review+unmatched = hard 数。"""
    _insert([
        {"id": "quota:h1", "name": "配额超硬仪器一", "spec": "", "unit": "升", "product_code": "90041", "price": 10.0},
        {"id": "quota:h2", "name": "配额超硬仪器二", "spec": "", "unit": "升", "product_code": "90042", "price": 10.0},
    ])
    rows = [
        ["90041", "配额超硬仪器一", "", "千克", 1],   # BLOCK
        ["90042", "配额超硬仪器二", "", "千克", 1],   # BLOCK
        ["", "配额超硬不存在ZZZ", "特殊定制xyz", "台", 1],  # 无候选
    ] + [["", f"配额超硬干净{i}号", "规格100mm", "个", 1] for i in range(1, 8)]
    _insert([
        {"id": f"quota:h-clean:{i}", "name": f"配额超硬干净{i}号", "spec": "规格100mm", "price": 10.0}
        for i in range(1, 8)
    ])
    with TestClient(app) as client:
        _login(client)
        lines = _run_job(client, rows)
        review = [i for i in lines.values() if i["status"] == "review"]
        unmatched = [i for i in lines.values() if i["status"] == "unmatched"]
        # hard=3 > target=1：如实保留，不强行自动通过
        assert len(review) + len(unmatched) == 3
        assert len(unmatched) == 1
        assert all(i["confidence"] == "high" for i in lines.values() if i["status"] == "suggested")


def test_zero_target_passes_everything_except_hard():
    """QUOTE_REVIEW_TARGET_PERCENT=0：除 hard 行外全部 suggested。"""
    _insert([
        {"id": "quota:z1", "name": "配额零额仪器一", "spec": "规格100mm", "product_code": "90045", "price": 10.0},
        {"id": "quota:z2", "name": "配额零额仪器二", "spec": "规格100mm", "product_code": "90046", "price": 10.0},
    ])
    rows = [
        ["90045", "配额零额仪器一", "规格100mm", "个", 1],
        ["90046", "配额零额仪器二", "规格100mm", "个", 1],
        ["", "配额零额不存在ZZZ", "特殊定制q", "台", 1],
        ["", "配额零额低分仪器", "普通型，无特殊要求", "个", 1],
    ]
    _insert([{"id": "quota:z-low", "name": "配额零额低分仪器", "spec": "精度0.1mm，量程100mm", "price": 5.0}])
    import app.services as services

    original = services.REVIEW_TARGET_PERCENT
    services.REVIEW_TARGET_PERCENT = 0.0
    try:
        with TestClient(app) as client:
            _login(client)
            lines = _run_job(client, rows)
            statuses = {item["source_row"]: item["status"] for item in lines.values()}
            assert statuses[2] == "suggested"
            assert statuses[3] == "suggested"
            assert statuses[4] == "unmatched"   # 无候选 hard 行仍保留
            assert statuses[5] == "suggested"   # 低分行也自动通过
    finally:
        services.REVIEW_TARGET_PERCENT = original
