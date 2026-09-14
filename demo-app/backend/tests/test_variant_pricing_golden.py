"""Golden 回归测试：用户错误清单（报价错误.txt）全部 10 个案例。

数据用 fixture 自建（赛特尔25年.xls 来源的历史行直接写库），不依赖生产库。
名称统一加"黄金"前缀（编码用 888xx 段）避免与种子历史库（普教清单/凯迪）串池；
钻台案例为还原 0.6<0.65 的名称门槛场景，使用生产真实名称+独占编码。
"""

from io import BytesIO

from fastapi.testclient import TestClient
from openpyxl import Workbook

from app.database import SessionLocal
from app.excel_service import parse_history_workbook
from app.main import app
from app.matching import normalize_text
from app.models import HistoryQuote


def insert_history(records: list[dict]) -> None:
    """直接写历史库。records: {id, name, spec, price, product_code?, unit?, sheet?}。"""
    from app.database import init_db

    init_db()  # TestClient 启动前表可能尚未创建
    db = SessionLocal()
    try:
        for rec in records:
            db.add(
                HistoryQuote(
                    id=rec["id"],
                    source_file="赛特尔25年.xls",
                    source_sheet=rec.get("sheet", "初中物理"),
                    source_row=rec.get("source_row", 0),
                    name=rec["name"],
                    normalized_name=normalize_text(rec["name"]),
                    spec=rec.get("spec", ""),
                    normalized_spec=normalize_text(rec.get("spec", "")),
                    product_code=rec.get("product_code", ""),
                    normalized_product_code=normalize_text(rec.get("product_code", "")),
                    unit=rec.get("unit", "个"),
                    normalized_unit=normalize_text(rec.get("unit", "个")),
                    price=rec["price"],
                    data_quality=0.5,
                )
            )
        db.commit()
    finally:
        db.close()


def inquiry_workbook(rows: list[list]) -> bytes:
    """询价单：编号/名称/技术要求/单位/数量（与生产文件表头一致）。"""
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Sheet1"
    sheet.append(["编号", "名称", "技术要求", "单位", "数量"])
    for row in rows:
        sheet.append(row)
    stream = BytesIO()
    workbook.save(stream)
    return stream.getvalue()


def login(client: TestClient):
    response = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    assert response.status_code == 200, response.text


def run_job(client: TestClient, rows: list[list]) -> dict[int, dict]:
    """建任务（不关联客户）并返回 {source_row: line_detail}。"""
    response = client.post(
        "/api/quote-jobs",
        data={"requested_option_count": 3},
        files={"file": ("询价.xlsx", inquiry_workbook(rows),
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    )
    assert response.status_code == 202, response.text
    job_id = response.json()["id"]
    lines = client.get(f"/api/quote-jobs/{job_id}/lines", params={"page_size": 100}).json()
    assert lines["total"] == len(rows), lines
    return {
        item["source_row"]: client.get(f"/api/quote-lines/{item['id']}").json()
        for item in lines["items"]
    }


def primary_option(line_detail: dict) -> dict:
    """默认选中方案中 rank 最小（报价1）的选项。"""
    selected = [option for option in line_detail["options"] if option["selected"]]
    assert selected, f"无默认选中方案: {line_detail['name']}"
    return sorted(selected, key=lambda item: item["rank"])[0]


def test_syringe_same_code_specs_get_distinct_prices():
    """案例7（444-446行）：同码 02102 注射器 10/50/100mL 默认价互不相同。"""
    insert_history([
        {"id": "golden:syr:10", "name": "黄金注射器", "spec": "10mL，塑料", "unit": "只", "price": 1.0, "product_code": "88801"},
        {"id": "golden:syr:50", "name": "黄金注射器", "spec": "50mL，塑料", "unit": "只", "price": 2.0, "product_code": "88801"},
        {"id": "golden:syr:100", "name": "黄金注射器", "spec": "100mL，塑料", "unit": "支", "price": 8.0, "product_code": "88801"},
    ])
    with TestClient(app) as client:
        login(client)
        details = run_job(client, [
            ["88801", "黄金注射器", "一次性无菌注射器，10mL。", "只", 50],
            ["88801", "黄金注射器", "50mL，塑料。", "只", 5],
            ["88801", "黄金注射器", "100mL，塑料。", "只", 5],
        ])
        prices = [primary_option(details[row])["final_price"] for row in (2, 3, 4)]
        assert prices == [1.0, 2.0, 8.0]
        assert len(set(prices)) == 3


def test_dissecting_kit_pcs_distinguishes_variants():
    """案例10（671/672行）：解剖器 7件=22元 / 4件=14元，参数可识别。"""
    insert_history([
        {"id": "golden:kit:7", "name": "黄金解剖器", "spec": "7件", "unit": "套", "price": 22.0, "product_code": "88821", "sheet": "初中生物"},
        {"id": "golden:kit:4", "name": "黄金解剖器", "spec": "4件", "unit": "套", "price": 14.0, "product_code": "88822", "sheet": "初中生物"},
    ])
    with TestClient(app) as client:
        login(client)
        details = run_job(client, [
            [88821, "黄金解剖器", "不锈钢材料，7 件(大、小剪刀，大、小镊子，解剖刀，解剖针，弯头镊)。", "套", 2],
            [88822, "黄金解剖器", "不锈钢材料，4 件(大剪刀，解剖刀，解剖针，弯头镊)。", "套", 13],
        ])
        assert primary_option(details[2])["final_price"] == 22.0
        assert primary_option(details[3])["final_price"] == 14.0


def test_molecule_model_student_group_beats_demo():
    """案例8（490行）：分子结构模型（学生分组用）选中 分组用 40 元而非演示用 140 元。
    同码三变体（演示用/分组用/初中用）下，489 式演示用询价也必须选中演示用 140 元
    （参数保底不得抹平同码变体的排序差异）。"""
    insert_history([
        {"id": "golden:mol:demo", "name": "黄金分子结构模型", "spec": "演示用，氢原子球直径不小于23mm，其他原子球直径不小于30mm", "unit": "套", "price": 140.0, "product_code": "88831", "sheet": "高中化学"},
        {"id": "golden:mol:group", "name": "黄金分子结构模型", "spec": "分组用", "unit": "套", "price": 40.0, "product_code": "88831", "sheet": "高中化学"},
        {"id": "golden:mol:junior", "name": "黄金分子结构模型", "spec": "初中用", "unit": "套", "price": 55.0, "product_code": "88831", "sheet": "初中化学"},
    ])
    with TestClient(app) as client:
        login(client)
        details = run_job(client, [
            [88831, "黄金分子结构模型", "初中分组学生用，本模型主要是可组合新教材中的初中分子结构模型:氢气、氧气、水分子、二氧化碳分子、甲烷等分子结构。", "套", 13],
            [88831, "黄金分子结构模型", "演示用，可搭出化学教材中无机物和有机物各种分子的结构式。", "套", 1],
        ])
        group_line = primary_option(details[2])
        assert group_line["final_price"] == 40.0
        assert group_line["record"]["id"] == "golden:mol:group"
        demo_line = primary_option(details[3])
        assert demo_line["final_price"] == 140.0
        assert demo_line["record"]["id"] == "golden:mol:demo"


def test_flask_not_matched_to_flask_brush():
    """案例5（401行）：烧瓶 500mL 候选含 烧瓶刷 2元 时不得选中烧瓶刷。"""
    insert_history([
        {"id": "golden:flask:9", "name": "黄金烧瓶", "spec": "圆、长，500mL", "unit": "个", "price": 9.0},
        {"id": "golden:flask:15", "name": "黄金烧瓶", "spec": "500mL", "unit": "个", "price": 15.0},
        {"id": "golden:brush:2", "name": "黄金烧瓶刷", "spec": "500mL烧瓶用", "unit": "个", "price": 2.0},
    ])
    with TestClient(app) as client:
        login(client)
        details = run_job(client, [
            ["", "黄金烧瓶", "高硼硅玻璃材质；圆底，500mL。", "个", 5],
        ])
        primary = primary_option(details[2])
        assert primary["record"]["name"] == "黄金烧瓶"
        assert primary["final_price"] in (9.0, 15.0)


def test_beaker_same_single_value_no_false_range_warning():
    """案例6（400行）：烧杯 500mL 对 500mL 记录无"规格量程不符"假警告，价 8 元。"""
    insert_history([
        {"id": "golden:beaker:8", "name": "黄金烧杯", "spec": "500mL", "unit": "个", "price": 8.0},
    ])
    with TestClient(app) as client:
        login(client)
        details = run_job(client, [
            ["", "黄金烧杯", "高硼硅玻璃材质；500mL。", "个", 5],
        ])
        primary = primary_option(details[2])
        assert primary["final_price"] == 8.0
        # 修复目标：规格完全一致的记录（含默认选中）不再有"500~500 不符"假警告；
        # 其它容量（10mL 等）的真实量程警告仍然保留，不在本断言范围。
        assert not any(str(w).startswith("规格量程") for w in primary["warnings"]), primary["warnings"]
        for option in details[2]["options"]:
            assert not any("500~500ml，候选500~500ml" in str(w) for w in option["warnings"]), option["warnings"]


def test_glass_rod_phi_range_parsing():
    """案例9（597行）：Φ3mm~Φ4mm 询价选中 φ3~4 记录且无假警告；φ5~6 带真实量程警告。"""
    insert_history([
        {"id": "golden:rod:34", "name": "黄金玻璃棒", "spec": "φ3mm～φ4mm", "unit": "千克", "price": 12.0},
        {"id": "golden:rod:56", "name": "黄金玻璃棒", "spec": "φ5mm～φ6mm", "unit": "千克", "price": 13.0},
    ])
    with TestClient(app) as client:
        login(client)
        details = run_job(client, [
            ["", "黄金玻璃棒", "透明钠钙玻璃材质； Φ3mm~Φ4mm。", "千克", 2],
        ])
        primary = primary_option(details[2])
        assert primary["record"]["id"] == "golden:rod:34"
        assert primary["final_price"] == 12.0
        assert not any(str(w).startswith("规格量程") for w in primary["warnings"])
        rod56 = next(o for o in details[2]["options"] if o["record"]["id"] == "golden:rod:56")
        assert any(str(w).startswith("规格量程") for w in rod56["warnings"])


def test_exact_code_relaxed_name_gate_hand_crank():
    """案例4（305行）：手摇离心钻台 编码精确命中"手摇离心转台"，有默认选中方案。"""
    insert_history([
        {"id": "golden:crank:90", "name": "手摇离心转台", "spec": "", "unit": "台", "price": 90.0, "product_code": "88841"},
    ])
    with TestClient(app) as client:
        login(client)
        details = run_job(client, [
            [88841, "手摇离心钻台", "产品应由机座、传动系统（包括带手柄的主动轮、从动轮）等部件组成", "个", 1],
        ])
        primary = primary_option(details[2])
        assert primary["record"]["id"] == "golden:crank:90"
        assert primary["final_price"] == 90.0


def test_demo_pulley_block_distinguished_from_plain():
    """案例2（301行）：演示滑轮组（21031）选中演示滑轮组价而非滑轮组。"""
    insert_history([
        {"id": "golden:pulley:demo", "name": "黄金演示滑轮组", "spec": "单2，三并2，三串2", "unit": "组", "price": 18.0, "product_code": "88851"},
        {"id": "golden:pulley:plain", "name": "黄金滑轮组", "spec": "单4，二并2，二串2", "unit": "组", "price": 9.0, "product_code": "88852"},
    ])
    with TestClient(app) as client:
        login(client)
        details = run_job(client, [
            [88851, "黄金演示滑轮组", "1.包含单滑轮 2 个，轮盘 1 个；三并滑轮 2 个，轮盘 3 个；三串滑轮 2 个，轮盘 3 个，二件支杆滑轮；2.每个滑轮组中应至少有一个可止动滑轮。", "组", 1],
        ])
        primary = primary_option(details[2])
        assert primary["record"]["id"] == "golden:pulley:demo"
        assert primary["final_price"] == 18.0


def test_import_strips_decimal_suffix_from_codes(tmp_path):
    """案例：xls 数值格编码 "27001.0" 导入后 product_code=="27001"（编号列与型号列均覆盖）。"""
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "价目"
    sheet.append(["编号", "名称", "参数", "型号", "单位", "单价"])
    sheet.append([27001.0, "清洗测试仪器甲", "规格文本", "", "台", 10.0])
    sheet.append(["", "清洗测试仪器乙", "规格文本", 27002.0, "台", 20.0])
    path = tmp_path / "codes.xlsx"
    workbook.save(path)

    records = parse_history_workbook(str(path), "codes.xlsx")
    by_name = {record["name"]: record for record in records}
    assert by_name["清洗测试仪器甲"]["product_code"] == "27001"
    # 型号列的 "27002.0" 清洗后识别为 5 位 JY 编码并归入产品编码
    assert by_name["清洗测试仪器乙"]["product_code"] == "27002"
    assert by_name["清洗测试仪器乙"]["model"] == ""
