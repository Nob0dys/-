from __future__ import annotations

import re
import unicodedata
from functools import lru_cache


@lru_cache(maxsize=65536)
def _norm(text: object) -> str:
    return re.sub(r"[\s　]+", "", unicodedata.normalize("NFKC", str(text or "")).lower())


# ---- JY 教育装备编码类目 -------------------------------------------------
# 5 位 JY 编码前两位为大类。基于《JY/T 0386 初中理科教学仪器配备标准》。
JY_MAJOR = {
    "00": "通用仪器", "01": "通用仪器", "02": "通用仪器", "03": "通用仪器",
    "04": "计量", "05": "计量", "06": "计量",
    "10": "力学", "11": "力学", "12": "力学", "13": "力学", "14": "力学",
    "15": "力学", "16": "力学", "17": "力学",
    "20": "热学", "21": "热学", "22": "热学",
    "23": "电学", "24": "电磁学", "25": "电磁学", "26": "电磁学",
    "27": "电子", "28": "电子",
    "30": "光学", "31": "光学", "32": "光学",
    "33": "声学",
    "40": "化学仪器", "41": "化学药品", "42": "化学玻璃", "43": "化学玻璃",
    "50": "生物仪器", "51": "生物仪器",
    "60": "生物标本模型", "61": "生物标本模型",
    "70": "地理仪器", "71": "地理仪器",
    "80": "数学", "81": "数学",
    "90": "其他", "99": "其他",
}


@lru_cache(maxsize=65536)
def jy_major(code: str) -> str:
    text = _norm(code)
    if len(text) < 2 or not text[:2].isdigit():
        return ""
    return JY_MAJOR.get(text[:2], "")


# ---- 名称关键词类目推断 --------------------------------------------------
# 当记录没有 JY 编码时，用名称里的强关键词做粗分类。关键词是充分判据：
# 命中 → 类目确定；未命中 → unknown（不参与硬过滤）。
_NAME_CATEGORY_RULES: list[tuple[str, str]] = [
    # 力学-运动
    ("小车", "mech_cart"), ("斜面", "mech_cart"), ("滑轮", "mech_cart"),
    ("杠杆", "mech_lever"), ("轮轴", "mech_lever"), ("天平", "mech_scale"),
    ("弹簧测力", "mech_spring"), ("测力计", "mech_spring"), ("量筒", "mech_volume"),
    ("量杯", "mech_volume"),
    # 电磁-电源/电表
    ("电源", "em_power"), ("电池", "em_power"), ("电压表", "em_meter"),
    ("电流表", "em_meter"), ("万用表", "em_meter"), ("多用电表", "em_meter"),
    ("演示电表", "em_meter"), ("滑动变阻器", "em_resistor"), ("电阻箱", "em_resistor"),
    ("焦耳定律", "em_joule"), ("起电机", "em_generator"), ("发电机", "em_generator"),
    ("电磁继电器", "em_relay"), ("磁铁", "em_magnet"), ("强磁体", "em_magnet"),
    ("电磁铁", "em_magnet"), ("蹄形", "em_magnet"), ("条形", "em_magnet"),
    # 光学
    ("透镜", "opt_lens"), ("棱镜", "opt_lens"), ("平面镜", "opt_mirror"),
    ("潜望镜", "opt_mirror"), ("放大镜", "opt_magnifier"), ("显微镜", "opt_scope"),
    ("望远镜", "opt_scope"), ("光具座", "opt_bench"),
    # 热学
    ("温度计", "th_thermo"), ("干湿泡", "th_thermo"), ("热量计", "th_heat"),
    ("比热容", "th_heat"),
    # 声学
    ("音叉", "ac_fork"), ("发音齿轮", "ac_gear"), ("共鸣", "ac_fork"),
    # 化学-玻璃
    ("烧杯", "ch_beaker"), ("烧瓶", "ch_beaker"), ("锥形瓶", "ch_beaker"),
    ("试管", "ch_tube"), ("滴管", "ch_tube"), ("漏斗", "ch_tube"),
    ("集气瓶", "ch_gas"), ("广口瓶", "ch_gas"), ("细口瓶", "ch_gas"),
    ("酒精灯", "ch_lamp"), ("温度计套", "ch_lamp"),
    # 生物
    ("载玻片", "bio_slide"), ("盖玻片", "bio_slide"), ("计数板", "bio_slide"),
    ("解剖", "bio_dissect"), ("手术剪", "bio_dissect"), ("刀片", "bio_dissect"),
    ("离心机", "bio_centri"), ("恒温箱", "bio_incub"), ("培养皿", "bio_slide"),
    # 常见易混淆跨类目
    ("支架", "mech_cart"), ("铁架台", "mech_cart"), ("演示台", "mech_cart"),
    ("升降台", "mech_cart"), ("三脚架", "mech_cart"),
]


@lru_cache(maxsize=65536)
def infer_category_from_name(name: object) -> str:
    """Return a coarse category like ``em_power`` / ``mech_cart`` based on
    strong keywords in the product name.  "" when ambiguous."""
    text = _norm(name)
    for keyword, category in _NAME_CATEGORY_RULES:
        if keyword in text:
            return category
    # 学段标记是次级分类
    return ""


# ---- 类目一致性 -----------------------------------------------------------
@lru_cache(maxsize=262144)
def category_compatible(line_code: object, record_code: object,
                        line_name: object, record_name: object) -> tuple[bool, str]:
    """Decide whether an inquiry line and a history record may belong to
    comparable product families.

    Returns ``(compatible, reason)`` where reason explains any rejection.
    """
    line_major = jy_major(str(line_code or ""))
    record_major = jy_major(str(record_code or ""))
    if line_major and record_major and line_major != record_major:
        return False, f"类目不符(JY {line_major} vs {record_major})"

    line_cat = infer_category_from_name(line_name)
    record_cat = infer_category_from_name(record_name)
    if line_cat and record_cat and line_cat != record_cat:
        return False, f"类目不符({line_cat} vs {record_cat})"
    return True, ""


def same_category_pool(line: dict, records: list[dict]) -> list[dict]:
    """硬过滤：JY 大宗或名称类目不一致的记录直接剔除。
    任意一端无法判定时保留（不阻塞）。line 侧类目只算一次。"""
    line_major = jy_major(str(line.get("product_code") or ""))
    line_cat = infer_category_from_name(line.get("name"))
    pool: list[dict] = []
    for record in records:
        record_major = jy_major(str(record.get("product_code") or ""))
        if line_major and record_major and line_major != record_major:
            continue
        record_cat = infer_category_from_name(record.get("name"))
        if line_cat and record_cat and line_cat != record_cat:
            continue
        pool.append(record)
    return pool
