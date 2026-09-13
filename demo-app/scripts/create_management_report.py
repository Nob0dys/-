from datetime import date
from pathlib import Path

from docx import Document
from docx.enum.section import WD_SECTION
from docx.enum.table import WD_ALIGN_VERTICAL, WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor


OUT = Path(__file__).resolve().parents[1] / "deliverables" / "智能报价系统_Demo技术报告与正式版需求手册.docx"

# standard_business_brief preset, with a named CJK font override for macOS readability.
INK = "0B2545"
BLUE = "2E74B5"
DARK_BLUE = "1F4D78"
MUTED = "5B6573"
LIGHT_BLUE = "E8EEF5"
LIGHT_GRAY = "F2F4F7"
CALLOUT = "F4F6F9"
GOLD = "7A5A00"
GREEN = "1F6B4F"
RED = "9B1C1C"
TABLE_WIDTH = 9360
TABLE_INDENT = 120


def set_run_font(run, size=11, color=INK, bold=False, italic=False, name="Noto Sans CJK SC", east_asia="Noto Sans CJK SC"):
    run.font.name = name
    run._element.rPr.rFonts.set(qn("w:ascii"), name)
    run._element.rPr.rFonts.set(qn("w:hAnsi"), name)
    run._element.rPr.rFonts.set(qn("w:eastAsia"), east_asia)
    run.font.size = Pt(size)
    run.font.color.rgb = RGBColor.from_string(color)
    run.bold = bold
    run.italic = italic
    return run


def set_paragraph_border_bottom(paragraph, color="B7C5D6", size="10", space="8"):
    p_pr = paragraph._p.get_or_add_pPr()
    p_bdr = p_pr.find(qn("w:pBdr"))
    if p_bdr is None:
        p_bdr = OxmlElement("w:pBdr")
        p_pr.append(p_bdr)
    bottom = OxmlElement("w:bottom")
    bottom.set(qn("w:val"), "single")
    bottom.set(qn("w:sz"), size)
    bottom.set(qn("w:space"), space)
    bottom.set(qn("w:color"), color)
    p_bdr.append(bottom)


def set_cell_shading(cell, color):
    tc_pr = cell._tc.get_or_add_tcPr()
    shd = tc_pr.find(qn("w:shd"))
    if shd is None:
        shd = OxmlElement("w:shd")
        tc_pr.append(shd)
    shd.set(qn("w:fill"), color)


def set_cell_margins(cell, top=80, start=120, bottom=80, end=120):
    tc_pr = cell._tc.get_or_add_tcPr()
    tc_mar = tc_pr.find(qn("w:tcMar"))
    if tc_mar is None:
        tc_mar = OxmlElement("w:tcMar")
        tc_pr.append(tc_mar)
    for side, value in (("top", top), ("start", start), ("bottom", bottom), ("end", end)):
        tag = qn(f"w:{side}")
        node = tc_mar.find(tag)
        if node is None:
            node = OxmlElement(f"w:{side}")
            tc_mar.append(node)
        node.set(qn("w:w"), str(value))
        node.set(qn("w:type"), "dxa")


def set_cell_width(cell, width):
    tc_pr = cell._tc.get_or_add_tcPr()
    tc_w = tc_pr.find(qn("w:tcW"))
    if tc_w is None:
        tc_w = OxmlElement("w:tcW")
        tc_pr.append(tc_w)
    tc_w.set(qn("w:w"), str(width))
    tc_w.set(qn("w:type"), "dxa")


def set_table_borders(table, color="D5DCE5"):
    tbl_pr = table._tbl.tblPr
    borders = tbl_pr.find(qn("w:tblBorders"))
    if borders is None:
        borders = OxmlElement("w:tblBorders")
        tbl_pr.append(borders)
    for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
        tag = qn(f"w:{edge}")
        element = borders.find(tag)
        if element is None:
            element = OxmlElement(f"w:{edge}")
            borders.append(element)
        element.set(qn("w:val"), "single")
        element.set(qn("w:sz"), "4")
        element.set(qn("w:space"), "0")
        element.set(qn("w:color"), color)


def set_table_geometry(table, widths, header=True, indent=TABLE_INDENT):
    if sum(widths) != TABLE_WIDTH:
        raise ValueError(f"Table widths must add to {TABLE_WIDTH}: {widths}")
    table.alignment = WD_TABLE_ALIGNMENT.LEFT
    table.autofit = False
    tbl_pr = table._tbl.tblPr
    tbl_w = tbl_pr.find(qn("w:tblW"))
    if tbl_w is None:
        tbl_w = OxmlElement("w:tblW")
        tbl_pr.append(tbl_w)
    tbl_w.set(qn("w:w"), str(TABLE_WIDTH))
    tbl_w.set(qn("w:type"), "dxa")
    tbl_ind = tbl_pr.find(qn("w:tblInd"))
    if tbl_ind is None:
        tbl_ind = OxmlElement("w:tblInd")
        tbl_pr.append(tbl_ind)
    tbl_ind.set(qn("w:w"), str(indent))
    tbl_ind.set(qn("w:type"), "dxa")
    grid_cols = table._tbl.tblGrid.gridCol_lst
    for index, width in enumerate(widths):
        grid_cols[index].set(qn("w:w"), str(width))
    for row_index, row in enumerate(table.rows):
        tr_pr = row._tr.get_or_add_trPr()
        if tr_pr.find(qn("w:cantSplit")) is None:
            tr_pr.append(OxmlElement("w:cantSplit"))
        for col_index, cell in enumerate(row.cells):
            set_cell_width(cell, widths[col_index])
            set_cell_margins(cell)
            cell.vertical_alignment = WD_ALIGN_VERTICAL.CENTER
            for paragraph in cell.paragraphs:
                paragraph.paragraph_format.space_before = Pt(0)
                paragraph.paragraph_format.space_after = Pt(0)
                paragraph.paragraph_format.line_spacing = 1.1
        if header and row_index == 0:
            if tr_pr.find(qn("w:tblHeader")) is None:
                tr_pr.append(OxmlElement("w:tblHeader"))
    set_table_borders(table)


def write_cell(cell, text, *, bold=False, color=INK, size=10.2, align=WD_ALIGN_PARAGRAPH.LEFT):
    cell.text = ""
    p = cell.paragraphs[0]
    p.alignment = align
    p.paragraph_format.space_before = Pt(0)
    p.paragraph_format.space_after = Pt(0)
    p.paragraph_format.line_spacing = 1.1
    set_run_font(p.add_run(text), size=size, color=color, bold=bold)


def add_table(doc, headers, rows, widths, header_fill=LIGHT_GRAY, alignments=None):
    table = doc.add_table(rows=1, cols=len(headers))
    set_table_geometry(table, widths, header=True)
    for index, value in enumerate(headers):
        cell = table.rows[0].cells[index]
        set_cell_shading(cell, header_fill)
        write_cell(cell, value, bold=True, color=DARK_BLUE, size=10, align=WD_ALIGN_PARAGRAPH.CENTER)
    for row_values in rows:
        cells = table.add_row().cells
        for index, value in enumerate(row_values):
            alignment = alignments[index] if alignments else WD_ALIGN_PARAGRAPH.LEFT
            write_cell(cells[index], value, align=alignment)
    set_table_geometry(table, widths, header=True)
    return table


def add_callout(doc, label, text, color=INK):
    table = doc.add_table(rows=1, cols=1)
    set_table_geometry(table, [TABLE_WIDTH], header=False)
    cell = table.cell(0, 0)
    set_cell_shading(cell, CALLOUT)
    cell.text = ""
    p = cell.paragraphs[0]
    p.paragraph_format.space_after = Pt(0)
    p.paragraph_format.line_spacing = 1.15
    set_run_font(p.add_run(label + "  "), size=10.5, color=color, bold=True)
    set_run_font(p.add_run(text), size=10.5, color=INK)
    doc.add_paragraph().paragraph_format.space_after = Pt(2)


def add_page_field(paragraph):
    run = paragraph.add_run()
    begin = OxmlElement("w:fldChar")
    begin.set(qn("w:fldCharType"), "begin")
    instr = OxmlElement("w:instrText")
    instr.set(qn("xml:space"), "preserve")
    instr.text = "PAGE"
    end = OxmlElement("w:fldChar")
    end.set(qn("w:fldCharType"), "end")
    run._r.append(begin)
    run._r.append(instr)
    run._r.append(end)
    set_run_font(run, size=9, color=MUTED)


def configure_numbering(doc):
    numbering = doc.part.numbering_part.element

    def add_abstract(abstract_id, marker, is_decimal):
        abstract = OxmlElement("w:abstractNum")
        abstract.set(qn("w:abstractNumId"), str(abstract_id))
        multi = OxmlElement("w:multiLevelType")
        multi.set(qn("w:val"), "singleLevel")
        abstract.append(multi)
        lvl = OxmlElement("w:lvl")
        lvl.set(qn("w:ilvl"), "0")
        start = OxmlElement("w:start")
        start.set(qn("w:val"), "1")
        num_fmt = OxmlElement("w:numFmt")
        num_fmt.set(qn("w:val"), "decimal" if is_decimal else "bullet")
        lvl_text = OxmlElement("w:lvlText")
        lvl_text.set(qn("w:val"), marker)
        lvl_jc = OxmlElement("w:lvlJc")
        lvl_jc.set(qn("w:val"), "left")
        p_pr = OxmlElement("w:pPr")
        tabs = OxmlElement("w:tabs")
        tab = OxmlElement("w:tab")
        tab.set(qn("w:val"), "num")
        tab.set(qn("w:pos"), "720")
        tabs.append(tab)
        ind = OxmlElement("w:ind")
        ind.set(qn("w:left"), "720")
        ind.set(qn("w:hanging"), "360")
        p_pr.append(tabs)
        p_pr.append(ind)
        r_pr = OxmlElement("w:rPr")
        r_fonts = OxmlElement("w:rFonts")
        r_fonts.set(qn("w:ascii"), "Noto Sans CJK SC")
        r_fonts.set(qn("w:hAnsi"), "Noto Sans CJK SC")
        r_fonts.set(qn("w:eastAsia"), "Noto Sans CJK SC")
        r_pr.append(r_fonts)
        lvl.extend([start, num_fmt, lvl_text, lvl_jc, p_pr, r_pr])
        abstract.append(lvl)
        numbering.append(abstract)

    def add_num(num_id, abstract_id):
        num = OxmlElement("w:num")
        num.set(qn("w:numId"), str(num_id))
        abstract_ref = OxmlElement("w:abstractNumId")
        abstract_ref.set(qn("w:val"), str(abstract_id))
        num.append(abstract_ref)
        numbering.append(num)

    add_abstract(9001, "•", False)
    add_num(9001, 9001)
    add_abstract(9002, "%1.", True)
    add_num(9002, 9002)


def apply_num(paragraph, num_id):
    p_pr = paragraph._p.get_or_add_pPr()
    num_pr = OxmlElement("w:numPr")
    ilvl = OxmlElement("w:ilvl")
    ilvl.set(qn("w:val"), "0")
    num = OxmlElement("w:numId")
    num.set(qn("w:val"), str(num_id))
    num_pr.append(ilvl)
    num_pr.append(num)
    p_pr.append(num_pr)


def add_bullet(doc, text, *, color=INK):
    p = doc.add_paragraph()
    apply_num(p, 9001)
    p.paragraph_format.space_before = Pt(0)
    p.paragraph_format.space_after = Pt(5)
    p.paragraph_format.line_spacing = 1.167
    set_run_font(p.add_run(text), size=11, color=color)
    return p


def add_numbered(doc, text):
    p = doc.add_paragraph()
    apply_num(p, 9002)
    p.paragraph_format.space_before = Pt(0)
    p.paragraph_format.space_after = Pt(5)
    p.paragraph_format.line_spacing = 1.167
    set_run_font(p.add_run(text), size=11, color=INK)
    return p


def add_body(doc, text, *, bold_prefix=None):
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.LEFT
    p.paragraph_format.space_before = Pt(0)
    p.paragraph_format.space_after = Pt(6)
    p.paragraph_format.line_spacing = 1.1
    if bold_prefix and text.startswith(bold_prefix):
        set_run_font(p.add_run(bold_prefix), size=11, color=INK, bold=True)
        set_run_font(p.add_run(text[len(bold_prefix):]), size=11, color=INK)
    else:
        set_run_font(p.add_run(text), size=11, color=INK)
    return p


def add_heading(doc, text, level=1):
    style = {1: "Heading 1", 2: "Heading 2", 3: "Heading 3"}[level]
    p = doc.add_paragraph(style=style)
    p.paragraph_format.keep_with_next = True
    set_run_font(
        p.add_run(text),
        size={1: 16, 2: 13, 3: 12}[level],
        color={1: BLUE, 2: BLUE, 3: DARK_BLUE}[level],
        bold=True,
    )
    return p


def add_section_break(doc):
    section = doc.add_section(WD_SECTION.NEW_PAGE)
    configure_section(section)
    configure_header_footer(section)
    return section


def configure_section(section):
    section.page_width = Inches(8.5)
    section.page_height = Inches(11)
    section.top_margin = Inches(1)
    section.bottom_margin = Inches(1)
    section.left_margin = Inches(1)
    section.right_margin = Inches(1)
    section.header_distance = Inches(0.492)
    section.footer_distance = Inches(0.492)


def configure_header_footer(section):
    header = section.header
    p = header.paragraphs[0]
    p.alignment = WD_ALIGN_PARAGRAPH.LEFT
    p.paragraph_format.space_before = Pt(0)
    p.paragraph_format.space_after = Pt(0)
    p.clear()
    set_run_font(p.add_run("智能报价系统｜技术报告与正式版需求手册"), size=9, color=MUTED)
    footer = section.footer
    p = footer.paragraphs[0]
    p.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    p.paragraph_format.space_before = Pt(0)
    p.paragraph_format.space_after = Pt(0)
    p.clear()
    set_run_font(p.add_run("内部沟通材料｜第 "), size=9, color=MUTED)
    add_page_field(p)
    set_run_font(p.add_run(" 页"), size=9, color=MUTED)


def configure_styles(doc):
    normal = doc.styles["Normal"]
    normal.font.name = "Noto Sans CJK SC"
    normal._element.rPr.rFonts.set(qn("w:ascii"), "Noto Sans CJK SC")
    normal._element.rPr.rFonts.set(qn("w:hAnsi"), "Noto Sans CJK SC")
    normal._element.rPr.rFonts.set(qn("w:eastAsia"), "Noto Sans CJK SC")
    normal.font.size = Pt(11)
    normal.font.color.rgb = RGBColor.from_string(INK)
    normal.paragraph_format.space_before = Pt(0)
    normal.paragraph_format.space_after = Pt(6)
    normal.paragraph_format.line_spacing = 1.1
    for name, size, color, before, after in (
        ("Heading 1", 16, BLUE, 16, 8),
        ("Heading 2", 13, BLUE, 12, 6),
        ("Heading 3", 12, DARK_BLUE, 8, 4),
    ):
        style = doc.styles[name]
        style.font.name = "Noto Sans CJK SC"
        style._element.rPr.rFonts.set(qn("w:ascii"), "Noto Sans CJK SC")
        style._element.rPr.rFonts.set(qn("w:hAnsi"), "Noto Sans CJK SC")
        style._element.rPr.rFonts.set(qn("w:eastAsia"), "Noto Sans CJK SC")
        style.font.size = Pt(size)
        style.font.bold = True
        style.font.color.rgb = RGBColor.from_string(color)
        style.paragraph_format.space_before = Pt(before)
        style.paragraph_format.space_after = Pt(after)
        style.paragraph_format.line_spacing = 1.1


def add_title_block(doc):
    p = doc.add_paragraph()
    p.paragraph_format.space_before = Pt(16)
    p.paragraph_format.space_after = Pt(4)
    set_run_font(p.add_run("技术报告与需求手册"), size=11, color=DARK_BLUE, bold=True)
    p = doc.add_paragraph()
    p.paragraph_format.space_before = Pt(0)
    p.paragraph_format.space_after = Pt(5)
    p.paragraph_format.line_spacing = 1.0
    set_run_font(p.add_run("智能报价系统 Demo"), size=25, color=INK, bold=True)
    p = doc.add_paragraph()
    p.paragraph_format.space_before = Pt(0)
    p.paragraph_format.space_after = Pt(14)
    set_run_font(p.add_run("已完成能力、基本技术原理与正式版升级建议"), size=14, color=MUTED)
    for label, value in (
        ("用途", "管理层评审、客户沟通与正式版立项准备"),
        ("版本", "V0.1（本地演示版）"),
        ("日期", "2026年8月10日"),
        ("状态", "已完成 Demo 验证；正式版需求待共同确认"),
    ):
        p = doc.add_paragraph()
        p.paragraph_format.space_before = Pt(0)
        p.paragraph_format.space_after = Pt(2)
        set_run_font(p.add_run(label + "："), size=10.5, color=INK, bold=True)
        set_run_font(p.add_run(value), size=10.5, color=INK)
    rule = doc.add_paragraph()
    rule.paragraph_format.space_before = Pt(10)
    rule.paragraph_format.space_after = Pt(10)
    set_paragraph_border_bottom(rule, color="8AA6BF", size="12", space="8")


def build_document():
    doc = Document()
    configure_section(doc.sections[0])
    configure_header_footer(doc.sections[0])
    configure_styles(doc)
    configure_numbering(doc)
    add_title_block(doc)

    add_heading(doc, "一、管理层摘要", 1)
    add_callout(
        doc,
        "结论",
        "当前 Demo 已验证“上传报价表—检索历史报价—匹配候选—人工确认—导出 Excel”的完整闭环。首版采用本机浏览器、本地文件与规则匹配，优先保证数据不外流、结果可解释、每条价格可追溯。正式版的重点不应是立即堆叠大模型，而应先完成历史数据治理、可持续导入、权限审计和大容量检索。",
        color=GREEN,
    )
    add_body(doc, "本次 Demo 的定位是业务与技术可行性验证，不是直接替代生产系统。它证明了现有 Excel 数据可以被抽取为统一的报价记录，并能在新报价表中定位相关历史价格，保留人工确认环节后安全导出。")
    add_bullet(doc, "已验证业务价值：把过去需要跨文件、跨工作表查找的工作，收敛到一个可搜索、可筛选、可回溯的页面。")
    add_bullet(doc, "已验证技术路线：规则匹配可在不调用云端模型的前提下给出候选、分数与来源，适合早期数据质量尚未完全统一的场景。")
    add_bullet(doc, "建议管理决策：同意进入“数据盘点与正式版需求确认”阶段，并以分阶段验收的方式控制范围、工期和报价风险。")

    add_heading(doc, "二、Demo 已完成范围与验证结果", 1)
    add_body(doc, "演示版完全在本机浏览器中运行。历史报价在启动时由两份 Excel 生成本地索引；用户上传待报价的海南清单后，系统在浏览器内完成解析、匹配、确认和导出。文件不会上传到云端，刷新页面后任务状态清空。")
    add_table(
        doc,
        ["项目", "已验证结果"],
        [
            ("历史报价库", "从“普教清单.xlsx”和“高中理化生报价.xlsx”提取 2,491 条单价大于零的历史报价记录，覆盖 1,064 个规范化产品名称。"),
            ("待报价模板", "已验证海南发改委14包和15包的“凯迪”工作表；分别识别约 2,427 条和 1,529 条有效产品行。"),
            ("匹配示例", "“小推车”优先匹配到 270 元的详细历史报价；“直联泵（直联高速旋片式真空泵）”能匹配到 320 元候选。"),
            ("人工控制", "每行最多展示前 5 个候选；允许切换历史记录、人工修改单价、逐行确认或批量确认。"),
            ("Excel 导出", "在原工作表右侧新增 P:U 六列，写入确认单价、金额、品牌、制造商、型号和历史来源；不修改原上传文件。"),
            ("验证情况", "已执行匹配逻辑测试、生产构建测试和两份海南文件的实际导出检查；原始上传文件哈希保持不变。"),
        ],
        [2700, 6660],
        header_fill=LIGHT_BLUE,
    )
    add_body(doc, "Demo 的刻意限制：只支持当前 `.xlsx` 模板；不支持旧版 `.xls`、多人账号、任务持久化、云端部署、ERP 对接和数十 GB 的历史数据直接检索。这些不是功能缺陷，而是为快速验证业务闭环而设置的边界。")

    add_heading(doc, "三、系统如何工作", 1)
    add_body(doc, "系统采用“先找证据、再给建议、最后由人确认”的方法。它不会直接生成或决定报价，而是从历史报价中找出相关记录，给出排序和依据，再由业务人员确定最终单价。")
    add_callout(doc, "业务流程", "历史 Excel → 数据清洗与标准化 → 历史索引；待报价 Excel → 产品行识别 → 候选匹配与评分 → 歧义提示 → 人工确认 → 生成新的报价 Excel。")

    add_heading(doc, "3.1 数据抽取与标准化", 2)
    add_body(doc, "系统从不同 Excel 工作表中提取产品名称、参数、产品编码、型号、品牌、制造商、单位、数量、单价、金额及来源位置。随后对文本做标准化处理，例如统一全角半角字符、空格、常见标点和大小写，从而减少“写法不同但实际为同一产品”的情况。")
    add_body(doc, "每一条历史记录保留来源文件名、工作表名和原始行号。这个设计使业务人员能够随时回到原始 Excel 核对，而不是只能相信系统给出的结果。")

    add_heading(doc, "3.2 候选检索与评分", 2)
    add_body(doc, "系统优先在产品名称完全一致的历史记录中排序；只有没有同名记录时，才从全部历史库进行中文字符二元组相似度匹配。二元组相似度可以理解为：把两个名称分别拆成连续的两个字组合，计算两者重合程度，适合处理轻微错别字、空格或描述差异。")
    add_table(
        doc,
        ["比较字段", "最高分值", "业务含义"],
        [
            ("产品名称", "完全一致 50；模糊匹配最高 40", "名称是候选范围的首要依据；同名优先可避免被大量相似名称干扰。"),
            ("产品编码", "15", "若编码一致，通常是较强的产品身份线索。"),
            ("型号", "10", "型号一致可帮助区分同名但不同规格的产品。"),
            ("参数", "完全一致 15；相似最高 10", "参数用于识别配置和性能差异。"),
            ("品牌、制造商、单位", "各 5", "作为辅助证据，减少跨品牌或跨单位的误匹配。"),
        ],
        [2200, 1800, 5360],
        header_fill=LIGHT_BLUE,
        alignments=[WD_ALIGN_PARAGRAPH.LEFT, WD_ALIGN_PARAGRAPH.CENTER, WD_ALIGN_PARAGRAPH.LEFT],
    )
    add_body(doc, "总分最高为 100 分。若多个候选得分相近，系统继续比较参数是否完全一致、型号是否完全一致、历史记录的详细程度及原始行号，以得到稳定的排序。")

    add_heading(doc, "3.3 歧义提示与人工确认", 2)
    add_body(doc, "当排名第一和第二的候选价格不同，且两者分数相差不足 8 分时，系统会标记“存在歧义”。这意味着数据证据不足以自动区分两个价格，应优先由报价人员查看参数、品牌和来源，而不是让系统暗中做最终选择。")
    add_bullet(doc, "预选最高分候选只是减少操作量，默认状态仍为“待确认”。")
    add_bullet(doc, "切换候选或手动改价后，该行重新进入待确认状态。")
    add_bullet(doc, "导出只写入已确认的产品行；未确认行保持空白并以黄色提示。")

    add_heading(doc, "四、当前技术方案与价值边界", 1)
    add_table(
        doc,
        ["技术组件", "当前用途", "通俗解释"],
        [
            ("React + TypeScript", "浏览器单页界面", "React 用于组织页面和交互；TypeScript 为数据字段增加类型约束，降低开发中把名称、价格或数量用错的风险。"),
            ("ExcelJS", "读取、解析与导出 `.xlsx`", "用于在不破坏原有工作表、样式和合并单元格的前提下，读取数据并写入报价结果。"),
            ("本地 JSON 索引", "保存演示历史报价库", "JSON 是轻量的结构化文本格式。演示版把历史记录预处理为本地文件，使浏览器可以快速加载和搜索。"),
            ("规则匹配引擎", "候选筛选、打分、排序和歧义提示", "把业务人员可理解的判断依据写成明确规则，结果可解释、可复核，也便于后续按客户规则调整。"),
            ("自动化测试", "验证核心匹配、构建和导出", "通过固定样例反复验证关键逻辑，避免后续修改时破坏已验证的匹配结果。"),
        ],
        [2200, 2200, 4960],
        header_fill=LIGHT_BLUE,
    )
    add_callout(doc, "为什么首版没有直接使用大模型", "报价匹配首先需要可追溯和可控。当前规则方法能明确说明“为何推荐这个价格”，没有模型调用成本，也不会把客户文件发送到外部。大模型或语义模型适合在正式版处理别名、长描述和跨模板差异，但应作为增强能力，不应取代人工定价责任。", color=GOLD)

    add_heading(doc, "五、正式版需求手册", 1)
    add_body(doc, "正式版的目标是让系统能够长期、多人、安全地使用，并在数十 GB 历史数据规模下稳定检索。以下优先级以“先可用、可管、可验收”为原则，不建议把所有高级功能一次性打包。")
    add_table(
        doc,
        ["优先级", "需求模块", "正式版核心要求"],
        [
            ("P0", "数据接入与治理", "支持批量导入历史 Excel/CSV 或数据库数据；记录文件版本、导入时间、来源和错误行；建立字段映射、去重、价格状态与税率等数据规则。"),
            ("P0", "历史检索与匹配", "支持名称、编码、型号、参数、品牌、制造商等组合查询；保留 Top 5 候选、评分依据、歧义提示和来源追溯。"),
            ("P0", "报价任务闭环", "支持上传模板、批量处理、人工确认、手动改价、导出及历史任务回看；明确未确认产品的处理规则。"),
            ("P0", "用户、权限与审计", "区分管理员、报价人员、审核人员等角色；记录谁在何时确认或修改了哪条价格，满足内部追责和复盘需要。"),
            ("P0", "模板与导出管理", "将当前已验证模板固化为配置；新增模板必须经过解析、导出和样式保留测试后再上线。"),
            ("P1", "质量与价格分析", "增加低置信度队列、重复产品提示、异常价格提示、历史价格区间和趋势展示，辅助管理人员复核。"),
            ("P1", "ERP/采购系统接口", "在确认业务主数据、接口权限和责任边界后，同步产品编码、供应商、采购订单或审批状态。"),
            ("P2", "语义检索与智能助手", "针对别名、长描述和跨模板写法，引入语义向量检索与大模型解释；输出仍必须带来源和人工确认入口。"),
        ],
        [1200, 2100, 6060],
        header_fill=LIGHT_BLUE,
        alignments=[WD_ALIGN_PARAGRAPH.CENTER, WD_ALIGN_PARAGRAPH.LEFT, WD_ALIGN_PARAGRAPH.LEFT],
    )

    add_heading(doc, "5.1 正式版关键业务规则", 2)
    add_numbered(doc, "价格定义：客户需要明确“历史报价、采购价、中标价、成交价”分别如何使用；含税口径、税率、运输费、安装费和币种必须可区分。")
    add_numbered(doc, "最终决策：系统结果应定义为“推荐参考”，最终报价必须由客户授权人员确认；系统不承担自动定价或交易承诺责任。")
    add_numbered(doc, "模板范围：要列出首期承诺支持的文件类型、工作表和表头规则。新增模板、严重不规则表格或旧版 `.xls` 应作为需求变更单独评估。")
    add_numbered(doc, "验收口径：双方共同准备一批已人工标注的测试样本，约定 Top 1/Top 5 命中率、人工复核范围、导出字段与响应时间，避免以个别案例替代整体验收。")
    add_numbered(doc, "数据责任：客户负责提供合法、完整、可使用的源数据；乙方负责按约定规则处理、保护数据并对系统缺陷进行修复。")

    add_heading(doc, "六、正式版推荐升级架构", 1)
    add_body(doc, "当历史数据达到数十 GB 时，不能继续让浏览器加载全部报价记录。正式版应把“界面、业务服务、数据库、文件存储、检索与审计”分层部署。当前的页面交互和匹配对象可以继续复用，重点替换数据加载和任务处理方式。")
    add_table(
        doc,
        ["层级", "推荐技术方向", "解决的问题"],
        [
            ("前端", "保留 React + TypeScript 页面", "继续复用查询、候选确认和导出体验；通过接口读取数据，不再把全量历史库下载到浏览器。"),
            ("业务服务", "FastAPI（Python Web 框架）", "提供安全的查询、导入、匹配、导出和权限接口；把业务规则集中管理，便于后续修改。"),
            ("关系数据库", "PostgreSQL", "保存规范化的产品、价格、任务、用户和审计记录；支持事务、权限和可靠备份。"),
            ("文件存储", "对象存储或客户内网文件服务", "保存原始上传文件、导入版本和导出结果，避免文件散落在个人电脑。"),
            ("检索增强", "全文检索/相似度检索；后续可用 pgvector", "先解决型号、名称和参数的快速检索；再用向量检索理解别名和长描述，提高召回率。"),
            ("异步任务", "后台队列与任务状态", "将大文件导入、批量匹配和导出放到后台执行，避免用户等待页面卡住，并保留失败重试记录。"),
            ("安全与运维", "角色权限、操作审计、备份、监控与私有部署", "保证谁能看、谁能改有记录；保证数据可恢复、系统异常可发现。"),
        ],
        [1600, 2800, 4960],
        header_fill=LIGHT_BLUE,
    )
    add_body(doc, "语义向量检索是把产品文字转换成一组数字特征，并用“语义距离”找相近描述的技术。它适合解决“同一产品不同叫法”的问题。建议在积累人工确认记录后上线，并且与现有字段规则共同使用：规则保证精确身份约束，向量检索提高候选召回范围。")

    add_heading(doc, "七、分阶段升级建议", 1)
    add_numbered(doc, "阶段 A：数据盘点与需求确认。梳理历史数据来源、规模、价格口径、模板种类、用户角色与部署环境；共同制作验收样本和需求清单。")
    add_numbered(doc, "阶段 B：生产基础版。建设后端服务、数据库、文件存储、账号权限、审计记录和当前模板的稳定导入导出；实现历史任务保存。")
    add_numbered(doc, "阶段 C：检索增强与质量运营。增加多模板配置、异常价格提示、低置信度队列、数据质量报表和语义检索。")
    add_numbered(doc, "阶段 D：系统集成与智能辅助。按需接入 ERP/采购系统、审批流和供应商主数据；审慎引入大模型做描述归纳、规则解释和人工复核辅助。")
    add_callout(doc, "实施原则", "每一阶段都单独定义交付物、验收数据、工期、报价和变更规则。这样既能快速产生业务价值，也能避免在数据质量和接口范围未明确前一次性承诺过多。", color=GREEN)

    add_heading(doc, "八、立项与签约前需确认的事项", 1)
    confirmation_rows = [
        ("数据范围", "历史数据的总容量、文件数量、格式、模板数量、质量问题和合法使用权；是否包含最终成交价或仅为历史报价。"),
        ("价格口径", "含税/未税、税率、币种、运输安装服务费、时间范围、区域和供应商差异如何处理。"),
        ("首期边界", "首期支持哪些模板、哪些部门、多少用户、哪些角色；不在首期范围的内容如何定义为变更。"),
        ("部署与安全", "客户内网、私有云还是公有云；是否允许联网；账号体系、日志、备份、保密和数据保留要求。"),
        ("验收标准", "固定验收样本、匹配指标、人工复核比例、导出格式、处理时长和客户验收反馈期限。"),
        ("接口与维护", "ERP/采购系统接口是否可用、谁提供文档和测试环境；免费维护期、故障响应和新增需求计费规则。"),
        ("责任边界", "系统是辅助决策工具；客户授权人员对最终报价负责。客户提供错误数据、接口异常或延迟配合导致的影响应有顺延和免责约定。"),
    ]
    add_table(doc, ["确认主题", "需要形成书面结论的内容"], confirmation_rows, [2700, 6660], header_fill=LIGHT_BLUE)

    add_heading(doc, "九、术语说明", 1)
    terms = [
        ("前端", "用户在浏览器中直接看到和操作的页面，例如搜索框、候选列表和导出按钮。"),
        ("后端", "运行在服务器上的业务程序，负责保存数据、执行权限校验、处理大任务并向前端提供结果。"),
        ("数据库", "按字段、关系和权限长期保存大量数据的系统；它与简单 Excel 文件相比更适合多人查询、审计和备份。"),
        ("API", "系统之间约定的数据接口。例如 ERP 通过 API 向报价系统提供产品编码或读取审核状态。"),
        ("规则匹配", "把名称、型号、参数等比较逻辑明确写成可检查的规则，并根据权重得到分数。"),
        ("语义检索", "不只比较字面文字，而是识别描述含义的相近程度，适合处理别名、简称和长描述。"),
        ("向量/向量检索", "把文字转换为数字特征后进行相似度搜索的技术，是语义检索的一种常见实现。"),
        ("人工在环", "系统推荐、人员确认的控制模式。它让自动化承担重复劳动，人保留最终商业判断。"),
        ("审计日志", "系统自动记录谁在何时进行了何种操作，便于追责、复盘和合规检查。"),
        ("对象存储", "专门保存大量文件的存储方式，适合管理原始 Excel、导出文件和文件版本。"),
    ]
    add_table(doc, ["术语", "本项目中的含义"], terms, [2700, 6660], header_fill=LIGHT_GRAY)

    add_heading(doc, "十、建议的下一步", 1)
    add_body(doc, "建议先与客户完成一次“数据与需求澄清会”，以当前 Demo 的实际匹配结果为基础，共同确认首期范围。会后形成三份可签约材料：需求规格说明、验收样本与指标、项目实施与报价方案。")
    add_bullet(doc, "将当前四个 Excel 之外的代表性数据样本提供给项目组，用于评估模板差异和数据质量。")
    add_bullet(doc, "由业务负责人确认价格口径、最终审核角色和首期上线范围。")
    add_bullet(doc, "以 P0 功能为基础拆分正式版项目；ERP、语义检索和大模型能力作为后续可选阶段。")

    doc.core_properties.title = "智能报价系统 Demo 技术报告与正式版需求手册"
    doc.core_properties.subject = "管理层沟通材料"
    doc.core_properties.author = "项目组"
    doc.core_properties.comments = "基于本地智能报价 Demo 的已验证结果整理"
    OUT.parent.mkdir(parents=True, exist_ok=True)
    doc.save(OUT)
    print(OUT)


if __name__ == "__main__":
    build_document()
