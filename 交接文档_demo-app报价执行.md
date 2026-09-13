# 交接文档：用 demo-app 现成管线完成两份报价

> 任务：对 `20260911科学仪器.xlsx`、`龙岩.xlsx` 报价。
> 要求：使用 **demo-app 现成报价管线**（后端 API：上传 → 匹配 → 导出），不要再用自写拼列脚本。
> 报价口径：原单价 + 10% 含税价（导出自动计算，税率 0.10）。

---

## 0. 输入与输出

| | 文件 |
|---|---|
| 输入 B | `E:\东方理工\个人中小型项目\智能报价系统_完整代码包\生产数据\20260911科学仪器.xlsx`（4 段，749 个产品行） |
| 输入 C | `E:\东方理工\个人中小型项目\智能报价系统_完整代码包\生产数据\龙岩.xlsx`（2 段，114 个产品行） |
| 输出 | 建议另存到 `生产数据\` 下：`20260911科学仪器_内部复核版.xlsx`、`龙岩_内部复核版.xlsx`（客户版可选） |

管线组件（已存在于代码包，直接调用即可）：

| 环节 | 代码 |
|---|---|
| 解析 + 匹配 + 默认选方案 | `demo-app\backend\app\services.py` → `process_job()` |
| 在原文件上填价导出 | `demo-app\backend\app\excel_service.py` → `create_export()` |
| 触发方式 | 后端 API：`POST /api/quote-jobs` → `POST /api/quote-jobs/{id}/auto-confirm-and-export/internal`（或 `customer`） |

---

## 1. 环境

- Python 3.13（已装 fastapi / uvicorn / sqlalchemy / openpyxl / xlrd / requests）。
- 数据库：`demo-app\backend\data\quote_saitel.db`（**仅赛特尔25年，4131 条**；不要用 `quote.db`）。
- 端口：127.0.0.1:8000。

---

## 2. 启动后端（窗口 1）

```powershell
cd "E:\东方理工\个人中小型项目\智能报价系统_完整代码包\demo-app\backend"
$env:DATABASE_URL = "sqlite:///./data/quote_saitel.db"
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

健康检查：浏览器打开 `http://127.0.0.1:8000/api/health` → `{"status":"ok","service":"quote-api"}`

（可选：前端 UI 方式，另一个窗口 `cd demo-app; npm run dev`，访问 http://localhost:3000 ，账号 admin/admin123，走“新建报价→上传→一键导出”。）

---

## 3. 上传并导出（窗口 2）

对 B、C 各执行一次下面的 Python 片段（`FILE`/`OUT` 换成本文件路径即可）：

```python
import time
import requests

BASE = "http://127.0.0.1:8000"
s = requests.Session()
s.post(f"{BASE}/api/auth/login", json={"username": "admin", "password": "admin123"}).raise_for_status()

FILE = r"E:\东方理工\个人中小型项目\智能报价系统_完整代码包\生产数据\20260911科学仪器.xlsx"
OUT  = r"E:\东方理工\个人中小型项目\智能报价系统_完整代码包\生产数据\20260911科学仪器_内部复核版.xlsx"

with open(FILE, "rb") as f:
    r = s.post(
        f"{BASE}/api/quote-jobs",
        files={"file": (FILE.rsplit("\\", 1)[-1], f)},
        data={"requested_option_count": "1", "tax_rate": "0.10"},  # 方案数1；填3会多出 报价2/报价3 列
    )
r.raise_for_status()
jid = r.json()["id"]
print("job:", jid)

while True:  # 等待匹配完成（约 1~6 秒）
    st = s.get(f"{BASE}/api/quote-jobs/{jid}").json()
    print(st["status"], st["progress"])
    if st["status"] not in ("queued", "reserved", "matching"):
        break
    time.sleep(2)

resp = s.post(f"{BASE}/api/quote-jobs/{jid}/auto-confirm-and-export/internal")
resp.raise_for_status()
open(OUT, "wb").write(resp.content)
print("saved:", OUT)
```

- **内部复核版**：用 `.../auto-confirm-and-export/internal`（带 匹配状态/匹配分/风险提示/历史来源 + 「待复核清单」sheet）。
- **客户版**：把 `internal` 换成 `customer`（不含内部列）。
- 一个任务可先导出一版，再改 URL 导另一版，无需重新上传。
- 备注：`auto-confirm-and-export` 会自动确认所有“有默认选中方案”的行（估算价行不确认），随后导出。

---

## 4. 导出后必做：修复被清空的段表头单元格

demo-app 导出会把“段表头行”的单位/数量单元格清空，导出后执行一次本修复：

```python
import openpyxl

# B：第 44/71/240 行是段表头（序号|名称|规格|单位|数量）
p = r"E:\东方理工\个人中小型项目\智能报价系统_完整代码包\生产数据\20260911科学仪器_内部复核版.xlsx"
wb = openpyxl.load_workbook(p)
ws = wb["Sheet1"]
for r in (44, 71, 240):
    ws.cell(r, 4, "单位")
    ws.cell(r, 5, "数量")
wb.save(p)

# C：第 75 行是段表头（序号|器材名称|规格|品牌型号|参数|单 位|数量|单价|总计）
p2 = r"E:\东方理工\个人中小型项目\智能报价系统_完整代码包\生产数据\龙岩_内部复核版.xlsx"
wb2 = openpyxl.load_workbook(p2)
ws2 = wb2["Sheet1"]
ws2.cell(75, 6, "单 位")
ws2.cell(75, 7, "数量")
wb2.save(p2)
print("段表头已修复")
```

---

## 5. 导出内容说明（现成管线自动产出）

**20260911科学仪器（B）** 追加列（共 18 列）：
`… 数量 | 单价(确认单价) | 品牌 | 型号 | 规格 | 税后单价 | 总价 | 税后总价 | 制造商 | 产品编码 | 匹配状态 | 匹配分 | 风险提示 | 历史来源`
（原「单价/品牌/型号/单位/数量」列直接复用；税后=×1.1；总价=确认单价×数量；税后总价=税后单价×数量）

**龙岩（C）** 追加列（共 20 列）：
`… 数量 | 单价(确认单价) | 总计 | 税后单价 | 总价 | 税后总价 | 品牌 | 制造商 | 型号 | 产品编码 | 匹配状态 | 匹配分 | 风险提示 | 历史来源`
（原「总计」列不复用，系统另加「总价」列填数）

两份都新增 sheet：
- **「多方案报价明细」**：含 `规格(赛特尔参数)、产品编码、报价、税后单价、小计、税后总价、历史来源` 等，供“复查参数和价格”。
- **「待复核清单」**：未匹配/低置信行清单。

**我试跑实测**：B 解析 752 行、696 行有价、导出 18 列+2 sheet；C 解析 115 行、102 行有价、导出 20 列+2 sheet。抽样核对：确认单价=赛特尔原价、税后=×1.1、总价/税后总价=×数量，均正确。

---

## 6. 输出前复查（脚本）

```python
import openpyxl, re

def check(path, price_col, tax_col, total_col, taxtotal_col, qty_col, ncols, title):
    ws = openpyxl.load_workbook(path, data_only=True).active
    bad = 0
    n = 0
    for r in range(1, ws.max_row + 1):
        a = ws.cell(r, 1).value
        if a is None or not re.fullmatch(r"\d+", str(a).strip()) or not ws.cell(r, 2).value:
            continue
        p, t = ws.cell(r, price_col).value, ws.cell(r, tax_col).value
        if p in (None, ""):
            continue
        n += 1
        q = ws.cell(r, qty_col).value
        tot, tt = ws.cell(r, total_col).value, ws.cell(r, taxtotal_col).value
        if abs(float(t) - round(float(p) * 1.1, 2)) > 0.011: bad += 1; print("税后单价错", r, p, t)
        if q not in (None, "") and abs(float(tot) - round(float(p) * float(q), 2)) > 0.011: bad += 1; print("总价错", r, p, q, tot)
        if q not in (None, "") and abs(float(tt) - round(float(t) * float(q), 2)) > 0.011: bad += 1; print("税后总价错", r, t, q, tt)
    print(f"{title}: 有价{n}行, 错误{bad}")

check(r"...20260911科学仪器_内部复核版.xlsx", 6, 10, 11, 12, 5, 18, "B")
check(r"...龙岩_内部复核版.xlsx", 8, 10, 11, 12, 7, 20, "C")
```

补充人工复查项：
1. 原表列（B 的 A–E、C 的 A–G）与原件逐格一致（除第4步修复的段表头行外，导出不改原列）。
2. 用「多方案报价明细」的 `规格/产品编码` 与询价要求比对（参数复查）。
3. 「待复核清单」逐行处理未匹配/低置信项。

---

## 7. 注意事项

1. **只能连 `quote_saitel.db`**（赛特尔专库），确保报价基于且只基于赛特尔25年。
2. **上传原始文件**，不要改表头/结构；段表头修复是导出后再做（见第4步）。
3. 未匹配行：价格列留空，只会出现在「待复核清单」里，价格需另行处理。
4. 品牌/制造商多为空：赛特尔价目本无品牌字段；`型号`=记录型号（多为空），**5/11 位新课标/老课标编号在「产品编码」列**。
5. 旧的手写产物 `生产数据\20260911科学仪器_报价.xlsx`、`生产数据\龙岩_报价.xlsx` **作废**（缺 税后总价/总价/产品编码/制造商/明细sheet 等）。
6. 导出原始文件同时保存在 `demo-app\backend\data\exports\{job_id}\` 下，任务记录可在任务列表查询。
7. 试跑用的临时验证文件在 `C:\Users\admin\AppData\Local\Temp\opencode\export_B_internal.xlsx / export_C_internal.xlsx`，可对照格式。
