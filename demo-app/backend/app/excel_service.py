from __future__ import annotations

import os
import re
import unicodedata
import difflib
from dataclasses import dataclass
from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill

try:  # 老式 .xls 二进制格式读取支持（xlrd 2.x 只支持 .xls）
    import xlrd
except ImportError:  # pragma: no cover
    xlrd = None


HEADER_ALIASES = {
    "name": ("采购品目", "货物名称", "产品名称", "品名", "器材名称", "设备名称", "仪器名称", "名称"),
    "product_code": ("产品编码", "编码", "编号", "货号"),
    "quantity": ("数量", "需求数量", "采购数量", "配置数量", "每套数量", "套数"),
    "unit": ("单位", "计量单位"),
    "spec": ("参数", "技术参数", "主要技术参数", "主要性能要求", "技术性能要求", "性能要求", "规格", "规格参数", "规格说明", "技术要求"),
    "model": ("规格型号", "型号", "规格"),
    "brand": ("品牌", "商标"),
    "manufacturer": ("制造商名称", "制造商", "制造厂家", "厂家", "生产厂家"),
}


def cell_text(value: object) -> str:
    return re.sub(r"[\s\u3000]+", " ", str(value or "")).strip()


def cell_name(value: object) -> str:
    """Cell value for a product-name column.

    Procurement lists commonly wrap names across lines inside a single cell
    (e.g. ``中学物理实验室教师\\n演示台``).  Collapsing those line breaks to a
    space would inject a spurious gap in the name, so line-wrapped names are
    joined without whitespace; natural single spaces are kept otherwise.
    """
    text = str(value or "").strip()
    if "\n" in text or "\r" in text:
        return re.sub(r"[\s\u3000]+", "", text)
    return re.sub(r"[\s\u3000]+", " ", text)


def normalize_header_key(value: object) -> str:
    """Normalize a header label into a comparison key for column reuse."""
    return re.sub(r"[\s\u3000]+", "", str(value or "")).lower()


JY_CODE_RE = re.compile(r"^\d{5}$")
LINE_CODE_RE = re.compile(r"(?<![\dA-Za-z])([0-8]\d{4})(?![0-9A-Za-z.:+])")


def normalize_code_fields(record: dict) -> dict:
    """Move a JY five-digit product code mis-stored as 型号 into 产品编码.

    The 高中理化生报价(凯迪) style price books put the JY 教育装备编码
    (e.g. 04013 起电机) in the 规格型号 column. Treat a pure five-digit
    ``model`` as the authoritative product code so it can drive exact-code
    matching and stop polluting the 型号 column.  Long free-text kept in the
    same column (海口赛特尔 style 规格型号) is spec text, so it moves to spec.
    """
    model = str(record.get("model") or "").strip()
    if model and JY_CODE_RE.match(model):
        record = dict(record)
        if not record.get("product_code"):
            record["product_code"] = model
        record["model"] = ""
    elif model and len(model) > 20 and not record.get("spec"):
        record = dict(record)
        record["spec"] = model
        record["model"] = ""
    return record


def strip_code_decimal_suffix(value: object) -> str:
    """xls 数值格把编码读成 "27001.0"：纯整数+.0 形态去小数后缀还原编码。

    只处理"纯整数+.0"形态，真实含小数的编码（如 "3.5"）不受影响。
    """
    text = str(value or "").strip()
    if text.endswith(".0") and text[:-2].isdigit():
        return text[:-2]
    return text


def extract_line_code(*texts: object) -> str:
    """Pull a standalone five-digit JY code out of a raw inquiry cell.

    Conservative on purpose: the code must be bounded by non-alphanumeric
    characters, its first digit must be 0-8, and round numbers (ending 000)
    are rejected to avoid picking up quantities like 16000r/min or 12000lx.
    """
    for chunk in texts:
        text = unicodedata.normalize("NFKC", str(chunk or ""))
        for match in LINE_CODE_RE.finditer(text):
            code = match.group(1)
            if code.endswith("000"):
                continue
            return code
    return ""


def number_value(value: object) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    cleaned = re.sub(r"[,，￥¥]", "", str(value or "")).strip()
    try:
        return float(cleaned)
    except ValueError:
        return None


def find_header(ws, extra_aliases: dict | None = None, start_row: int = 1) -> tuple[int, dict[str, int]] | None:
    aliases_map = dict(HEADER_ALIASES)
    if extra_aliases:
        aliases_map.update(extra_aliases)
    # 表头匹配用去空白归一化键（"产   品   名   " → "产品名称"），否则
    # 带不规则空格的旧版价目本（如 赛特尔25年.xls 的 初中物理 等 sheet）
    # 整个 sheet 会因表头识别失败被跳过，导致精确产品行缺失。
    normalized_aliases = {
        field: {normalize_header_key(alias) for alias in aliases}
        for field, aliases in aliases_map.items()
    }
    for row_number in range(start_row, min(ws.max_row, start_row + 40) + 1):
        values = [normalize_header_key(ws.cell(row_number, column)) for column in range(1, ws.max_column + 1)]
        columns: dict[str, int] = {}
        for field, aliases in normalized_aliases.items():
            columns[field] = next(
                (index + 1 for index, value in enumerate(values) if value in aliases), 0
            )
        claimed = {index for index in columns.values() if index}
        if not columns["name"]:
            # Fuzzy fallback for real-world headers such as ``器材名称`` /
            # ``设备名称`` / ``采购品目清单``.  Only applies when no exact alias
            # matched, and never swallows a column already claimed by an exact
            # alias (e.g. ``制造商名称`` -> manufacturer, ``规格型号`` -> model).
            columns["name"] = next(
                (
                    index + 1
                    for index, value in enumerate(values)
                    if value
                    and index + 1 not in claimed
                    and (value.endswith("名称") or "品目" in value)
                ),
                0,
            )
        if not columns["name"]:
            continue
        if not columns["spec"]:
            # Same idea for spec columns labelled e.g. ``规格、品名、教学性能要求``;
            # without it such files yield zero product rows.
            columns["spec"] = next(
                (
                    index + 1
                    for index, value in enumerate(values)
                    if value
                    and index + 1 not in claimed
                    and (value.startswith("规格") or "性能要求" in value)
                ),
                0,
            )
            claimed.add(columns["spec"])
        if not columns["spec"]:
            # 重复表头兜底：部分文件规格列的表头也叫“器材名称”（如 初高中预算
            # 高中化学 sheet：第2、3列均为“器材名称”）。此时把 name 列右侧
            # 第一个重复表头列兼作规格列，否则整 sheet 因无 spec 被跳过。
            columns["spec"] = next(
                (
                    index + 1
                    for index, value in enumerate(values)
                    if value
                    and index + 1 not in claimed
                    and index + 1 > columns["name"]
                    and value == values[columns["name"] - 1]
                ),
                0,
            )
            claimed.add(columns["spec"])
        if not columns["spec"] and columns.get("model"):
            # 无独立参数列时，规格型号列兼作技术参数（该列内容多为规格文字而非型号）
            columns["spec"] = columns["model"]
        if "price" in aliases_map and not columns["price"]:
            # History-import only (price aliases are passed explicitly): accept
            # headers like ``赛特尔单价`` / ``市场报价``.  Same rules as above —
            # exact aliases win, claimed columns are never swallowed.
            columns["price"] = next(
                (
                    index + 1
                    for index, value in enumerate(values)
                    if value
                    and index + 1 not in claimed
                    and ("单价" in value or value.endswith("价格") or "报价" in value)
                ),
                0,
            )
        return row_number, columns
    return None


class XlsxSheetAdapter:
    __slots__ = ("_ws", "_title")

    def __init__(self, ws, title: str):
        self._ws = ws
        self._title = title

    @property
    def title(self) -> str:
        return self._title

    @property
    def max_row(self) -> int:
        return self._ws.max_row

    @property
    def max_column(self) -> int:
        return self._ws.max_column

    def cell(self, row: int, column: int):
        return self._ws.cell(row, column).value

    def data_type(self, row: int, column: int) -> str | None:
        return self._ws.cell(row, column).data_type


class XlsSheetAdapter:
    __slots__ = ("_sheet", "_title")

    def __init__(self, sheet, title: str):
        self._sheet = sheet
        self._title = title

    @property
    def title(self) -> str:
        return self._title

    @property
    def max_row(self) -> int:
        return self._sheet.nrows

    @property
    def max_column(self) -> int:
        return self._sheet.ncols

    def cell(self, row: int, column: int):
        return self._sheet.cell_value(row - 1, column - 1)

    def data_type(self, row: int, column: int) -> str | None:
        # xlrd: 3 == XL_CELL_FORMULA; the cached result is returned by cell().
        return "f" if self._sheet.cell_type(row - 1, column - 1) == 3 else None


class XlsxWorkbookAdapter:
    __slots__ = ("_path", "_wb", "_formula_wb")

    def __init__(self, path: str):
        self._path = path
        self._wb = load_workbook(path, data_only=True, read_only=False)
        self._formula_wb = None

    @property
    def sheets(self) -> list:
        return [XlsxSheetAdapter(ws, ws.title) for ws in self._wb.worksheets]

    def formula_sheet(self, title: str):
        if self._formula_wb is None:
            self._formula_wb = load_workbook(self._path, data_only=False, read_only=False)
        return XlsxSheetAdapter(self._formula_wb[title], title)

    def close(self) -> None:
        self._wb.close()
        if self._formula_wb is not None:
            self._formula_wb.close()


class XlsWorkbookAdapter:
    __slots__ = ("_book",)

    def __init__(self, path: str):
        if xlrd is None:
            raise ValueError("缺少依赖 xlrd，无法读取 .xls 文件（pip install xlrd）")
        self._book = xlrd.open_workbook(path)

    @property
    def sheets(self) -> list:
        return [XlsSheetAdapter(ws, ws.name) for ws in self._book.sheets()]

    def formula_sheet(self, title: str):
        return None  # xlrd 的 cell() 已返回公式缓存值，无需二次打开

    def close(self) -> None:
        pass


@dataclass
class WorkbookAdapter:
    """Version-agnostic workbook handle returned by open_workbook()."""
    impl: object
    path: str

    @property
    def sheets(self) -> list:
        return self.impl.sheets

    def formula_sheet(self, title: str):
        return self.impl.formula_sheet(title)

    def close(self) -> None:
        self.impl.close()


def open_workbook(path: str) -> WorkbookAdapter:
    lower = str(path).lower()
    if lower.endswith(".xls") and not lower.endswith(".xlsx") and not lower.endswith(".xlsm"):
        return WorkbookAdapter(XlsWorkbookAdapter(path), path)
    if lower.endswith((".xlsx", ".xlsm")):
        return WorkbookAdapter(XlsxWorkbookAdapter(path), path)
    raise ValueError("仅支持 .xlsx / .xlsm / .xls 文件")


def parse_quote_workbook(path: str) -> list[dict]:
    workbook = open_workbook(path)
    # Quote templates commonly keep the billing quantity in a formula column
    # (for example ``=D7*2``).  For xlsx that metadata is stripped by
    # ``data_only=True``, so a second data_only=False pass is the only way to
    # tell a genuine formula cell apart from a plain number (e.g. a unit-price
    # column) that must never override the quantity.  xlrd already returns the
    # cached formula result through cell().
    lines: list[dict] = []
    for ws in workbook.sheets:
        header = find_header(ws)
        if not header:
            continue
        header_row, columns = header
        for row_number in range(header_row + 1, ws.max_row + 1):
            name = cell_name(ws.cell(row_number, columns["name"]))
            spec = cell_text(ws.cell(row_number, columns["spec"])) if columns["spec"] else ""
            if not name or re.fullmatch(r"(?:小计|合计|总计|配置班额|一般|合计金额)", name):
                continue
            if re.match(r"^共\s*\d+", spec) or "配置清单如下" in spec:
                continue
            values = {
                field: cell_text(ws.cell(row_number, column)) if column else ""
                for field, column in columns.items()
                if field not in ("name", "quantity")
            }
            if not spec and not any(values.get(field) for field in ("product_code", "model", "brand", "manufacturer")):
                continue
            quantity = number_value(ws.cell(row_number, columns["quantity"])) if columns["quantity"] else None
            # 部分询价表把“数量/单位”两列写反（单位列填数字、数量列填单位词），
            # 按语义交换，避免导出缺数量/总价/税后总价。
            if (
                quantity is None
                and columns.get("unit")
                and columns.get("quantity")
                and values.get("unit")
                and re.fullmatch(r"\d[\d,，]*(?:\.\d+)?", values["unit"])
            ):
                quantity_text = cell_text(ws.cell(row_number, columns["quantity"]))
                if quantity_text and not re.fullmatch(r"\d[\d,，]*(?:\.\d+)?", quantity_text):
                    quantity = number_value(values["unit"])
                    values["unit"] = quantity_text
            # Column 6 only serves as the billing quantity when it genuinely
            # holds a formula (``=D7*2`` style).  Plain numbers there may be
            # unit prices (e.g. 普教清单's 含税单价) and must not be used.
            fallback_quantity = None
            if ws.max_column >= 6:
                formula_ws = workbook.formula_sheet(ws.title)
                if formula_ws is not None and formula_ws.data_type(row_number, 6) == "f":
                    fallback_quantity = number_value(ws.cell(row_number, 6))
            product_code = values.get("product_code", "")
            if not product_code:
                # No explicit code column: salvage a standalone five-digit JY code
                # from the spec/model/brand text so exact-code matching can fire.
                product_code = extract_line_code(spec, values.get("model", ""), values.get("brand", ""))
            lines.append(
                {
                    "sheet_name": ws.title,
                    "source_row": row_number,
                    "name": name,
                    "spec": spec,
                    "product_code": product_code,
                    "model": values.get("model", ""),
                    "brand": values.get("brand", ""),
                    "manufacturer": values.get("manufacturer", ""),
                    "unit": values.get("unit", ""),
                    "quantity": quantity,
                    "pricing_quantity": fallback_quantity if fallback_quantity and fallback_quantity > 0 else quantity,
                }
            )
    workbook.close()
    if not lines:
        raise ValueError("没有找到可识别的产品行，请检查表头和文件内容")
    return lines


# Price aliases are only used for history-library imports; they are passed to
# ``find_header`` explicitly so quote-sheet parsing is unaffected.
PRICE_ALIASES = ("单价", "含税单价", "报价", "价格")

# 历史价表中"含税单价"类表头的固定含税率（普教清单等），导入时折回税前基准。
TAX_INCLUSIVE_RATE = float(os.getenv("HISTORY_TAX_RATE", "0.10"))


def parse_history_workbook(path: str, source_name: str = "") -> list[dict]:
    """Parse every sheet of a history-quote workbook into raw records.

    A valid row needs a non-empty name; the price column must exist in the
    sheet header.  Rows with a missing/non-positive price are still returned
    (``price=None``) so the caller can count them as invalid.
    """
    workbook = open_workbook(path)
    records: list[dict] = []
    price_column_found = False
    for ws in workbook.sheets:
        header = find_header(ws, {"price": PRICE_ALIASES})
        if not header or not header[1].get("price"):
            continue
        price_column_found = True
        header_row, columns = header
        # “含税单价/含税价格”类表头按固定含税率折回税前基准，避免导出再乘税（双重计税）。
        # 注意 “不含税单价” 也含 “含税” 子串，必须先排除 “不含”。
        price_label = cell_text(ws.cell(header_row, columns["price"]))
        tax_inclusive = ("含税" in price_label) and ("不含" not in price_label)
        for row_number in range(header_row + 1, ws.max_row + 1):
            # 投标分项报价表常含多段表头（不同学科/学校，列位置不同）：识别
            # “序号/名称…单价（元）”形态的新表头行并切换解析列。
            name_cell = cell_text(ws.cell(row_number, columns["name"]))
            if name_cell in ("序号", "名称", "目录"):
                new_header = find_header(ws, {"price": PRICE_ALIASES}, start_row=row_number)
                if new_header and new_header[1].get("price"):
                    header_row, columns = new_header
                    price_label = cell_text(ws.cell(header_row, columns["price"]))
                    tax_inclusive = ("含税" in price_label) and ("不含" not in price_label)
                    continue
            name = cell_name(ws.cell(row_number, columns["name"]))
            if not name or re.fullmatch(r"(?:小计|合计|总计|配置班额|一般|合计金额|单套合计|单套金额|目录)", name):
                continue
            price = number_value(ws.cell(row_number, columns["price"]))
            if tax_inclusive and price:
                price = round(price / (1 + TAX_INCLUSIVE_RATE), 2)
            record = {
                "sheet_name": ws.title,
                "source_row": row_number,
                "name": name,
                "spec": cell_text(ws.cell(row_number, columns["spec"])) if columns.get("spec") else "",
                # xls 数值格编码去 ".0" 后缀（"27001.0"→"27001"），
                # 否则 normalize_text 后变 270010，exact-code 分流整体失效
                "product_code": strip_code_decimal_suffix(cell_text(ws.cell(row_number, columns["product_code"]))) if columns.get("product_code") else "",
                "model": strip_code_decimal_suffix(cell_text(ws.cell(row_number, columns["model"]))) if columns.get("model") else "",
                "brand": cell_text(ws.cell(row_number, columns["brand"])) if columns.get("brand") else "",
                "manufacturer": cell_text(ws.cell(row_number, columns["manufacturer"])) if columns.get("manufacturer") else "",
                "unit": cell_text(ws.cell(row_number, columns["unit"])) if columns.get("unit") else "",
                "price": price,
            }
            records.append(normalize_code_fields(record))
    workbook.close()
    if not price_column_found:
        raise ValueError("未找到价格列（需包含 单价/价格/报价 表头）")
    return records


def _option_field(option, field: str):
    """Read a descriptive field from the linked history quote, or from the
    manual_* columns for history-free manual options."""
    history = option.history_quote
    if history is not None:
        return getattr(history, field) or ""
    return getattr(option, f"manual_{field}", None) or ""


def normalize_standard_code(value: object) -> str:
    """课标编号规范化为纯文本：去空白、去误存的 ".0" 小数后缀（01045 这类
    5 位老课标编号需保留前导零，不能按数字写回）。"""
    text = re.sub(r"[\s\u3000]+", "", str(value or ""))
    if text.endswith(".0") and text[:-2].isdigit():
        text = text[:-2]
    return text


_SPEC_UNIT_FACTORS = {
    "mm": ("len", 1.0), "cm": ("len", 10.0), "m": ("len", 1000.0),
    "ml": ("vol", 1.0), "l": ("vol", 1000.0),
    "g": ("mass", 1.0), "kg": ("mass", 1000.0),
}


def _spec_key(text: object) -> str:
    return re.sub(r"[\s\u3000]+", "", unicodedata.normalize("NFKC", str(text or "")))


def _name_core(name: object) -> str:
    """名称去括号注释（如 家蚊（雌）口器装片 → 家蚊口器装片）用于同品归一。"""
    return re.sub(r"[（(].*?[)）]", "", str(name or "")).strip()


def _spec_keyset(text: object) -> set:
    normalized = unicodedata.normalize("NFKC", str(text or ""))
    keys = set(re.findall(r"\d+(?:\.\d+)?", normalized))
    for value, unit in re.findall(
        r"(\d+(?:\.\d+)?)\s*(mm|cm|m|ml|l|kg|g)(?![A-Za-z0-9])", normalized, flags=re.I
    ):
        factor = _SPEC_UNIT_FACTORS.get(unit.lower())
        if factor:
            keys.add((factor[0], round(float(value) * factor[1], 3)))
    return keys


def _cluster_spec_keys(keys: list[str]) -> list[str]:
    parent = list(range(len(keys)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    for i in range(len(keys)):
        for j in range(i + 1, len(keys)):
            if difflib.SequenceMatcher(None, keys[i], keys[j]).ratio() >= 0.8:
                parent[find(i)] = find(j)
    groups: dict[int, list[str]] = {}
    for index, key in enumerate(keys):
        groups.setdefault(find(index), []).append(key)
    return [max(members, key=len) for members in groups.values()]


def _pick_spec_alias(group: dict[str, str], inquiry_spec: object) -> str | None:
    """在赛特尔价目本同一品名的多条简介里挑与询价规格最一致的一条：
    简介文字直接命中 →（近重复合并后）数字/单位覆盖率唯一最高（≥0.5）→ 其余留空。"""
    if not group:
        return None
    if len(group) == 1:
        return next(iter(group.values()))
    inquiry_key = _spec_key(inquiry_spec)
    hits = [raw for key, raw in group.items() if len(key) >= 4 and key in inquiry_key]
    if len(hits) == 1:
        return hits[0]
    representatives = _cluster_spec_keys(list(group.keys()))
    inquiry_keys = _spec_keyset(inquiry_spec)
    scored = []
    for representative in representatives:
        members = [
            raw
            for key, raw in group.items()
            if key == representative or difflib.SequenceMatcher(None, key, representative).ratio() >= 0.8
        ]
        raw = max(members, key=len)
        candidate_keys = _spec_keyset(raw)
        inter = len(inquiry_keys & candidate_keys)
        coverage = inter / max(1, min(len(inquiry_keys), len(candidate_keys))) if inquiry_keys else 0.0
        scored.append((coverage, inter, raw))
    scored.sort(key=lambda item: (-item[0], -item[1], len(item[2])))
    if scored and scored[0][0] >= 0.5 and (len(scored) == 1 or scored[0][0] > scored[1][0]):
        return scored[0][2]
    return None


def _build_spec_alias_cache(db):
    """加载赛特尔价目本全部非空简介（按 编号+品名 与 品名 建索引），供主记录
    缺简介时以同品名条目回填参数列。"""
    from sqlalchemy import select

    from .models import HistoryQuote

    by_code_name: dict[tuple[str, str], dict[str, str]] = {}
    by_name: dict[str, dict[str, str]] = {}
    for code, name, spec in db.execute(
        select(HistoryQuote.product_code, HistoryQuote.name, HistoryQuote.spec)
    ):
        text = str(spec or "").strip()
        item_name = str(name or "").strip()
        if not text or not item_name:
            continue
        key = _spec_key(text)
        core = _name_core(item_name)
        code = str(code or "").strip()
        if code:
            by_code_name.setdefault((code, core), {}).setdefault(key, text)
        by_name.setdefault(core, {}).setdefault(key, text)
    return by_code_name, by_name


def _fallback_spec(record, inquiry_spec: object, cache) -> str:
    by_code_name, by_name = cache
    code = str(getattr(record, "product_code", "") or "").strip()
    core = _name_core(getattr(record, "name", ""))
    pool: dict[str, str] = {}
    if code:
        pool.update(by_code_name.get((code, core), {}))
    pool.update(by_name.get(core, {}))
    return _pick_spec_alias(pool, inquiry_spec) or ""


def _option_source(option) -> str:
    history = option.history_quote
    if history is None:
        return "手工方案"
    return f"{history.source_file}｜{history.source_sheet}｜第{history.source_row}行"


def _option_values(option, include_internal: bool, quantity: float | None = None, tax_rate: float = 0.10) -> list:
    history = option.history_quote
    subtotal = round(option.final_price * quantity, 2) if quantity else ""
    tax_rate = float(tax_rate or 0.10)
    tax_unit = round(option.final_price * (1 + tax_rate), 2)
    tax_subtotal = round(tax_unit * quantity, 2) if quantity else ""
    base = [
        option.rank,
        _option_field(option, "manufacturer") or "未记录制造商",
        _option_field(option, "brand"),
        _option_field(option, "model"),
        _option_field(option, "product_code"),
        _option_field(option, "spec"),
        _option_field(option, "unit"),
        quantity or "",
        option.final_price,
        tax_unit,
        subtotal,
        tax_subtotal,
        history.quote_date if history is not None else "",
    ]
    if include_internal:
        base.extend(
            [
                option.score,
                "；".join(option.warnings or []),
                _option_source(option),
            ]
        )
    return base


def xls_to_xlsx(source_path: str, output_path: str) -> str:
    """Convert a legacy .xls workbook to .xlsx (values only) for export.

    ``create_export`` works on an openpyxl workbook; legacy .xls files are
    converted to an xlsx copy first (formulas keep their cached results,
    images/formatting are dropped).
    """
    if xlrd is None:
        raise ValueError("缺少依赖 xlrd，无法读取 .xls 文件（pip install xlrd）")
    book = xlrd.open_workbook(source_path)
    target = Workbook()
    target.remove(target.active)
    for sheet in book.sheets():
        ws = target.create_sheet(sheet.name)
        for row_number in range(sheet.nrows):
            for column_number in range(sheet.ncols):
                value = sheet.cell_value(row_number, column_number)
                if value != "":
                    ws.cell(row_number + 1, column_number + 1, value)
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    target.save(output_path)
    target.close()
    return output_path


def create_export(job, variant: str, output_path: str, db=None) -> str:
    include_internal = variant == "internal"
    rate = float(getattr(job, "tax_rate", 0.10) or 0.10)
    spec_alias_cache = _build_spec_alias_cache(db) if db is not None else None
    source_path = job.source_file_path
    converted = None
    if str(source_path).lower().endswith(".xls") and not str(source_path).lower().endswith((".xlsx", ".xlsm")):
        converted = str(Path(output_path).with_name("_source_converted.xlsx"))
        xls_to_xlsx(source_path, converted)
        source_path = converted
    workbook = load_workbook(source_path)
    line_by_sheet_row = {(line.sheet_name, line.source_row): line for line in job.lines}
    for ws in workbook.worksheets:
        relevant = [line for line in job.lines if line.sheet_name == ws.title]
        if not relevant:
            continue
        header_row = max(1, min(line.source_row for line in relevant) - 1)
        extra_option_count = max(0, (job.requested_option_count or 1) - 1)

        # 原表各列表头已在行内时优先复用，避免导出重复的 数量/单位 等列。
        existing: dict[str, int] = {}
        for column in range(1, ws.max_column + 1):
            label = ws.cell(header_row, column).value
            if label:
                existing.setdefault(normalize_header_key(label), column)
        assign: dict[str, int] = {}
        next_col = ws.max_column + 1

        def ensure(field: str, header_label: str, key_hints: tuple[str, ...]) -> None:
            nonlocal next_col
            for key in key_hints:
                if key in existing:
                    assign[field] = existing[key]
                    return
            col = next_col
            cell = ws.cell(header_row, col, header_label)
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="0F766E")
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            assign[field] = col
            next_col += 1

        ensure("数量", "数量", ("数量", "需求数量", "采购数量", "配置数量", "每套数量", "套数"))
        ensure("确认单价", "确认单价", ("确认单价", "单价"))
        ensure("税后单价", "税后单价", ("税后单价",))
        ensure("总价", "总价", ("总价", "金额"))
        ensure("税后总价", "税后总价", ("税后总价",))
        ensure("品牌", "品牌", ("品牌", "商标"))
        ensure("制造商", "制造商", ("制造商", "制造商名称", "制造厂家", "厂家", "生产厂家"))
        ensure("型号", "型号", ("型号", "规格型号"))
        ensure("产品编码", "产品编码", ("产品编码", "编码", "货号"))
        ensure("单位", "报价单位", ("报价单位", "单位", "计量单位"))
        for index in range(2, extra_option_count + 2):
            ensure(f"报价{index}单价", f"报价{index}单价", (f"报价{index}单价",))
            ensure(f"报价{index}制造商", f"报价{index}制造商", (f"报价{index}制造商",))
        if include_internal:
            ensure("匹配状态", "匹配状态", ("匹配状态",))
            ensure("匹配分", "匹配分", ("匹配分",))
            ensure("风险提示", "风险提示", ("风险提示",))
            ensure("历史来源", "历史来源", ("历史来源",))
        # 目录简介列：原表重复出现“规格”占位列时复用并命名“参数”（型号|规格 模板）；
        # 否则在表尾新增“目录简介”，绝不改动原“参数”等原始列。
        spec_columns = [
            column
            for column in range(1, ws.max_column + 1)
            if normalize_header_key(ws.cell(header_row, column).value) == "规格"
        ]
        if len(spec_columns) > 1:
            param_column = spec_columns[-1]
            header_cell = ws.cell(header_row, param_column, "参数")
            header_cell.font = Font(bold=True, color="FFFFFF")
            header_cell.fill = PatternFill("solid", fgColor="0F766E")
            header_cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            assign["参数"] = param_column
            spec_field = "参数"
        else:
            ensure("目录简介", "目录简介", ("目录简介",))
            spec_field = "目录简介"

        for (sheet_name, row_number), line in line_by_sheet_row.items():
            if sheet_name != ws.title:
                continue
            selected = sorted((item for item in line.options if item.selected), key=lambda item: item.rank)
            primary = selected[0] if selected else None
            quantity = line.pricing_quantity or line.quantity
            values: dict[str, object] = {
                "数量": quantity or "",
                "确认单价": "",
                "税后单价": "",
                "总价": "",
                "税后总价": "",
                "品牌": "",
                "制造商": "",
                "型号": "",
                "产品编码": "",
                "单位": line.unit or "",
            }
            values[spec_field] = ""
            if primary and (include_internal or line.confirmed):
                tax_unit = round(primary.final_price * (1 + rate), 2)
                total = round(primary.final_price * quantity, 2) if quantity else ""
                values["确认单价"] = primary.final_price
                values["税后单价"] = tax_unit
                values["总价"] = total
                values["税后总价"] = round(tax_unit * quantity, 2) if quantity else ""
                values["品牌"] = _option_field(primary, "brand")
                values["制造商"] = _option_field(primary, "manufacturer")
                values["型号"] = normalize_standard_code(_option_field(primary, "product_code"))
                values["产品编码"] = normalize_standard_code(_option_field(primary, "product_code"))
                spec_text = _option_field(primary, "spec")
                if not spec_text and spec_alias_cache is not None and primary.history_quote is not None:
                    spec_text = _fallback_spec(primary.history_quote, line.spec, spec_alias_cache)
                values[spec_field] = spec_text
                values["单位"] = line.unit or _option_field(primary, "unit")
            for index in range(extra_option_count):
                option = selected[index + 1] if index + 1 < len(selected) else None
                if option:
                    values[f"报价{index + 2}单价"] = option.final_price
                    values[f"报价{index + 2}制造商"] = _option_field(option, "manufacturer")
            if include_internal:
                values["匹配状态"] = line.status
                values["匹配分"] = line.recommended_score
                values["风险提示"] = "；".join(line.warnings or [])
                values["历史来源"] = _option_source(primary) if primary else ""
            for field, value in values.items():
                cell = ws.cell(row_number, assign[field], value)
                cell.alignment = Alignment(vertical="center", wrap_text=True)
                cell.fill = PatternFill(
                    "solid", fgColor="E8F5EE" if line.confirmed else "FFF4CC"
                )

    detail_title = "内部方案明细" if include_internal else "多方案报价明细"
    if detail_title in workbook.sheetnames:
        del workbook[detail_title]
    detail = workbook.create_sheet(detail_title)
    headers = ["原表页", "原表行", "产品名称", "方案", "制造商", "品牌", "型号", "产品编码", "规格", "单位", "数量", "报价", "税后单价", "小计", "税后总价", "报价日期"]
    if include_internal:
        headers.extend(["匹配分", "风险提示", "历史来源"])
    detail.append(headers)
    for cell in detail[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="0F766E")
        cell.alignment = Alignment(horizontal="center", vertical="center")
    for line in sorted(job.lines, key=lambda item: (item.sheet_name, item.source_row)):
        quantity = line.pricing_quantity or line.quantity
        for option in sorted((item for item in line.options if item.selected), key=lambda item: item.rank):
            values = [line.sheet_name, line.source_row, line.name]
            values.extend(_option_values(option, include_internal, quantity, rate))
            detail.append(values)
    widths = [16, 10, 24, 8, 30, 18, 18, 58, 10, 10, 14, 14, 14, 12, 42, 42]
    for index, width in enumerate(widths, start=1):
        detail.column_dimensions[chr(64 + index) if index <= 26 else "A"].width = width
    detail.freeze_panes = "A2"
    detail.auto_filter.ref = detail.dimensions
    for row in detail.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = Alignment(vertical="top", wrap_text=True)

    if include_internal:
        review_sheet = workbook.create_sheet("待复核清单")
        review_headers = ["原表页", "原表行", "产品名称", "规格", "单位", "数量", "估算价", "风险提示"]
        review_sheet.append(review_headers)
        for cell in review_sheet[1]:
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="B45309")
            cell.alignment = Alignment(horizontal="center", vertical="center")
        for line in sorted(job.lines, key=lambda item: (item.sheet_name, item.source_row)):
            if line.confirmed:
                continue
            estimate = ""
            for option in sorted((o for o in line.options if o.selected), key=lambda o: o.rank):
                if any("估算价" in str(w) for w in (option.warnings or [])):
                    estimate = option.final_price
                    break
            review_sheet.append([
                line.sheet_name,
                line.source_row,
                line.name,
                line.spec,
                line.unit,
                line.pricing_quantity or line.quantity or "",
                estimate if estimate else "",
                "；".join(line.warnings or []),
            ])
        for index, width in enumerate([16, 10, 24, 58, 8, 10, 12, 46], start=1):
            review_sheet.column_dimensions[chr(64 + index)].width = width
        review_sheet.freeze_panes = "A2"
        for row in review_sheet.iter_rows(min_row=2):
            for cell in row:
                cell.alignment = Alignment(vertical="top", wrap_text=True)

    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(output)
    workbook.close()
    if converted:
        Path(converted).unlink(missing_ok=True)
    return str(output)
