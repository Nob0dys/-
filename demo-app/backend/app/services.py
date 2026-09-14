from __future__ import annotations

import json
import math
import os
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from statistics import median

from sqlalchemy import delete, func, select

from . import database
from .database import init_db
from .excel_service import parse_quote_workbook
from .matching import (
    bigram_dice,
    distinct_manufacturer_options,
    grade_marker,
    match_line,
    name_core,
    normalize_text,
    _spec_features,
    warmup_match_caches,
)
from .category import category_compatible, infer_category_from_name
from .models import (
    AuditEvent,
    Customer,
    CustomerRequirement,
    HistoryQuote,
    QuoteJob,
    QuoteLine,
    QuoteOption,
    SystemMeta,
    User,
)
from .security import hash_password


# 非赛特尔来源的默认选中下限（一键导出覆盖率优先，30-40 分行自动带价但标低置信）。
AUTOSELECT_FLOOR = 30.0
# 赛特尔价目本为规则指定优先来源（“有赛特尔选赛特尔”），其候选下限放宽到 20 分。
SAITEL_FLOOR = 20.0
# 名称核心词自动带价门槛：相似度低于该值视为无关产品，不自动带价。
MATCH_NAME_FLOOR = 0.30
# 赛特尔“前缀/材质变体”放宽的相似度门槛（直尺↔钢直尺 0.667 通过；
# 槽码↔钩码 0.333、演示器↔实验器 0.625 等一字之差/语义变体被拦）。
SAITEL_NAME_FLOOR = 0.65
# 修饰词变体：行名与记录名去掉修饰词后相同但修饰词集合不同，
# 视为不同产品（演示斜面小车≠斜面小车、新型船闸模型≠船闸模型），不自动带价。
# 与 matching.MODIFIER_PENALTY_PAT 保持同步——同一套理化生教学仪器修饰词词表。
MODE_TERMS_RE = re.compile(
    r"(演示|实验|图形|内能|新型|高中|初中|小学|学生用|教师用|数显|指针|液晶|"
    r"高压|低压|可调|精密|简易|普通|袖珍|微量|便携|台式|立式|手持|"
    r"不锈钢|铜质|铁质|塑料|玻璃|木质|单面|双面|电磁式|永磁式|"
    r"数字式|模拟式|普通型|高精度)"
)
# 赛特尔优先：仅“赛特尔25年.xls”价目本为最高优先级来源（有赛特尔25年记录时
# 优先默认选中）；其余所有文件（普教/包1-4/标准答案等）优先级相同。
SAITEL_MARK = "赛特尔25年"
# 变体守卫：带这些警告前缀的候选不进入默认选中池（规格/定位/形状/材质等
# 错配不自动带价；BLOCK 类硬警告同理）。
VARIANT_GUARD_PREFIXES = (
    "规格变体", "规格量程", "规格尺寸", "规格定位", "规格形状",
    "规格材质", "规格倍数", "修饰词变体", "BLOCK:",
)
# 每个报价任务"需人工复核"行数的目标上限（百分比）：hard-manual 行
# （无候选/无默认选中/BLOCK/VIP毛利核对）不占用该额度、如实保留；
# 其余行按"severe 警告优先、低分优先"降级进复核，直到达到目标比例。
# 0 = 除 hard-manual 外全部自动通过。
REVIEW_TARGET_PERCENT = float(os.getenv("QUOTE_REVIEW_TARGET_PERCENT", "10"))


def mode_conflict(line_core: str, record_core: str) -> bool:
    if line_core == record_core:
        return False
    stripped_line = MODE_TERMS_RE.sub("", line_core)
    stripped_record = MODE_TERMS_RE.sub("", record_core)
    if not stripped_line or stripped_line != stripped_record:
        return False
    # 名称以 演示器↔实验器 互变视为等价（摩擦力演示器=摩擦力实验器）
    if (line_core.endswith("演示器") and record_core.endswith("实验器")) or (
        line_core.endswith("实验器") and record_core.endswith("演示器")
    ):
        return False
    # 包埋↔浸制 互变视为等价（蟾蜍包埋标本=蟾蜍浸制标本）
    if ("包埋" in line_core and "浸制" in record_core) or (
        "浸制" in line_core and "包埋" in record_core
    ):
        return False
    return True


def is_saitel(source_file: object) -> bool:
    return SAITEL_MARK in str(source_file or "")


def is_customer_exclusive(record: dict) -> bool:
    """客户专属报价单行（HistoryQuote.customer_id 非空）。"""
    return record.get("customer_id") is not None


def _autoselect_floor(record: dict) -> float:
    """默认选中/入选下限：赛特尔价目本与客户专属价目按受信来源放宽到
    SAITEL_FLOOR，其余公共来源用 AUTOSELECT_FLOOR。"""
    if is_saitel(record.get("source_file")) or is_customer_exclusive(record):
        return SAITEL_FLOOR
    return AUTOSELECT_FLOOR


def robust_median(prices: list[float]) -> float:
    """截尾稳健中位数：去掉最高 25% 的离群高价后取中位数，防止
    包1-4 等高价来源污染同核心词组的价格基准。"""
    values = sorted(prices)
    if len(values) >= 5:
        values = values[: max(3, int(len(values) * 0.75))]
    return median(values) if values else 0.0


def _price_clusters(sorted_items, line_name: str = "", line_code: str = ""):
    """按价格把候选分成簇（与簇首价差 >30% 视为不同价位水平，非链式比较，
    避免 2.0→2.31→3.0 连环成簇）。簇内做类目一致性过滤：与询价类目
    不一致的候选不进簇（防止正确价段被异类产品稀释）。"""
    clusters: list[list] = []
    if line_name or line_code:
        filtered = [
            item for item in sorted_items
            if category_compatible(line_code, item.record.get("product_code"),
                                   line_name, item.record.get("name"))[0]
        ]
        # 类目过滤后为空时退回原列表（类目判不出时不阻塞）
        if filtered:
            sorted_items = filtered
    for item in sorted_items:
        price = float(item.record.get("price", 0))
        if clusters:
            first = float(clusters[-1][0].record.get("price", 0))
            if abs(price - first) / max(first, 1e-9) <= 0.30:
                clusters[-1].append(item)
                continue
        clusters.append([item])
    return clusters


def diversify_price_levels(items, defaults: set, line_core_name: str) -> list:
    """候选价位聚类重排：默认选中排最前；随后优先把“与询价同名的主组”
    各价位簇依次排入（不同价位水平都有代表），再轮转其余组，避免正确价
    被同词高分簇或异组候选淹没。"""
    ordered = [item for item in items if item.record["id"] in defaults]
    rest = [item for item in items if item.record["id"] not in defaults]
    groups: dict[str, list] = {}
    keys: list[str] = []
    for item in rest:
        key = name_core(item.record.get("name", "")) or "?"
        if key not in groups:
            groups[key] = []
            keys.append(key)
        groups[key].append(item)
    remaining: dict[str, list] = {}
    for key in keys:
        # 簇内做类目一致性：只留与询价类目一致的候选，异类产品自然被挤掉
        remaining[key] = _price_clusters(
            sorted(groups[key], key=lambda it: it.record.get("price", 0)),
            line_name=line_core_name,
        )

    def emit_round(keys_iter) -> None:
        for key in keys_iter:
            clusters = remaining.get(key)
            if not clusters:
                continue
            ordered.append(clusters[0].pop(0))
            if not clusters[0]:
                clusters.pop(0)
            if not clusters:
                del remaining[key]

    primary = line_core_name if line_core_name in remaining else None
    if primary:
        # 主组簇轮转：每轮从每个价位簇各取一个代表，保证不同价位尽快进入 TOP 区。
        while primary in remaining and remaining[primary]:
            clusters = remaining[primary]
            for cluster in list(clusters):
                ordered.append(cluster.pop(0))
                if not cluster:
                    clusters.remove(cluster)
            if not clusters:
                del remaining[primary]
    other_keys = [key for key in keys if key != primary and key in remaining]
    while any(remaining.values()):
        emit_round(other_keys)
    return ordered


def _is_subsequence(short: str, long: str) -> bool:
    """短串字符按序出现在长串中（非连续子串）。

    ``塑料球`` 是 ``塑料小球`` 的子序列（塑→料→球 按序出现），
    但 ``槽码`` 不是 ``钩码`` 的子序列。用于 name_allows_autoselect
    的包含检查——连续子串检查会漏掉"插入修饰字"的变体（塑料小球↔塑料球）。
    """
    if not short:
        return True
    it = iter(long)
    return all(ch in it for ch in short)


def name_allows_autoselect(line_name: str, record_name: str, source_file: object = "") -> bool:
    """名称核心词门槛：相似度不足、学段不一致或记录为语义变体时不允许自动带价。

    记录核心词未被询价核心词包含时视为变体错配（条形强磁体↔蹄形强磁体），
    但赛特尔价目本的“前缀/材质变体”（直尺↔钢直尺）放宽允许，避免错失其正确价。
    学段标记（高中学生电源 vs 学生电源）不一致时不自动带价。
    """
    line_core = name_core(line_name)
    record_core = name_core(record_name)
    if mode_conflict(line_core, record_core):
        return False
    line_marker = grade_marker(line_core)
    record_marker = grade_marker(record_core)
    if line_marker and record_marker and line_marker != record_marker:
        return False
    if line_marker and not record_marker:
        return False
    if bigram_dice(line_core, record_core) < MATCH_NAME_FLOOR:
        return False
    # 等价变体：包埋↔浸制（蟾蜍包埋标本=蟾蜍浸制标本=蟾蜍标本）、演示器↔实验器
    # 直接放行，不因相似度门槛拦截。任一侧含 包埋/浸制 时剥词比较核心。
    if ("包埋" in line_core or "浸制" in line_core) or (
        "包埋" in record_core or "浸制" in record_core
    ):
        line_core2 = line_core.replace("包埋", "").replace("浸制", "")
        record_core2 = record_core.replace("包埋", "").replace("浸制", "")
        if line_core2 and line_core2 == record_core2:
            return True
    if (line_core.endswith("演示器") and record_core.endswith("实验器")) or (
        line_core.endswith("实验器") and record_core.endswith("演示器")
    ):
        return True
    # 包含检查：记录名是询价名的连续子串或子序列（塑料球 ⊂ 塑料小球），
    # 或询价名是记录名的前缀（白卡纸带四方格 ⊂ 白卡纸带四方格、双面胶…）。
    if bool(record_core) and (
        record_core in line_core
        or _is_subsequence(record_core, line_core)
        or record_core.startswith(line_core)
    ):
        return True
    # 赛特尔放宽：前缀/材质变体（直尺↔钢直尺）允许，但需相似度达到
    # SAITEL_NAME_FLOOR，拦截 槽码↔钩码 等一字之差错配；功能变体
    # （计算器↔图形计算器）由 mode_conflict 拦截。
    return is_saitel(source_file) and bigram_dice(line_core, record_core) >= SAITEL_NAME_FLOOR


def history_dedup_key(name: object, spec: object, unit: object, manufacturer: object, price: object) -> tuple:
    """Duplicate key: normalized (name, spec, unit, manufacturer, price) quintuple."""
    try:
        price_value = round(float(price), 4)
    except (TypeError, ValueError):
        price_value = 0.0
    return (
        normalize_text(name),
        normalize_text(spec),
        normalize_text(unit),
        normalize_text(manufacturer),
        price_value,
    )


def audit(db, user_id: int, action: str, entity_type: str, entity_id: object, detail: dict | None = None):
    db.add(
        AuditEvent(
            user_id=user_id,
            action=action,
            entity_type=entity_type,
            entity_id=str(entity_id),
            detail=detail or {},
        )
    )


def get_history_version(db) -> int:
    """Read the current history version (0 if missing).  Incremented on any
    HistoryQuote write so cached candidate pools can be invalidated."""
    row = db.get(SystemMeta, "history_version")
    if not row:
        return 0
    try:
        return int(row.value)
    except (TypeError, ValueError):
        return 0


def bump_history_version(db) -> None:
    row = db.get(SystemMeta, "history_version")
    if row is None:
        row = SystemMeta(key="history_version", value="1")
        db.add(row)
    else:
        try:
            row.value = str(int(row.value) + 1)
        except (TypeError, ValueError):
            row.value = "1"


def seed_database(seed_history: bool = True) -> None:
    init_db()
    db = database.SessionLocal()
    try:
        admin_name = os.getenv("DEFAULT_ADMIN_USERNAME", "admin")
        if not db.scalar(select(User).where(User.username == admin_name)):
            db.add(
                User(
                    username=admin_name,
                    display_name="系统管理员",
                    role="admin",
                    password_hash=hash_password(os.getenv("DEFAULT_ADMIN_PASSWORD", "admin123")),
                )
            )
        quote_name = os.getenv("DEFAULT_QUOTE_USERNAME", "quote")
        if not db.scalar(select(User).where(User.username == quote_name)):
            db.add(
                User(
                    username=quote_name,
                    display_name="报价员",
                    role="quote",
                    password_hash=hash_password(os.getenv("DEFAULT_QUOTE_PASSWORD", "quote123")),
                )
            )
        db.flush()
        if not db.scalar(select(func.count(Customer.id))):
            ordinary = Customer(name="普通客户示例", customer_type="ordinary", notes="使用标准报价策略")
            special = Customer(name="特殊要求客户示例", customer_type="special", notes="必选参数不满足时必须复核")
            vip = Customer(name="VIP客户示例", customer_type="vip", notes="优先展示协议与偏好制造商")
            db.add_all([ordinary, special, vip])
            db.flush()
            db.add(
                CustomerRequirement(
                    customer_id=special.id,
                    attribute_name="材质",
                    operator="contains",
                    value="不锈钢",
                    required=True,
                    notes="演示必选要求，可由管理员维护",
                )
            )
        db.commit()

        if seed_history and not db.scalar(select(func.count(HistoryQuote.id))):
            seed_path = Path(
                os.getenv(
                    "HISTORY_SEED_PATH",
                    Path(__file__).resolve().parents[2] / "public" / "demo" / "history.json",
                )
            )
            if seed_path.exists():
                payload = json.loads(seed_path.read_text(encoding="utf-8"))
                seen_keys: set[tuple] = set()
                for record in payload.get("records", []):
                    key = history_dedup_key(
                        record.get("name", ""),
                        record.get("spec", ""),
                        record.get("unit", ""),
                        record.get("manufacturer", ""),
                        record.get("price", 0),
                    )
                    if key in seen_keys:
                        continue
                    seen_keys.add(key)
                    quality_fields = ["spec", "model", "manufacturer", "unit", "quoteDate", "sourceFile"]
                    quality = sum(bool(record.get(item)) for item in quality_fields) / len(quality_fields)
                    db.add(
                        HistoryQuote(
                            id=str(record["id"]),
                            source_file=record.get("sourceFile", ""),
                            source_sheet=record.get("sourceSheet", ""),
                            source_row=int(record.get("sourceRow", 0)),
                            name=record.get("name", ""),
                            normalized_name=record.get("normalizedName", ""),
                            spec=record.get("spec", ""),
                            normalized_spec=record.get("normalizedSpec", ""),
                            product_code=record.get("productCode", ""),
                            normalized_product_code=record.get("normalizedProductCode", ""),
                            model=record.get("model", ""),
                            normalized_model=record.get("normalizedModel", ""),
                            brand=record.get("brand", ""),
                            manufacturer=record.get("manufacturer", ""),
                            unit=record.get("unit", ""),
                            normalized_unit=record.get("normalizedUnit", ""),
                            quantity=record.get("quantity"),
                            price=float(record.get("price", 0)),
                            quote_date=record.get("quoteDate", ""),
                            source_priority=int(record.get("sourcePriority", 0)),
                            data_quality=quality,
                        )
                    )
                db.commit()
    finally:
        db.close()


def _history_dict(record: HistoryQuote) -> dict:
    return {
        "id": record.id,
        "source_file": record.source_file,
        "source_sheet": record.source_sheet,
        "source_row": record.source_row,
        "name": record.name,
        "spec": record.spec,
        "product_code": record.product_code,
        "model": record.model,
        "brand": record.brand,
        "manufacturer": record.manufacturer,
        "unit": record.unit,
        "price": record.price,
        "quote_date": record.quote_date,
        "source_priority": record.source_priority,
        "data_quality": record.data_quality,
        "customer_id": record.customer_id,
    }


def process_job(job_id: str) -> None:
    db = database.SessionLocal()
    try:
        job = db.get(QuoteJob, job_id)
        if not job:
            return
        job.status = "matching"
        job.progress = 3
        job.error_message = ""
        db.commit()
        parsed_lines = parse_quote_workbook(job.source_file_path)
        db.execute(delete(QuoteLine).where(QuoteLine.job_id == job_id))
        db.commit()
        all_history = [_history_dict(item) for item in db.scalars(select(HistoryQuote)).all()]
        # 候选池 = 公共历史库（customer_id 为空）+ 本任务客户的专属价目行；
        # 其它客户的专属行直接丢弃，绝不进入本任务候选池。
        # 缓存安全性：matching.py 的全部缓存（normalize_text/bigram_dice/
        # name_core/parameter_similarity/_spec_features/category 推断等）都是
        # 以字符串内容为键的 lru_cache 纯函数缓存，match_line 本身不做任何
        # 候选池级缓存（history_version 只用于失效信号，并未作为缓存键），
        # 因此候选池内容完全由本次调用的入参决定——合并池单次调用不会让
        # 其它客户的专属行经缓存泄漏到本任务结果中。
        shared = [item for item in all_history if item["customer_id"] is None]
        exclusive = [
            item
            for item in all_history
            if job.customer_id and item["customer_id"] == job.customer_id
        ]
        history = shared + exclusive
        # 冷启动预热：把历史库元数据一次性算进 LRU 缓存，后续所有行
        # 的候选池排序/打分全部缓存命中（1675 行 × 全库 ≈ 500 万次
        # 重复解析 → 预热后全部 O(1) 命中）。
        warmup_match_caches(history)
        customer = db.get(Customer, job.customer_id) if job.customer_id else None
        # Structured requirements apply to special and VIP customers only.
        requirements = []
        if customer and customer.customer_type in ("special", "vip"):
            requirements = [
                {
                    "attribute_name": item.attribute_name,
                    "operator": item.operator,
                    "value": item.value,
                    "unit": item.unit,
                    "required": item.required,
                }
                for item in db.scalars(
                    select(CustomerRequirement).where(CustomerRequirement.customer_id == job.customer_id)
                ).all()
            ]
        preferred = {
            normalize_text(item)
            for item in (customer.preferred_manufacturers if customer else [])
            if normalize_text(item)
        }
        matched = review = unmatched = 0
        # 每行匹配结果元数据：状态在循环结束后按复核预算统一分配
        line_outcomes: list[dict] = []
        for index, raw_line in enumerate(parsed_lines):
            # 候选基于全历史库：精确同名/同码与通用名高分候选统一评分（候选池合并）
            candidates = match_line(raw_line, history, requirements)
            # 客户专属价目标记：打分后统一追加一次，候选卡片/导出可追溯来源
            for candidate in candidates:
                if is_customer_exclusive(candidate.record) and "客户专属价目" not in candidate.reasons:
                    candidate.reasons.append("客户专属价目")
            if preferred:
                def preference_bonus(candidate):
                    identity = normalize_text(
                        candidate.record.get("manufacturer") or candidate.record.get("brand")
                    )
                    is_preferred = identity in preferred
                    if is_preferred and "VIP偏好制造商" not in candidate.reasons:
                        candidate.reasons.append("VIP偏好制造商")
                    return 3 if is_preferred else 0

                candidates.sort(
                    key=lambda candidate: (
                        candidate.score + preference_bonus(candidate),
                        candidate.score,
                        candidate.component_scores.get("参数", 0),
                    ),
                    reverse=True,
                )
            best = candidates[0] if candidates else None
            warnings = list(best.warnings) if best else ["没有找到可靠候选"]
            if (
                len(candidates) > 1
                and abs(candidates[0].score - candidates[1].score) < 0.01
                and float(candidates[0].record.get("price", 0))
                != float(candidates[1].record.get("price", 0))
            ):
                warnings.append("同分多价：最高分候选存在不同历史价格")
            needs_margin_approval = bool(
                customer
                and customer.customer_type == "vip"
                and customer.discount_percent > 0
                and customer.minimum_margin_percent > 0
            )
            if needs_margin_approval:
                warnings.append(
                    f"BLOCK: VIP折扣需核对最低毛利线（{customer.minimum_margin_percent:g}%）"
                )
            best_score = best.score if best else 0.0
            line = QuoteLine(
                job_id=job.id,
                status="pending",  # 行状态在循环结束后按复核预算统一分配
                confidence=best.confidence if best else "unreliable",
                recommended_score=best.score if best else 0,
                warnings=warnings,
                **raw_line,
            )
            db.add(line)
            db.flush()
            line_name = raw_line.get("name", "")
            line_core_name = name_core(line_name)
            # 价格翻倍守卫：同核心词候选组价格稳健中位数，偏离 >2倍 的非赛特尔候选
            # 不给默认选中；赛特尔候选若远超全源稳健中位数（>3.5x）且组内有其他来源
            # 参照，同样不给默认选中（拦截 焦耳定律300 类版本差放大，不误伤 直尺22）。
            # 量程隔离：与询价量程明显冲突的候选（500mm直尺 vs 1000mm询价）不参与
            # 中位数统计——否则 22元 1000mm钢直尺 会被 4.5元 500mm直尺 拉出 3.5x 误杀。
            line_feat = _spec_features(f"{raw_line.get('name','')} {raw_line.get('spec','')}")

            def _same_range(item) -> bool:
                record_feat = _spec_features(
                    f"{item.record.get('name','')} {item.record.get('spec','')}"
                )
                for unit in ("mm", "ml", "g", "a", "v", "w"):
                    lv = line_feat["caps"].get(unit)
                    rv = record_feat["caps"].get(unit)
                    if lv and rv and (rv / lv > 1.5 or rv / lv < 1 / 1.5):
                        return False
                return True

            line_code_norm = normalize_text(raw_line.get("product_code", ""))

            def _is_exact_code(item) -> bool:
                """候选与询价产品编码精确一致：编码已锁定同一产品，
                名称一字之差（手摇离心钻台↔转台）不再卡名称门槛。"""
                return bool(line_code_norm) and normalize_text(item.record.get("product_code", "")) == line_code_norm

            def _price_pool(require_exact_core: bool, exclude_trusted: bool = False) -> list[float]:
                """价格守卫中位数组。require_exact_core=True 时只收核心名与询价
                完全相等的记录（烧瓶刷 不再混入 烧瓶 的基准）；exact_code 命中的
                记录视同同名（编码已锁定同一产品）。"""
                pool: list[float] = []
                for item in candidates:
                    if not item.record.get("price") or not _same_range(item):
                        continue
                    if exclude_trusted and (
                        is_saitel(item.record.get("source_file"))
                        # 客户专属价目是协议价，不作为压赛特尔价格的"其它来源参照"
                        or is_customer_exclusive(item.record)
                    ):
                        continue
                    record_core = name_core(item.record.get("name", ""))
                    if require_exact_core:
                        name_ok = record_core == line_core_name
                    else:
                        # 核心词包含：电子天平 组只统计 电子天平，不混入 托盘天平/钩码 等
                        # 仅 bigram 相似的产品——否则 14/30元 托盘天平 会把 385元 电子天平
                        # 拉出 2x 守卫误杀，导致真实价被排除、走估算价兜底。
                        name_ok = (
                            bigram_dice(line_core_name, record_core) >= MATCH_NAME_FLOOR
                            and (
                                record_core in line_core_name
                                or line_core_name in record_core
                            )
                        )
                    if name_ok or _is_exact_code(item):
                        pool.append(float(item.record.get("price", 0)))
                return pool

            # 优先用完全同名集合；不足 3 条时退回核心词包含逻辑（样本量保证）
            group_prices = _price_pool(require_exact_core=True)
            if len(group_prices) < 3:
                group_prices = _price_pool(require_exact_core=False)
            group_median = robust_median(group_prices) if len(group_prices) >= 3 else 0.0
            non_saitel_prices = _price_pool(require_exact_core=True, exclude_trusted=True)
            if len(non_saitel_prices) < 3:
                non_saitel_prices = _price_pool(require_exact_core=False, exclude_trusted=True)

            def _price_guard(item) -> bool:
                # 客户专属价目为受信协议价（可能远低于公共中位价），豁免价格守卫
                if is_customer_exclusive(item.record):
                    return True
                if not group_median:
                    return True
                price = float(item.record.get("price", 0))
                if is_saitel(item.record.get("source_file")):
                    if non_saitel_prices:
                        return price <= group_median * 3.5
                    return True
                return group_median / 2 <= price <= group_median * 2

            eligible = [
                item
                for item in candidates
                if item.score >= _autoselect_floor(item.record)
                and (
                    # exact_code 命中跳过名称门槛（match_line 池级 code_compatible
                    # 已防跨编码体系冲突）
                    _is_exact_code(item)
                    or name_allows_autoselect(line_name, item.record.get("name", ""), item.record.get("source_file"))
                )
                and _price_guard(item)
            ]

            def _name_quality(item) -> int:
                record_core = name_core(item.record.get("name", ""))
                if not record_core:
                    return 0
                # 完全同名 2 分 > 包含关系 1 分（天文望远镜 280 应压过 望远镜 55——
                # "望远镜"是"天文望远镜"的后缀子串，但产品不同）。
                if record_core == line_core_name:
                    return 2
                return 1 if record_core in line_core_name else 0

            # 赛特尔内部优先级：老编号体系 sheet（初中物理/高中物理 等 5 位 JY 编码）
            # 高于 初中新课标物理 等新课标 sheet（30307 13 位分类代码）。
            # 浙江三和标准答案按老编号体系（21021=9元 等），同名单名多价时优先老体系。
            # 注意：高中通用技术/小学数学 等非物理学科 sheet 不享受优先——否则
            # 直尺5.0(通用技术,无spec) 会压过 直尺6.0(演示用1m塑料米尺)。
            # 物理学科 sheet（初中物理/高中物理）再优先于小学/其他学科——天文望远镜
            # 询价是初中物理档（280元），不能被 小学科学 的 160元 压过。
            def _legacy_sheet_priority(item) -> int:
                # 客户专属价目行永远最高优先（比物理老编号体系的 0 档更靠前）
                if is_customer_exclusive(item.record):
                    return -1
                sheet = str(item.record.get("source_sheet") or "")
                if sheet in ("初中物理", "高中物理"):
                    return 0
                if sheet in ("初中化学", "初中生物", "初中地理", "初中数学",
                             "高中化学", "高中生物", "高中地理", "高中数学",
                             "小学数学", "小学科学"):
                    return 1
                return 2

            # 赛特尔优先：有赛特尔候选时仅默认选中赛特尔（报价1 为赛特尔），
            # 其余来源仍作为备选展示；赛特尔内部按名称匹配质量→分数排序。
            # 变体守卫：赛特尔候选若带规格类/修饰词变体/BLOCK 警告（如初中电源
            # 顶替高中电源），不默认选中，避免“有赛特尔选赛特尔”引入错配。
            saitel_eligible = [
                item
                for item in eligible
                if is_saitel(item.record.get("source_file"))
                and not any(
                    str(warning).startswith(VARIANT_GUARD_PREFIXES)
                    for warning in item.warnings
                )
            ]
            # 客户专属价目优先级最高（高于赛特尔）：同样带变体守卫，
            # 带规格类/修饰词变体/BLOCK 警告的专属候选不默认选中。
            exclusive_eligible = [
                item
                for item in eligible
                if is_customer_exclusive(item.record)
                and not any(
                    str(warning).startswith(VARIANT_GUARD_PREFIXES)
                    for warning in item.warnings
                )
            ]
            default_pool = (
                exclusive_eligible
                if exclusive_eligible
                else (saitel_eligible if saitel_eligible else eligible)
            )
            # 名称质量优先（同核心词候选先于变体名），参数分量次之（同码不同规格
            # 由此分出先后，注射器 10/50/100mL 不再同价）；老编号体系 sheet 仅在
            # 名称质量相同时做价位 tiebreak（如 压力和压强演示器 9元 优先 22元）；
            # 不能把 老编号 排在 名称质量 前——否则 木直尺(初中物理) 会压过
            # 真正同名的 直尺(1000mm塑料) 候选。
            # 守卫池为空回退到 eligible 时同样按此 key 排序，回退不等于乱选。
            default_pool.sort(key=lambda item: (-_name_quality(item), -item.component_scores.get("参数", 0), _legacy_sheet_priority(item), -item.score))
            defaults = {item.record["id"] for item in distinct_manufacturer_options(default_pool, job.requested_option_count, min_score=SAITEL_FLOOR)}
            # 默认方案按“同核心词×价位簇”去重：同品同价位只保留一个默认选中，
            # 避免 3 个同价赛特尔方案占满 TOP 区，把不同价位挤到后面。
            seen_level: dict[str, list[float]] = {}

            def _keep_default(item) -> bool:
                key = name_core(item.record.get("name", "")) or "?"
                price = float(item.record.get("price", 0))
                levels = seen_level.setdefault(key, [])
                if any(abs(price - prev) / max(prev, 1e-9) <= 0.30 for prev in levels):
                    return False
                levels.append(price)
                return True

            defaults = {item.record["id"] for item in default_pool if item.record["id"] in defaults and _keep_default(item)}
            # Drop duplicate candidates: identical manufacturer/brand + price +
            # spec + model only ever produce one option card (the best-ranked).
            seen_option_keys: set[tuple] = set()
            rank = 0
            discount = float(customer.discount_percent if customer else 0)
            line_has_selected = False
            base_order = sorted(
                candidates,
                key=lambda item: (
                    item.record["id"] not in defaults,
                    # 名称质量优先；参数分量次之（同码不同规格的变体由此排序，
                    # 报价1 给参数最吻合的记录）；老编号体系 sheet 仅在同名
                    # 多价时做价位 tiebreak
                    -_name_quality(item),
                    -item.component_scores.get("参数", 0),
                    _legacy_sheet_priority(item),
                    -item.score,
                ),
            )
            ordered_candidates = diversify_price_levels(base_order, defaults, line_core_name)
            for candidate in ordered_candidates:
                record = candidate.record
                base_price = candidate.normalized_price or float(record["price"])
                # 客户专属价目为最终协议价：不再叠加 VIP 协议折扣
                if is_customer_exclusive(record):
                    final_price = round(base_price, 2)
                else:
                    final_price = round(base_price * (1 - discount / 100), 2)
                dedup_key = (
                    normalize_text(record.get("manufacturer") or record.get("brand")),
                    final_price,
                    normalize_text(record.get("spec")),
                    normalize_text(record.get("model")),
                )
                if dedup_key in seen_option_keys:
                    continue
                seen_option_keys.add(dedup_key)
                rank += 1
                option_warnings = list(candidate.warnings)
                if not _price_guard(candidate) and group_median:
                    option_warnings.append(
                        f"价格异常：偏离同组中位数（¥{group_median:g}）超过2倍，需人工核对"
                    )
                if discount and not is_customer_exclusive(record):
                    option_warnings.append(f"已应用客户折扣 {discount:g}% ，需核对毛利")
                if needs_margin_approval:
                    option_warnings.append(
                        f"BLOCK: VIP折扣需核对最低毛利线（{customer.minimum_margin_percent:g}%）"
                    )
                db.add(
                    QuoteOption(
                        line_id=line.id,
                        history_quote_id=record["id"],
                        rank=rank,
                        score=candidate.score,
                        confidence=candidate.confidence,
                        component_scores=candidate.component_scores,
                        reasons=candidate.reasons,
                        warnings=option_warnings,
                        unit_status=candidate.unit_status,
                        normalized_price=candidate.normalized_price,
                        selected=record["id"] in defaults and candidate.score >= _autoselect_floor(record),
                        final_price=final_price,
                    )
                )
                if record["id"] in defaults and candidate.score >= _autoselect_floor(record):
                    line_has_selected = True
            if not line_has_selected and group_prices:
                # 估算价兜底：无默认选中时按"同 JY/名称类目+同学段"的第 10 分位价
                # 生成保守估算方案（类目分位比核心词组更稳定，避免错配产品拖低
                # 分位价），待人工复核。
                line_cat = infer_category_from_name(raw_line.get("name", ""))
                line_marker = grade_marker(raw_line.get("name", ""))
                est_pool_prices: list[float] = []
                for hist in history:
                    if not hist.get("price"):
                        continue
                    if line_cat and infer_category_from_name(hist.get("name", "")) != line_cat:
                        continue
                    if line_marker and grade_marker(hist.get("name", "")) not in ("", line_marker):
                        continue
                    est_pool_prices.append(float(hist["price"]))
                if len(est_pool_prices) >= 5:
                    sorted_pool = sorted(est_pool_prices)
                    # 第 10 分位：比 P25 更保守，避免错配污染
                    est_base = sorted_pool[max(0, (len(sorted_pool) - 1) // 10)]
                    est_label = f"类目第10分位(共{len(est_pool_prices)}条)"
                else:
                    # 类目样本不足：退回原"同核心词组第 25 分位"
                    sorted_prices = sorted(group_prices)
                    est_base = sorted_prices[min(len(sorted_prices) - 1, len(sorted_prices) // 4)]
                    est_label = "同核心词组低位分位"
                est_price = round(est_base * (1 - discount / 100), 2)
                pick = min(candidates, key=lambda item: abs(float(item.record.get("price", 0)) - est_base)) if candidates else None
                rank += 1
                db.add(
                    QuoteOption(
                        line_id=line.id,
                        history_quote_id=pick.record["id"] if pick else None,
                        rank=rank,
                        score=0.0,
                        confidence="low",
                        component_scores={},
                        reasons=["估算"],
                        warnings=[f"估算价：按{est_label}（¥{est_base:g}）生成，需人工复核"],
                        unit_status="exact",
                        normalized_price=None,
                        selected=True,
                        final_price=est_price,
                    )
                )
                warnings.append(f"估算价：按{est_label}生成，需人工复核")
            # 收集行状态元数据（best.warnings 上的 severe/阻断判定只用于状态分配，
            # 不影响候选打分与默认选中）
            best_warnings = list(best.warnings) if best else []
            line_outcomes.append(
                {
                    "line": line,
                    "best_score": best_score,
                    "best_confidence": best.confidence if best else "unreliable",
                    "severe": any(
                        str(warning).startswith(VARIANT_GUARD_PREFIXES)
                        for warning in best_warnings
                    ),
                    "has_block": any(
                        str(warning).startswith("BLOCK:") for warning in best_warnings
                    ),
                    "has_candidates": bool(candidates),
                    "has_selected": line_has_selected,
                    "needs_margin_approval": needs_margin_approval,
                }
            )
            if index % 200 == 0:
                job.progress = min(95, 5 + int((index + 1) / len(parsed_lines) * 90))
                db.commit()
        # 行状态统一分配：hard-manual（无候选/无默认选中/BLOCK/VIP毛利核对）永远
        # 保留人工处理、不占复核额度；其余行按"severe 警告优先、低分优先"降级进
        # 复核，直到 复核+无匹配 行数达到 REVIEW_TARGET_PERCENT 目标比例。
        total = len(line_outcomes)
        target = 0 if total <= 3 else math.ceil(total * REVIEW_TARGET_PERCENT / 100)
        for item in line_outcomes:
            item["hard"] = (
                not item["has_candidates"]
                or not item["has_selected"]
                or item["has_block"]
                or item["needs_margin_approval"]
            )
        hard_count = sum(1 for item in line_outcomes if item["hard"])
        budget = max(0, target - hard_count)
        soft = [item for item in line_outcomes if not item["hard"]]
        soft.sort(key=lambda item: (not item["severe"], item["best_score"]))
        demoted_lines = {id(item["line"]) for item in soft[:budget]}
        for item in line_outcomes:
            line = item["line"]
            if not item["has_candidates"] or not item["has_selected"]:
                line.status = "unmatched"
                line.confidence = "unreliable"
                unmatched += 1
            elif item["hard"] or id(line) in demoted_lines:
                line.status = "review"
                line.confidence = (
                    "low" if item["best_confidence"] == "unreliable" else item["best_confidence"]
                )
                review += 1
            else:
                line.status = "suggested"
                line.confidence = "high"
                matched += 1
        job.total_lines = len(parsed_lines)
        job.matched_lines = matched
        job.review_lines = review
        job.unmatched_lines = unmatched
        job.confirmed_lines = 0
        job.progress = 100
        job.status = "review"
        job.updated_at = datetime.now(timezone.utc)
        db.commit()
    except Exception as exc:
        db.rollback()
        job = db.get(QuoteJob, job_id)
        if job:
            job.status = "failed"
            job.error_message = str(exc)
            job.progress = 100
            db.commit()
    finally:
        db.close()


def run_worker() -> None:
    seed_database()
    interval = float(os.getenv("WORKER_POLL_SECONDS", "1.5"))
    while True:
        db = database.SessionLocal()
        try:
            job = db.scalar(
                select(QuoteJob).where(QuoteJob.status == "queued").order_by(QuoteJob.created_at).limit(1)
            )
            job_id = job.id if job else None
            if job:
                job.status = "reserved"
                db.commit()
        finally:
            db.close()
        if job_id:
            process_job(job_id)
        else:
            time.sleep(interval)
