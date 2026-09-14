from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from functools import lru_cache
from statistics import median
from typing import Iterable

from .category import category_compatible, same_category_pool, infer_category_from_name, jy_major

import datetime as _dt

_DATE_PAT = re.compile(r"(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})")
_QUARTER_PAT = re.compile(r"(\d{4})\s*[Qq]([1-4])")


def _parse_quote_date(text: object) -> _dt.date | None:
    """Parse ``YYYY-MM-DD`` / ``YYYY/M/D`` / ``YYYY年M月D日`` / ``YYYYQn`` / ``YYYY`` to a date."""
    s = str(text or "").strip()
    if not s:
        return None
    m = _DATE_PAT.search(s)
    if m:
        try:
            return _dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    m = _QUARTER_PAT.search(s)
    if m:
        try:
            return _dt.date(int(m.group(1)), int(m.group(2)) * 3, 1)
        except ValueError:
            return None
    if re.fullmatch(r"\d{4}", s):
        try:
            return _dt.date(int(s), 1, 1)
        except ValueError:
            return None
    return None


def quote_recency_bonus(quote_date: object, today: _dt.date | None = None) -> float:
    """近 12 个月报价给小幅加分，>36 个月扣一点。版本差由 services 层单独检测。"""
    date = _parse_quote_date(quote_date)
    if date is None:
        return 0.0
    today = today or _dt.date.today()
    age_days = (today - date).days
    if age_days < 0:
        return 0.0
    if age_days <= 365:
        return 1.5
    if age_days <= 730:
        return 0.5
    if age_days <= 1095:
        return 0.0
    return -0.5


RELIABLE_THRESHOLD = 55.0
HIGH_THRESHOLD = 80.0
MEDIUM_THRESHOLD = 65.0


@lru_cache(maxsize=65536)
def normalize_text(value: object) -> str:
    return re.sub(
        r"[\s\u3000·•，,。.;；:：()（）\[\]【】<>《》“”\"'‘’/\\_—–-]+",
        "",
        unicodedata.normalize("NFKC", str(value or "")).lower(),
    )


NAME_PREFIX_RE = re.compile(r"^(高中物理|高中生物|高中化学|初中物理|初中生物|初中化学)")

GRADE_MARKER_RE = re.compile(r"^(高中|初中|小学)")

TRAIL_VARIANT_RE = re.compile(r"\d{1,2}$")


@lru_cache(maxsize=65536)
def grade_marker(value: object) -> str:
    match = GRADE_MARKER_RE.match(normalize_text(value))
    return match.group(1) if match else ""


@lru_cache(maxsize=65536)
def name_core(value: object) -> str:
    """Name key that ignores subject prefixes and trailing variant markers.

    Procurement lists prefix items with 高中物理/高中生物/… and number variants
    (游标卡尺1/游标卡尺2). Treating those as ignorable lets the same product
    from different sources (赛特尔/普教/理化生) score equally, while variant
    disambiguation (数显/数字/指针) still happens via raw spec features.
    A bare grade marker (高中学生电源 vs 学生电源) is preserved on purpose:
    those are different products (高中电源 vs 初中电源).
    """
    text = normalize_text(value)
    if len(text) >= 4:
        while True:
            stripped = NAME_PREFIX_RE.sub("", text)
            if stripped == text:
                break
            text = stripped
    if len(text) >= 2:
        text = TRAIL_VARIANT_RE.sub("", text)
    return text or normalize_text(value)


@lru_cache(maxsize=1048576)
def bigram_dice(left: object, right: object) -> float:
    a, b = normalize_text(left), normalize_text(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if len(a) < 2 or len(b) < 2:
        return float(a == b)
    counts: dict[str, int] = {}
    for index in range(len(a) - 1):
        gram = a[index : index + 2]
        counts[gram] = counts.get(gram, 0) + 1
    overlap = 0
    for index in range(len(b) - 1):
        gram = b[index : index + 2]
        if counts.get(gram, 0) > 0:
            overlap += 1
            counts[gram] -= 1
    return 2 * overlap / (len(a) + len(b) - 2)


PARAM_PATTERN = re.compile(
    r"\d+(?:\.\d+)?(?:\s*[x×*~～-]\s*\d+(?:\.\d+)?){0,3}\s*"
    r"(?:mm|cm|m|mg|kg|g|ml|l|w|kw|v|a|hz|pa|kpa|mpa|℃|°c|%)?",
    re.IGNORECASE,
)


@lru_cache(maxsize=262144)
def parameter_tokens(value: str) -> frozenset[str]:
    text = unicodedata.normalize("NFKC", value or "").lower().replace(" ", "")
    return frozenset(
        normalize_text(token) for token in PARAM_PATTERN.findall(text) if normalize_text(token)
    )


@lru_cache(maxsize=262144)
def parameter_similarity(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    if normalize_text(left) == normalize_text(right):
        return 1.0

    # P0: 范围比较（优先于所有其他逻辑）
    left_ranges = spec_ranges(left)
    right_ranges = spec_ranges(right)
    range_bonus = 0.0
    has_ranges = False
    for unit in ("mm", "ml", "g", "a", "v", "w", "℃"):
        lr = left_ranges.get(unit)
        rr = right_ranges.get(unit)
        if lr and rr:
            has_ranges = True
            overlap = max(0, min(lr[1], rr[1]) - max(lr[0], rr[0]))
            total = max(lr[1], rr[1]) - min(lr[0], rr[0])
            if total == 0:
                # 退化区间 (v,v) 对 (v,v)：单值相等视为完全一致
                range_bonus = max(range_bonus, 0.3)
                continue
            iou = overlap / total
            if iou > 0.8:
                range_bonus = max(range_bonus, 0.3)
            elif iou > 0.5:
                range_bonus = max(range_bonus, 0.15)
            elif iou < 0.2:
                range_bonus = min(range_bonus, -0.2)

    # 短 spec 是弱信号：4-6 字的"永磁、电磁场"类描述不应与长文本高相似，
    # 避免让"部分同名但规格巧合重叠"的候选压过真正同名的候选。
    left_len = len(normalize_text(left))
    right_len = len(normalize_text(right))
    if min(left_len, right_len) < 8:
        # 有范围时仍计算范围比较，否则用弱化 bigram
        if has_ranges:
            base = bigram_dice(left, right) * 0.4 + range_bonus
            return min(1.0, max(0.0, base))
        return bigram_dice(left, right) * 0.4
    left_tokens, right_tokens = parameter_tokens(left), parameter_tokens(right)
    token_score = 0.0
    if left_tokens and right_tokens:
        token_score = len(left_tokens & right_tokens) / len(left_tokens | right_tokens)
    base = 0.65 * bigram_dice(left, right) + 0.35 * token_score

    # P0: 范围比较加分
    base = min(1.0, base + range_bonus)

    # 量程信号：规格里的 长度(mm)/容量(ml)/质量(g)/电流(A)/电压(V)/功率(W)
    # 数值一致则加分，明显冲突则降分——500mm直尺 与 1000mm直尺 因此被区分。
    left_feat = _spec_features(left)
    right_feat = _spec_features(right)
    for unit in ("mm", "ml", "g", "a", "v", "w"):
        lv = left_feat["caps"].get(unit)
        rv = right_feat["caps"].get(unit)
        if lv and rv:
            ratio = rv / lv if lv else 0
            if 0.98 <= ratio <= 1.02:
                base = base * 0.7 + 0.3
            elif ratio > 1.5 or ratio < 1 / 1.5:
                base = base * 0.6
    return min(1.0, base)


# 缓存清理入口：match_line 每行调用一次，防止长任务中缓存无界增长。
def clear_match_caches() -> None:
    normalize_text.cache_clear()
    grade_marker.cache_clear()
    name_core.cache_clear()
    bigram_dice.cache_clear()
    parameter_tokens.cache_clear()
    parameter_similarity.cache_clear()
    _spec_features.cache_clear()
    _similarity.cache_clear()


UNIT_ALIASES = {
    "g": ("mass", 1.0, "克"),
    "克": ("mass", 1.0, "克"),
    "kg": ("mass", 1000.0, "千克"),
    "千克": ("mass", 1000.0, "千克"),
    "公斤": ("mass", 1000.0, "千克"),
    "mg": ("mass", 0.001, "毫克"),
    "毫克": ("mass", 0.001, "毫克"),
    "ml": ("volume", 1.0, "毫升"),
    "毫升": ("volume", 1.0, "毫升"),
    "l": ("volume", 1000.0, "升"),
    "升": ("volume", 1000.0, "升"),
    "mm": ("length", 1.0, "毫米"),
    "毫米": ("length", 1.0, "毫米"),
    "cm": ("length", 10.0, "厘米"),
    "厘米": ("length", 10.0, "厘米"),
    "m": ("length", 1000.0, "米"),
    "米": ("length", 1000.0, "米"),
}
PACKAGE_UNITS = {"瓶", "盒", "袋", "桶", "罐", "支", "包", "套"}
CONTENT_PATTERN = re.compile(
    r"(\d+(?:\.\d+)?)\s*(mg|kg|g|ml|l|毫克|千克|克|毫升|升)",
    re.IGNORECASE,
)


@dataclass
class UnitResult:
    status: str
    score: float
    normalized_price: float | None
    warning: str = ""


def package_content(spec: str, package_unit: str) -> tuple[str, float, str] | None:
    """Return a package's dimension and base quantity when net content is explicit."""
    text = unicodedata.normalize("NFKC", spec or "").lower()
    package = normalize_text(package_unit)
    if package not in PACKAGE_UNITS:
        return None
    # A quantity is only treated as package content when the specification says
    # net content or explicitly associates it with the package unit.
    if "净含量" not in text and f"每{package}" not in normalize_text(text) and f"/{package}" not in normalize_text(text):
        return None
    for amount_text, unit_text in CONTENT_PATTERN.findall(text):
        info = UNIT_ALIASES.get(normalize_text(unit_text))
        if info and info[0] in ("mass", "volume"):
            amount = float(amount_text) * info[1]
            return info[0], amount, f"{amount_text}{unit_text}"
    return None


def compare_units(
    target_unit: str,
    source_unit: str,
    source_price: float,
    target_spec: str = "",
    source_spec: str = "",
) -> UnitResult:
    target = normalize_text(target_unit)
    source = normalize_text(source_unit)
    if target and source and target == source:
        return UnitResult("exact", 1.0, source_price)
    if not target or not source:
        # 单位缺失给部分分（0.4），避免把无单位的历史记录整体判为不可靠
        return UnitResult("missing", 0.4, None, "单位信息缺失，请人工核对")
    target_info, source_info = UNIT_ALIASES.get(target), UNIT_ALIASES.get(source)
    if target_info and source_info and target_info[0] == source_info[0]:
        normalized = source_price / source_info[1] * target_info[1]
        return UnitResult(
            "convertible",
            0.7,
            round(normalized, 6),
            f"单位已换算：{source_unit} → {target_unit}",
        )
    # 单位兜底：套/个/支 通过 spec 里的 "N支/套" / "N件/套" 换算
    _PACKAGE_BASE_UNITS = {"个", "支", "件", "只", "条", "根"}
    _PACK_UNITS = {"套", "盒", "包", "箱"}
    # 中文计数量词全集（厂家要求：个/支/台/套 等中文量词互变不构成阻断项，
    # 只有 升/毫升/克/米 等物理量词才构成阻断）。
    COUNT_UNITS = (
        _PACKAGE_BASE_UNITS | _PACK_UNITS
        | {"台", "把", "块", "张", "副", "付", "对", "组", "辆", "架", "部",
           "片", "枚", "颗", "粒", "节", "双", "串", "排", "捆", "卷", "瓶",
           "袋", "桶", "罐", "支"}
    )
    def _extract_pack_ratio(spec: str, pack_unit: str) -> float | None:
        text = unicodedata.normalize("NFKC", spec or "")
        for base in _PACKAGE_BASE_UNITS:
            m = re.search(rf"(\d+(?:\.\d+)?)\s*{re.escape(base)}\s*/\s*{re.escape(pack_unit)}", text)
            if m:
                try:
                    return float(m.group(1))
                except (TypeError, ValueError):
                    continue
        return None
    if target in _PACKAGE_BASE_UNITS and source in _PACK_UNITS:
        ratio = _extract_pack_ratio(source_spec, source) or _extract_pack_ratio(target_spec, source)
        if ratio and ratio > 0:
            return UnitResult(
                "convertible",
                0.75,
                round(source_price / ratio, 6),
                f"包装换算:{source_unit}→{target_unit} (1{source_unit}={ratio:g}{target_unit})",
            )
    if target in _PACK_UNITS and source in _PACKAGE_BASE_UNITS:
        ratio = _extract_pack_ratio(target_spec, target) or _extract_pack_ratio(source_spec, target)
        if ratio and ratio > 0:
            return UnitResult(
                "convertible",
                0.75,
                round(source_price * ratio, 6),
                f"包装换算:{source_unit}→{target_unit} (1{target_unit}={ratio:g}{source_unit})",
            )
    # 中文计数量词互变（个↔只↔支↔台↔套↔把↔件↔根↔条…）：厂家要求不构成
    # 阻断项（BLOCK 会把 直尺[把/个]、仪器车[台/辆] 等正确价拦掉）。
    if target in COUNT_UNITS and source in COUNT_UNITS:
        return UnitResult(
            "convertible",
            0.7,
            round(source_price, 6),
            f"中文量词互换：{source_unit} → {target_unit}（价格不变）",
        )
    source_package = package_content(source_spec, source_unit)
    target_package = package_content(target_spec, target_unit)
    if target_info and source_package and target_info[0] == source_package[0]:
        normalized = source_price / source_package[1] * target_info[1]
        return UnitResult(
            "convertible",
            0.7,
            round(normalized, 6),
            f"单位按包装净含量换算：{source_unit}（{source_package[2]}） → {target_unit}",
        )
    if source_info and target_package and source_info[0] == target_package[0]:
        normalized = source_price / source_info[1] * target_package[1]
        return UnitResult(
            "convertible",
            0.7,
            round(normalized, 6),
            f"单位按包装净含量换算：{source_unit} → {target_unit}（{target_package[2]}）",
        )
    if source_package and target_package and source_package[0] == target_package[0]:
        normalized = source_price / source_package[1] * target_package[1]
        return UnitResult(
            "convertible",
            0.7,
            round(normalized, 6),
            f"包装单位已按净含量换算：{source_unit} → {target_unit}",
        )
    return UnitResult(
        "incompatible",
        0.0,
        None,
        f"BLOCK: 单位不可直接换算（询价 {target_unit} / 历史 {source_unit}）",
    )


def confidence_for(score: float, warnings: Iterable[str]) -> str:
    blocking = any(str(item).startswith("BLOCK:") for item in warnings)
    if score < RELIABLE_THRESHOLD:
        return "unreliable"
    if blocking:
        return "review"
    if score >= HIGH_THRESHOLD:
        return "high"
    if score >= MEDIUM_THRESHOLD:
        return "medium"
    return "low"


@dataclass
class Candidate:
    record: dict
    score: float
    confidence: str
    component_scores: dict[str, float]
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    unit_status: str = "exact"
    normalized_price: float | None = None


@lru_cache(maxsize=262144)
def _similarity(left: str, right: str) -> float:
    a, b = normalize_text(left), normalize_text(right)
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


DIGITAL_PAT = re.compile(r"数显|数字式|数字|液晶")
ANALOG_PAT = re.compile(r"指针式|指针|模拟式")
HALF_DIGIT_PAT = re.compile(r"\d\s*-\s*1\s*/\s*2\s*位|四位半|三位半")
MF_PAT = re.compile(r"mf\d+", re.IGNORECASE)
# 教学磁钢型号：D-CG-LT-180 / D-CG-LU-80 等（条形/蹄形磁铁规格标识）
MAGNET_MODEL_PAT = re.compile(r"d-cg-[a-z]{2}-\d+", re.IGNORECASE)
CAP_PATTERN = re.compile(
    r"(\d+(?:\.\d+)?)\s*(ml|毫升|l|升|g|克|kg|千克|公斤|mm|毫米|cm|厘米|m|米|a|安|v|伏|w|瓦)",
    re.IGNORECASE,
)

# P0: 规格范围提取模式
# 匹配 "Φ7～8mm" "φ7mm～8mm" "7-8mm" "7~8mm" "7—8mm" "7－8mm" 等范围格式；
# 第二个数字允许带 φ/Φ 前缀（"Φ3mm~Φ4mm" 应解析为 (3,4) 而非单值 (3,3)+(4,4)）
RANGE_PATTERN = re.compile(
    r"[Φφ]?\s*(\d+(?:\.\d+)?)\s*(?:mm|cm|m|ml|l|g|kg|mg|℃|°c|v|a|w|hz|pa|kpa|mpa|%)?\s*"
    r"[～~—－-]\s*[Φφ]?\s*(\d+(?:\.\d+)?)\s*"
    r"(mm|cm|m|ml|l|g|kg|mg|℃|°c|v|a|w|hz|pa|kpa|mpa|%)?",
    re.IGNORECASE,
)
# 匹配温度范围 "-30~50℃" "-50～40℃" 等（支持负数，NFKC后℃变°c）
TEMP_RANGE_PATTERN = re.compile(
    r"(-?\d+(?:\.\d+)?)\s*(?:[℃°c])?\s*[~～—－-]\s*(-?\d+(?:\.\d+)?)\s*(?:[℃°c])",
    re.IGNORECASE,
)

# P0: 单位标准化映射（统一全角/半角/中英文）
UNIT_MAP = {
    "毫米": "mm", "mm": "mm",
    "厘米": "cm", "cm": "cm",
    "米": "m", "m": "m",
    "毫升": "ml", "ml": "ml", "ml": "ml",
    "升": "l", "l": "l",
    "克": "g", "g": "g",
    "千克": "kg", "kg": "kg", "公斤": "kg",
    "毫克": "mg", "mg": "mg",
    "℃": "℃", "°c": "℃",
    "安": "a", "a": "a",
    "伏": "v", "v": "v",
    "瓦": "w", "w": "w",
    "hz": "hz", "pa": "pa", "kpa": "kpa", "mpa": "mpa",
}


@lru_cache(maxsize=262144)
def spec_ranges(text: str) -> dict:
    """从规格文本中提取结构化范围。

    返回 {unit: (min, max)} 字典，例如:
    - "Φ7～8mm" → {"mm": (7.0, 8.0)}
    - "-30~50℃" → {"℃": (-30.0, 50.0)}
    - "250×180×100mm" → {"mm": (100.0, 250.0)}

    额外返回 "dims" 键存储有序三维尺寸（用于精确比较）:
    - "250mm×180mm×100mm" → {"dims": [100.0, 180.0, 250.0]}
    """
    ranges = {}
    if not text:
        return ranges
    nt = unicodedata.normalize("NFKC", text).lower()

    # 温度范围（优先，避免被普通范围模式误捕）
    for m in TEMP_RANGE_PATTERN.finditer(nt):
        low, high = float(m.group(1)), float(m.group(2))
        if low > high:
            low, high = high, low
        ranges["℃"] = (low, high)

    # 一般范围（mm/ml/g/v/a/w 等）
    for m in RANGE_PATTERN.finditer(nt):
        low, high = float(m.group(1)), float(m.group(2))
        # 确定单位：优先取第二个捕获组（范围后的单位），否则取第一个数字后的单位
        raw_unit = m.group(3) or m.group(2) or ""
        unit = UNIT_MAP.get(raw_unit, raw_unit)
        if not unit:
            continue
        if low > high:
            low, high = high, low
        if unit not in ranges or (high - low) > (ranges[unit][1] - ranges[unit][0]):
            ranges[unit] = (low, high)

    # 三维/二维尺寸：保留有序维度用于精确比较
    # 250mm×180mm×100mm → dims=[100, 180, 250], mm=(100, 250)
    # φ15mm×150mm → dims=[15, 150], mm=(15, 150)
    multi_dim_pattern = re.compile(
        r"(\d+(?:\.\d+)?)\s*(mm|cm|m)?\s*[x×*]\s*"
        r"(\d+(?:\.\d+)?)\s*(mm|cm|m)?\s*(?:[x×*]\s*"
        r"(\d+(?:\.\d+)?)\s*(mm|cm|m))?"
    )
    for m in multi_dim_pattern.finditer(nt):
        vals = [float(m.group(1)), float(m.group(3))]
        if m.group(5):
            vals.append(float(m.group(5)))
        # 单位优先取最后一个有单位的
        raw_unit = m.group(6) or m.group(4) or m.group(2) or ""
        unit = UNIT_MAP.get(raw_unit, raw_unit)
        if unit:
            # 存储有序维度（排序后）
            sorted_vals = sorted(vals)
            if "dims" not in ranges or len(sorted_vals) > len(ranges["dims"]):
                ranges["dims"] = sorted_vals
            # 同时存储范围
            if unit not in ranges:
                ranges[unit] = (min(vals), max(vals))

    # 单值兜底：如果有 CAP_PATTERN 匹配但没进 range（如 "150mm" 无范围）
    # 将单值存储为 (value, value) 范围，用于跨规格比较
    for m in CAP_PATTERN.finditer(nt):
        value = float(m.group(1))
        raw_unit = m.group(2)
        unit = UNIT_MAP.get(raw_unit, raw_unit)
        if not unit or unit in ranges:
            continue
        ranges[unit] = (value, value)

    return ranges

# 修饰词变体表（理化生教学仪器常见“同根词不同产品”），写正则而非字面集合：
# line/record 去掉这些修饰词后相同 → 视为不同产品，给 12 分罚。
MODIFIER_PENALTY_PAT = re.compile(
    r"(演示|实验|图形|内能|新型|高中|初中|小学|学生用|教师用|教学用|数显|指针|液晶|"
    r"高压|低压|可调|精密|简易|普通|袖珍|微量|便携|台式|立式|手持|"
    r"不锈钢|铜质|铁质|塑料|玻璃|木质|单面|双面|电磁式|永磁式|"
    r"数字式|模拟式|普通型|高精度)"
)


@lru_cache(maxsize=262144)
def _spec_features(text: object) -> dict:
    """Extract distinguishing features from a product name+spec.

    Used to keep product variants apart: digital vs analog meters, rated
    capacity/量程 (g/ml/mm/A/V), half-digit resolution and MF model codes.
    """
    nt = unicodedata.normalize("NFKC", str(text or "")).lower()
    features: dict = {
        "digital": bool(DIGITAL_PAT.search(nt)),
        "analog": bool(ANALOG_PAT.search(nt)),
        "half": bool(HALF_DIGIT_PAT.search(nt)),
        "caps": {},
        "mf": list(dict.fromkeys(MF_PAT.findall(nt))),
        "magnet": list(dict.fromkeys(MAGNET_MODEL_PAT.findall(nt))),
        # 学生用/教学用 规格定位（磁铁/天平/电源等）：学生用=小规格低价档，
        # 教学用=大规格高价档。询价与候选定位冲突时罚分。
        # 注意："教学用磁钢极性标注"是标准表述（含"教学用"但非定位词），
        # 用 学生用/教师用/演示用 等明确定位词判断，避免误触发。
        # "分组用"（学生分组实验用）归入学生定位：分子结构模型 分组用40元
        # 应与 演示用140元 区分。
        "student": bool(re.search(r"学生用|学生型|分组用", nt)),
        "teaching": bool(re.search(r"教师用|演示用|教学用(?!磁钢)", nt)),
        # P1: 形状关键词
        "shape": "",
        # P1: 材质关键词
        "material": "",
        # P1: 放大倍数
        "magnification": "",
        # P1: 套件件数（"7件"/"4件套"）：解剖器 7件 vs 4件 等套件规格的区分维度。
        # 只支持阿拉伯数字——"二件支杆滑轮" 等中文数词不提取，避免误判。
        "pcs": None,
    }

    # P1: 形状提取
    shape_patterns = [
        (r"u型|u形", "U型"),
        (r"直型|直形", "直型"),
        (r"单球", "单球"),
        (r"双球", "双球"),
        (r"弯管|弯形", "弯管"),
        (r"方形", "方形"),
        (r"圆形", "圆形"),
    ]
    for pattern, shape_name in shape_patterns:
        if re.search(pattern, nt):
            features["shape"] = shape_name
            break

    # P1: 材质提取
    material_patterns = [
        (r"高硼硅", "高硼硅"),
        (r"钠钙", "钠钙"),
        (r"石英", "石英"),
        (r"硼硅", "硼硅"),
        (r"塑料", "塑料"),
        (r"不锈钢", "不锈钢"),
        (r"铜制|铜质", "铜"),
        (r"铁制|铁质", "铁"),
        (r"铝制|铝质", "铝"),
        (r"玻璃", "玻璃"),
        (r"木质|木制", "木"),
    ]
    for pattern, mat_name in material_patterns:
        if re.search(pattern, nt):
            features["material"] = mat_name
            break

    # P1: 放大倍数提取 (200× 或 200×10 格式)
    mag_match = re.search(r"(\d+)\s*[×xX](?:\s*(\d+))?", nt)
    if mag_match:
        if mag_match.group(2):
            features["magnification"] = f"{mag_match.group(1)}×{mag_match.group(2)}"
        else:
            features["magnification"] = f"{mag_match.group(1)}×"

    # P1: 套件件数提取（"7件"/"4件套"）
    pcs_match = re.search(r"(\d+(?:\.\d+)?)\s*件\s*套?", nt)
    if pcs_match:
        features["pcs"] = float(pcs_match.group(1))

    for amount_text, unit_text in CAP_PATTERN.findall(nt):
        value = float(amount_text)
        unit = normalize_text(unit_text)
        if unit in ("千克", "公斤", "kg"):
            unit, value = "g", value * 1000
        elif unit in ("升", "l"):
            unit, value = "ml", value * 1000
        elif unit in ("厘米", "cm"):
            unit, value = "mm", value * 10
        elif unit in ("米", "m"):
            unit, value = "mm", value * 1000
        elif unit in ("克", "g"):
            unit = "g"
        elif unit in ("毫升", "ml"):
            unit = "ml"
        elif unit in ("毫米", "mm"):
            unit = "mm"
        else:
            continue
        if unit not in features["caps"] or value > features["caps"][unit]:
            features["caps"][unit] = value
    return features


def numeric_spec_bonus(line: dict, record: dict) -> float:
    """结构化规格数值加分：询价与候选在功率/容量/长度类量纲（g/ml/a/v/w/mm）上
    数值一致时加分，让规格真正吻合的记录排得更前（配合已有的冲突惩罚）。"""
    line_text = f"{line.get('name', '')} {line.get('spec', '')}"
    record_text = f"{record.get('name', '')} {record.get('spec', '')}"
    line_feat = _spec_features(line_text)
    record_feat = _spec_features(record_text)

    bonus = 0.0

    # P4: 范围比较加分（IoU > 0.8 时加分）
    line_ranges = spec_ranges(line_text)
    record_ranges = spec_ranges(record_text)
    for unit in ("g", "ml", "a", "v", "w", "mm", "℃"):
        lr = line_ranges.get(unit)
        rr = record_ranges.get(unit)
        if lr and rr:
            overlap = max(0, min(lr[1], rr[1]) - max(lr[0], rr[0]))
            total = max(lr[1], rr[1]) - min(lr[0], rr[0])
            if total == 0:
                # 退化区间 (v,v) 对 (v,v)：单值相等视为完全一致
                bonus += 2.0
            else:
                iou = overlap / total
                if iou > 0.8:
                    bonus += 2.0

    # 单值比较
    for unit in ("g", "ml", "a", "v", "w", "mm"):
        line_value = line_feat["caps"].get(unit)
        record_value = record_feat["caps"].get(unit)
        if line_value and record_value and abs(record_value - line_value) / line_value <= 0.02:
            bonus += 2.0
    # 套件件数一致加分（解剖器 7件 vs 7件）
    if line_feat["pcs"] and record_feat["pcs"] and line_feat["pcs"] == record_feat["pcs"]:
        bonus += 2.0
    return min(bonus, 6.0)


def variant_penalty(line: dict, record: dict) -> tuple[float, list[str]]:
    """Penalize candidates whose model variant contradicts the inquiry.

    Returns a score penalty and non-blocking warnings.  For example a digital
    (数显) inquiry is penalized when matched against a plain caliper, and a
    500g 托盘天平 is penalized against 200g candidates.
    """
    line_text = f"{line.get('name', '')} {line.get('spec', '')}"
    record_text = f"{record.get('name', '')} {record.get('spec', '')}"
    line_feat = _spec_features(line_text)
    record_feat = _spec_features(record_text)
    penalty = 0.0
    warnings: list[str] = []

    if line_feat["half"] and not record_feat["half"]:
        penalty += 6
        warnings.append("规格变体不符：询价为四位半/数字显示精度，候选无对应精度描述")
    if line_feat["digital"] and not record_feat["digital"]:
        penalty += 9
        warnings.append("规格变体不符：询价要求数显/数字式，候选非数显")
    if line_feat["analog"] and record_feat["digital"]:
        penalty += 9
        warnings.append("规格变体不符：询价要求指针式，候选为数字式")

    # P0: 范围比较（优先于单值比较）
    line_ranges = spec_ranges(line_text)
    record_ranges = spec_ranges(record_text)

    # P5: 三维尺寸有序比较（优先于范围比较）
    line_dims = line_ranges.get("dims")
    record_dims = record_ranges.get("dims")
    if line_dims and record_dims:
        if len(line_dims) == len(record_dims):
            # 逐维比较，允许一定误差（15%）
            max_rel_diff = 0.0
            for lv, rv in zip(line_dims, record_dims):
                if lv > 0:
                    rel_diff = abs(rv - lv) / lv
                    max_rel_diff = max(max_rel_diff, rel_diff)
            if max_rel_diff > 0.15:  # 任一维度差>15%视为不同
                penalty += 8
                warnings.append(
                    f"规格尺寸不符：询价{'×'.join(f'{v:g}' for v in line_dims)}mm，"
                    f"候选{'×'.join(f'{v:g}' for v in record_dims)}mm"
                )
        else:
            # 维度数不同（2D vs 3D）
            penalty += 8
            warnings.append(
                f"规格尺寸不符：询价{'×'.join(f'{v:g}' for v in line_dims)}mm，"
                f"候选{'×'.join(f'{v:g}' for v in record_dims)}mm"
            )

    for unit in ("mm", "ml", "g", "a", "v", "w", "℃"):
        line_range = line_ranges.get(unit)
        record_range = record_ranges.get(unit)
        if line_range and record_range:
            # 范围重叠度检查：如果两个范围有显著重叠（>80%），视为相同
            overlap = max(0, min(line_range[1], record_range[1]) - max(line_range[0], record_range[0]))
            total = max(line_range[1], record_range[1]) - min(line_range[0], record_range[0])
            if total == 0:
                # 退化区间 (v,v) 对 (v,v)：单值相等不罚
                # （修复 "询价500~500ml vs 候选500~500ml" 的假量程警告）
                continue
            if overlap / total > 0.8:
                continue  # 范围重叠，不罚分
            # 范围不重叠，罚分
            penalty += 8
            warnings.append(
                f"规格量程不符：询价{line_range[0]:g}~{line_range[1]:g}{unit}，"
                f"候选{record_range[0]:g}~{record_range[1]:g}{unit}"
            )
        elif line_range and not record_range:
            # 询价有范围，候选只有单值
            line_value = line_feat["caps"].get(unit)
            record_value = record_feat["caps"].get(unit)
            if line_value and record_value and abs(record_value - line_value) / line_value > 0.3:
                penalty += 8
                warnings.append(f"规格量程不符：询价{line_value:g}{unit}，候选{record_value:g}{unit}")
        elif not line_range and record_range:
            # 询价只有单值，候选有范围
            line_value = line_feat["caps"].get(unit)
            record_value = record_feat["caps"].get(unit)
            if line_value and record_value and abs(record_value - line_value) / line_value > 0.3:
                penalty += 8
                warnings.append(f"规格量程不符：询价{line_value:g}{unit}，候选{record_value:g}{unit}")

    # 单值比较（无范围时回退到原有逻辑）
    # P5: 如果 dims 已匹配，跳过单值比较（避免 CAP_PATTERN 提取差异导致误判）
    dims_matched = (
        line_dims and record_dims
        and len(line_dims) == len(record_dims)
        and all(abs(a - b) / max(a, 1e-9) <= 0.15 for a, b in zip(line_dims, record_dims))
    )
    if not dims_matched:
        for unit in ("g", "ml", "a", "v", "w", "mm"):
            line_value = line_feat["caps"].get(unit)
            record_value = record_feat["caps"].get(unit)
            if line_value and record_value and abs(record_value - line_value) / line_value > 0.3:
                penalty += 8
                warnings.append(f"规格量程不符：询价{line_value:g}{unit}，候选{record_value:g}{unit}")

    if line_feat["mf"] and record_feat["mf"] and set(line_feat["mf"]) != set(record_feat["mf"]):
        penalty += 8
        warnings.append("规格型号不符：" + "、".join(sorted(line_feat["mf"])))

    # 教学磁钢型号：D-CG-LT-180（条形） vs D-CG-LU-80（蹄形） 等型号冲突。
    # 询价明确型号而候选无型号（学生用小磁铁）同样罚——型号是规格硬约束。
    if line_feat["magnet"] and (
        not record_feat["magnet"] or set(line_feat["magnet"]) != set(record_feat["magnet"])
    ):
        penalty += 8
        warnings.append("磁钢型号不符：" + "、".join(sorted(line_feat["magnet"])))

    # 学生用↔教学用 定位冲突：询价"学生用"（小规格）匹配到"教学用"（大规格）
    # 或反之，罚 8 分（蹄形磁铁 学生用2元 vs 教学用13元 因此被区分）。
    if line_feat["student"] and record_feat["teaching"]:
        penalty += 8
        warnings.append("规格定位不符：询价学生用，候选为教学用/教师用")
    if line_feat["teaching"] and record_feat["student"]:
        penalty += 8
        warnings.append("规格定位不符：询价教学用，候选为学生用")

    # P2: 形状不符罚分（U型 vs 单球、方形 vs 圆形）
    if line_feat["shape"] and record_feat["shape"] and line_feat["shape"] != record_feat["shape"]:
        penalty += 8
        warnings.append(f"规格形状不符：询价{line_feat['shape']}，候选{record_feat['shape']}")

    # P2: 材质不符罚分（高硼硅 vs 钠钙、不锈钢 vs 铁）
    # 材质差异非阻断（降分但允许），因为不同材质可能有相同功能
    if line_feat["material"] and record_feat["material"] and line_feat["material"] != record_feat["material"]:
        penalty += 4
        warnings.append(f"规格材质不符：询价{line_feat['material']}，候选{record_feat['material']}")

    # P2: 放大倍数不符罚分（200× vs 500×）
    if line_feat["magnification"] and record_feat["magnification"] and line_feat["magnification"] != record_feat["magnification"]:
        penalty += 8
        warnings.append(f"规格倍数不符：询价{line_feat['magnification']}，候选{record_feat['magnification']}")

    # P1: 套件件数不符罚分（解剖器 7件 vs 4件）——双方都提取到件数且不等才罚
    if line_feat["pcs"] and record_feat["pcs"] and line_feat["pcs"] != record_feat["pcs"]:
        penalty += 8
        warnings.append(
            f"规格件数不符：询价{line_feat['pcs']:g}件，候选{record_feat['pcs']:g}件"
        )

    # 修饰词变体罚：把修饰词从两个名字都剥掉后核心相同、但修饰词集合不同，
    # 视为不同产品（演示斜面小车≠斜面小车、数显电流表≠指针电流表）。
    # 例外：演示器↔实验器、包埋↔浸制 互变视为等价（摩擦力演示器=摩擦力实验器、
    # 蟾蜍包埋标本=蟾蜍浸制标本——同一标本的不同保存方式）。
    line_core_full = name_core(line_text)
    record_core_full = name_core(record_text)
    line_stripped = MODIFIER_PENALTY_PAT.sub("", line_core_full)
    record_stripped = MODIFIER_PENALTY_PAT.sub("", record_core_full)
    if line_stripped and record_stripped and line_stripped == record_stripped:
        line_mods = set(MODIFIER_PENALTY_PAT.findall(line_core_full))
        record_mods = set(MODIFIER_PENALTY_PAT.findall(record_core_full))
        if line_mods != record_mods:
            shi_yan_swap = (
                (line_core_full.endswith("演示器") and record_core_full.endswith("实验器"))
                or (line_core_full.endswith("实验器") and record_core_full.endswith("演示器"))
            )
            bao_mai_swap = (
                ("包埋" in line_core_full and "浸制" in record_core_full)
                or ("浸制" in line_core_full and "包埋" in record_core_full)
            )
            if not shi_yan_swap and not bao_mai_swap:
                penalty += 12
                warnings.append(
                    "修饰词变体不符："
                    f"询价含[{'/'.join(sorted(line_mods)) or '∅'}]，"
                    f"候选含[{'/'.join(sorted(record_mods)) or '∅'}]"
                )

    return penalty, warnings


def requirement_warnings(record: dict, requirements: list[dict]) -> list[str]:
    warnings: list[str] = []
    combined = normalize_text(
        f"{record.get('spec', '')}{record.get('model', '')}{record.get('brand', '')}"
    )
    raw_combined = unicodedata.normalize(
        "NFKC", f"{record.get('spec', '')} {record.get('model', '')}"
    ).lower()
    for requirement in requirements:
        value = str(requirement.get("value", "")).strip()
        unit = str(requirement.get("unit", "")).strip()
        operator = str(requirement.get("operator", "contains"))
        satisfied = normalize_text(value) in combined if value else True
        numeric_match = re.fullmatch(r"-?\d+(?:\.\d+)?", value)
        if numeric_match and unit:
            target_value = float(value)
            found = [
                float(item)
                for item in re.findall(
                    rf"(-?\d+(?:\.\d+)?)\s*{re.escape(unit.lower())}", raw_combined
                )
            ]
            if operator in (">=", "≥"):
                satisfied = any(item >= target_value for item in found)
            elif operator in ("<=", "≤"):
                satisfied = any(item <= target_value for item in found)
            elif operator in ("=", "equals"):
                satisfied = any(math.isclose(item, target_value, rel_tol=0.01) for item in found)
        if not satisfied:
            prefix = "BLOCK: " if requirement.get("required", True) else ""
            label = requirement.get("attribute_name", "特殊要求")
            warnings.append(f"{prefix}未满足客户要求：{label} {operator} {value}{unit}")
    return warnings


def score_record(line: dict, record: dict, requirements: list[dict] | None = None) -> Candidate:
    requirements = requirements or []
    exact_code = bool(
        normalize_text(line.get("product_code"))
        and normalize_text(line.get("product_code"))
        == normalize_text(record.get("product_code"))
    )
    # 名称完全一致（含 演示器↔实验器 等价归一）：同名候选给决定性加分，
    # 防止"部分同名但长规格文本巧合重叠"的候选压过真正同名同产品的候选
    # （如 立体磁感线演示器 被 磁感线演示器 的长 spec 抢占 rank1）。
    line_core_full = name_core(line.get("name", ""))
    record_core_full = name_core(record.get("name", ""))
    name_exact = bool(
        line_core_full and line_core_full == record_core_full
    ) or (
        line_core_full.endswith("演示器") and record_core_full.endswith("实验器")
        and MODIFIER_PENALTY_PAT.sub("", line_core_full) == MODIFIER_PENALTY_PAT.sub("", record_core_full)
    ) or (
        line_core_full.endswith("实验器") and record_core_full.endswith("演示器")
        and MODIFIER_PENALTY_PAT.sub("", line_core_full) == MODIFIER_PENALTY_PAT.sub("", record_core_full)
    )
    # 类目兼容：JY 大类或名称类目不符时给重罚（不进BLOCK、但显著降分）。
    category_ok, category_reason = category_compatible(
        line.get("product_code"), record.get("product_code"),
        line.get("name"), record.get("name"),
    )
    name_similarity = bigram_dice(name_core(line.get("name", "")), name_core(record.get("name", "")))
    spec_similarity = parameter_similarity(line.get("spec", ""), record.get("spec", ""))
    model_similarity = _similarity(line.get("model", ""), record.get("model", ""))
    brand_similarity = _similarity(line.get("brand", ""), record.get("brand", ""))
    maker_similarity = _similarity(
        line.get("manufacturer", ""), record.get("manufacturer", "")
    )
    unit_result = compare_units(
        str(line.get("unit", "")),
        str(record.get("unit", "")),
        float(record.get("price", 0)),
        str(line.get("spec", "")),
        str(record.get("spec", "")),
    )
    quality_fields = ["spec", "model", "manufacturer", "unit", "quote_date", "source_file"]
    data_quality = sum(bool(record.get(field)) for field in quality_fields) / len(quality_fields)

    component_scores = {
        # 名称完全一致时参数分保底 40%（约16分）：询价 spec 短/缺失不能把
        # 精确同名产品压到选不中（如 摩擦力演示器 同名候选只有30分被判unmatched）。
        # 但候选自身无 spec 时不保底——无规格信息的记录无法证明量程吻合，
        # 保底会让 直尺5.0(无spec) 压过 直尺6.0(演示用1m塑料米尺)。
        # exact_code 候选不保底：其得分固定 100 基础分、不依赖分项，
        # 保底只会抹平同码不同规格在排序键上的参数差异（分子结构模型
        # 演示用/分组用/初中用 参数分量全被压成 16）。
        "参数": round(
            (spec_similarity if exact_code or not (name_exact and record.get("spec")) else max(spec_similarity, 0.4)) * 40,
            2,
        ),
        "型号": round(model_similarity * 13, 2),
        "名称": round(name_similarity * 13, 2),
        "制造商/品牌": round(((maker_similarity + brand_similarity) / 2) * 9, 2),
        "单位": round(unit_result.score * 9, 2),
        "数据质量": round(data_quality * 4, 2),
        # 新增：类目一致才给满 6 分，否则 0；价格一致性在 match_line 阶段注入
        "类目": 6.0 if category_ok else 0.0,
        "价格一致性": 0.0,  # 由 match_line 在聚合所有候选后回写
    }
    score = 100.0 if exact_code else round(sum(component_scores.values()), 2)
    reasons: list[str] = []
    if exact_code:
        reasons.append("产品编码精确一致")
    if name_exact and not exact_code:
        score += 5.0
        reasons.append("名称完全一致")
    # 学生用/教学用 定位一致加分：询价"学生用"匹配到"学生用"候选 +3 分，
    # 让 2.0元学生用蹄形磁铁 压过 14元无定位标记版（初中物理优先规则之外）。
    line_feat_loc = _spec_features(f"{line.get('name', '')} {line.get('spec', '')}")
    record_feat_loc = _spec_features(f"{record.get('name', '')} {record.get('spec', '')}")
    if line_feat_loc["student"] and record_feat_loc["student"]:
        score += 3.0
        reasons.append("学生用定位一致")
    if line_feat_loc["teaching"] and record_feat_loc["teaching"]:
        score += 3.0
        reasons.append("教学用定位一致")
    if name_similarity == 1:
        reasons.append("名称一致")
    elif name_similarity >= 0.6:
        reasons.append("名称相似")
    if spec_similarity >= 0.85:
        reasons.append("参数高度一致")
    elif spec_similarity >= 0.45:
        reasons.append("参数部分一致")
    if model_similarity >= 0.9 and line.get("model"):
        reasons.append("型号一致")
    if unit_result.status == "exact":
        reasons.append("单位一致")
    if category_ok:
        reasons.append("类目一致")
    warnings = []
    if unit_result.warning:
        warnings.append(unit_result.warning)
    if not category_ok and not exact_code:
        warnings.append(f"BLOCK: {category_reason}")
    warnings.extend(requirement_warnings(record, requirements))
    penalty, variant_warnings = variant_penalty(line, record)
    if not category_ok and not exact_code:
        penalty += 20  # 类目不符额外重罚（区别于规格变体）
    warnings.extend(variant_warnings)
    numeric_bonus = numeric_spec_bonus(line, record)
    # exact_code 保持 100 基础分与候选锁池，但规格变体罚分与数值加分照常执行：
    # 同码不同规格的记录由此拉开分差（注射器 10/50/100mL 不再被压成同分同价）。
    score = round(max(0.0, score - penalty) + numeric_bonus, 2)
    if numeric_bonus >= 4:
        reasons.append("规格数值吻合")
    confidence = confidence_for(score, warnings)
    return Candidate(
        record=record,
        score=score,
        confidence=confidence,
        component_scores=component_scores,
        reasons=reasons,
        warnings=warnings,
        unit_status=unit_result.status,
        normalized_price=unit_result.normalized_price,
    )


def match_line(
    line: dict,
    records: list[dict],
    requirements: list[dict] | None = None,
    limit: int = 30,
) -> list[Candidate]:
    normalized_name = name_core(line.get("name", ""))
    normalized_code = normalize_text(line.get("product_code", ""))
    # 候选池：先用类目硬过滤大幅缩小范围（类目判不出的仍保留）
    category_pool = same_category_pool(line, records)
    if len(category_pool) < 8:  # 类目过滤太狠导致候选不足时退回全库
        category_pool = records
    exact_code_records = [
        record
        for record in category_pool
        if normalized_code
        and normalize_text(record.get("product_code", "")) == normalized_code
    ]
    exact_name_records = [
        record for record in category_pool if name_core(record.get("name", "")) == normalized_name
    ]
    # 预过滤：先算一次 name_core + bigram（缓存命中），只对高相关记录做
    # 完整排序（parameter_similarity 成本高）。bigram 阈值 0.15 保守——
    # 候选池后续还有 0.3 门槛，不会漏掉真实候选。
    line_core = normalized_name
    line_spec = str(line.get("spec", ""))
    prefiltered = [
        record
        for record in category_pool
        if bigram_dice(line_core, name_core(record.get("name", ""))) >= 0.15
    ]
    scored_pool = sorted(
        prefiltered,
        key=lambda record: max(
            bigram_dice(line_core, name_core(record.get("name", ""))),
            parameter_similarity(line_spec, record.get("spec", "")) * 0.7,
        ),
        reverse=True,
    )[:250]
    if exact_code_records:
        # 同码名称兼容（如 编号=直尺 命中 直尺）时维持“同码优先”缩小候选池；
        # 同码名称不符（跨价目本编号体系冲突，预算清单 01013=计算器 vs
        # 赛特尔 01013=图形计算器）时编号不可信，与名称/参数候选合并评分。
        from .services import name_allows_autoselect  # 延迟导入避免循环依赖

        code_compatible = any(
            name_allows_autoselect(
                line.get("name", ""), record.get("name", ""), record.get("source_file")
            )
            for record in exact_code_records
        )
        if code_compatible or not exact_name_records:
            pool = exact_code_records
        else:
            pool = list(exact_code_records)
            seen_ids = {record.get("id") for record in pool}
            for record in exact_name_records + scored_pool:
                if record.get("id") not in seen_ids:
                    pool.append(record)
                    seen_ids.add(record.get("id"))
    elif exact_name_records:
        # 精确同名绝不独占候选池：与通用名高分候选合并，让不同来源的
        # 真实报价（如 数显游标卡尺 55/120）也能进入候选。
        seen_ids = {record.get("id") for record in exact_name_records}
        pool = exact_name_records + [record for record in scored_pool if record.get("id") not in seen_ids][:250]
    else:
        pool = scored_pool
    candidates = [score_record(line, record, requirements) for record in pool]

    # 价格一致性分项：基于同核心词组的稳健中位数（只统计类目一致、量程一致的）。
    # 偏离 >2x 扣 8 分、<0.5x 扣 8 分；0.8x~1.25x 给满 6 分。
    # 量程隔离：500mm直尺 不参与 1000mm直尺 的价格基准（否则 22元钢直尺
    # 会被 4.5元 500mm直尺 拉出 >2x 判 0 分，永远排不上）。
    line_core = normalized_name
    line_feat = _spec_features(f"{line.get('name', '')} {line.get('spec', '')}")

    def _same_range(record: dict) -> bool:
        record_feat = _spec_features(f"{record.get('name', '')} {record.get('spec', '')}")
        for unit in ("mm", "ml", "g", "a", "v", "w"):
            lv = line_feat["caps"].get(unit)
            rv = record_feat["caps"].get(unit)
            if lv and rv and (rv / lv > 1.5 or rv / lv < 1 / 1.5):
                return False
        return True

    peer_prices = []
    for item in candidates:
        price_val = float(item.record.get("price", 0))
        if (
            price_val
            and bigram_dice(line_core, name_core(item.record.get("name", ""))) >= 0.3
            and _same_range(item.record)
        ):
            peer_prices.append(price_val)
    if len(peer_prices) >= 3:
        sorted_prices = sorted(peer_prices)
        truncated = sorted_prices[: max(3, (len(sorted_prices) * 3) // 4)]
        peer_median = median(truncated) if truncated else 0.0
    else:
        peer_median = 0.0

    for item in candidates:
        price_val = float(item.record.get("price", 0))
        component = 6.0
        if peer_median and price_val:
            ratio = price_val / peer_median
            if ratio > 2.0 or ratio < 0.5:
                component = 0.0
            elif ratio > 1.5 or ratio < 0.67:
                component = 2.0
            elif ratio > 1.25 or ratio < 0.8:
                component = 4.0
        # score_record 打分时分项"价格一致性"=0 且 penalty 已扣；
        # 这里把回写后的价格一致性分项直接加上即可。
        item.component_scores["价格一致性"] = component
        # 时间衰减：近 12 个月 +1.5 分、>36 个月 -0.5 分（小分、不掩盖类目/参数差）
        recency = quote_recency_bonus(item.record.get("quote_date", ""))
        if recency:
            item.component_scores["时效"] = recency
        else:
            item.component_scores["时效"] = 0.0
        item.score = round(item.score + component + recency, 2)
    candidates.sort(
        key=lambda item: (
            item.score,
            item.component_scores.get("参数", 0),
            item.record.get("source_priority", 0),
        ),
        reverse=True,
    )
    prices = [
        float(item.record.get("price", 0))
        for item in candidates
        if item.record.get("price") and not any(
            str(warning).startswith("规格变体") for warning in item.warnings
        )
    ]
    mid = median(prices) if prices else 0
    for item in candidates:
        price = float(item.record.get("price", 0))
        if mid and (price > mid * 3 or price < mid / 3):
            item.warnings.append(f"价格偏离同组中位数（¥{mid:g}）")
    return candidates[:limit]


def distinct_manufacturer_options(candidates: list[Candidate], count: int, min_score: float = RELIABLE_THRESHOLD) -> list[Candidate]:
    selected: list[Candidate] = []
    seen: set[str] = set()
    for candidate in candidates:
        if candidate.score < min_score:
            continue
        record = candidate.record
        identity = normalize_text(record.get("manufacturer") or record.get("brand"))
        if not identity:
            identity = f"unknown:{record.get('id')}"
        if identity in seen:
            continue
        seen.add(identity)
        selected.append(candidate)
        if len(selected) >= count:
            break
    return selected


def warmup_match_caches(records: list[dict]) -> None:
    """冷启动预热：把历史库每条记录的轻量元数据（name_core / 类目推断 /
    归一化）一次性算进 LRU 缓存。

    任务开始时调用一次，后续所有行的候选池预过滤（bigram_dice 排序前的
    0.15 阈值筛）全部缓存命中。注意：不预热 _spec_features——它对长 spec
    的正则解析成本高，且匹配阶段只对候选池（每行 ≤250 条）按需计算，
    全量预热反而拖慢冷启动。
    """
    for record in records:
        name = record.get("name", "")
        spec = record.get("spec", "")
        code = record.get("product_code", "")
        normalize_text(name)
        normalize_text(spec)
        normalize_text(code)
        name_core(name)
        grade_marker(name)
        infer_category_from_name(name)
        jy_major(str(code or ""))
