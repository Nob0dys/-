import fs from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import ExcelJS from "exceljs";

const scriptDir = path.dirname(fileURLToPath(import.meta.url));
const projectRoot = path.resolve(scriptDir, "..");
const dataRoot = path.resolve(projectRoot, "../测试数据");
const outputPath = path.resolve(projectRoot, "public/demo/history.json");

function text(value) {
  if (value == null) return "";
  if (typeof value === "object") {
    if (Array.isArray(value.richText)) return value.richText.map((part) => part.text ?? "").join("");
    if (value.result != null) return text(value.result);
    if (value.text != null) return text(value.text);
  }
  return String(value).replace(/\s+/g, " ").trim();
}

function numberValue(value) {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (value && typeof value === "object" && value.result != null) return numberValue(value.result);
  const cleaned = text(value).replace(/[,，￥¥]/g, "");
  return /^-?\d+(?:\.\d+)?$/.test(cleaned) ? Number(cleaned) : null;
}

function normalizeText(value) {
  return String(value ?? "")
    .normalize("NFKC")
    .toLowerCase()
    .replace(/[\s\u3000·•，,。.;；:：()（）[\]【】<>《》“”"'‘’/\\_—–-]+/g, "");
}

function recordFromFields(fields) {
  return {
    ...fields,
    normalizedName: normalizeText(fields.name),
    normalizedSpec: normalizeText(fields.spec),
    normalizedProductCode: normalizeText(fields.productCode),
    normalizedModel: normalizeText(fields.model),
    normalizedBrand: normalizeText(fields.brand),
    normalizedManufacturer: normalizeText(fields.manufacturer),
    normalizedUnit: normalizeText(fields.unit),
  };
}

function rowValues(worksheet, rowNumber) {
  const row = worksheet.getRow(rowNumber);
  return Array.from({ length: Math.max(worksheet.columnCount, 20) }, (_, index) =>
    text(row.getCell(index + 1).value),
  );
}

function findHeaderRow(worksheet, names) {
  const limit = Math.min(12, worksheet.rowCount);
  for (let rowNumber = 1; rowNumber <= limit; rowNumber += 1) {
    const values = rowValues(worksheet, rowNumber);
    if (values.some((value) => names.includes(value))) return rowNumber;
  }
  return -1;
}

function headerMap(worksheet, rowNumber) {
  const values = rowValues(worksheet, rowNumber);
  const find = (...names) => {
    for (const name of names) {
      const index = values.findIndex((value) => value === name);
      if (index >= 0) return index + 1;
    }
    return -1;
  };
  return {
    name: find("产品名称", "采购品目"),
    productCode: find("产品编码"),
    spec: find("参数"),
    quantity: find("数量"),
    unit: find("单位"),
    model: find("规格型号", "型号"),
    brand: find("品牌"),
    manufacturer: find("制造商名称", "制造商"),
    price: find("含税单价", "赛特尔单价", "单价"),
    amount: find("金额"),
    quoteDate: find("报价日期", "日期", "询价日期"),
  };
}

function getValue(row, columnNumber) {
  return columnNumber > 0 ? row.getCell(columnNumber).value : null;
}

async function readWorkbook(fileName) {
  const workbook = new ExcelJS.Workbook();
  await workbook.xlsx.readFile(path.join(dataRoot, fileName));
  return workbook;
}

async function extractGeneralEducation() {
  const fileName = "普教清单.xlsx";
  const workbook = await readWorkbook(fileName);
  const records = [];

  for (const worksheet of workbook.worksheets) {
    const headerRow = findHeaderRow(worksheet, ["产品名称"]);
    if (headerRow < 0) continue;
    const columns = headerMap(worksheet, headerRow);
    for (let rowNumber = headerRow + 1; rowNumber <= worksheet.rowCount; rowNumber += 1) {
      const row = worksheet.getRow(rowNumber);
      const name = text(getValue(row, columns.name));
      const price = numberValue(getValue(row, columns.price));
      if (!name || price == null || price <= 0) continue;
      records.push(
        recordFromFields({
          id: `${fileName}:${worksheet.name}:${rowNumber}`,
          sourceFile: fileName,
          sourceSheet: worksheet.name,
          sourceRow: rowNumber,
          name,
          spec: text(getValue(row, columns.spec)),
          productCode: text(getValue(row, columns.productCode)),
          model: text(getValue(row, columns.model)),
          brand: text(getValue(row, columns.brand)),
          manufacturer: text(getValue(row, columns.manufacturer)),
          unit: text(getValue(row, columns.unit)),
          quantity: numberValue(getValue(row, columns.quantity)),
          price,
          amount: numberValue(getValue(row, columns.amount)),
          quoteDate: text(getValue(row, columns.quoteDate)),
          sourcePriority: 1,
        }),
      );
    }
  }
  return records;
}

async function extractHighSchoolQuotation() {
  const fileName = "高中理化生报价.xlsx";
  const workbook = await readWorkbook(fileName);
  const worksheet = workbook.getWorksheet("凯迪");
  if (!worksheet) throw new Error(`${fileName} 缺少“凯迪”工作表`);
  const headerRow = findHeaderRow(worksheet, ["采购品目"]);
  if (headerRow < 0) throw new Error(`${fileName} 未找到采购品目表头`);
  const columns = headerMap(worksheet, headerRow);
  const records = [];

  for (let rowNumber = headerRow + 1; rowNumber <= worksheet.rowCount; rowNumber += 1) {
    const row = worksheet.getRow(rowNumber);
    const name = text(getValue(row, columns.name));
    const price = numberValue(row.getCell(16).value);
    if (!name || price == null || price <= 0) continue;
    const outputBrand = text(row.getCell(18).value);
    const outputManufacturer = text(row.getCell(19).value);
    const outputModel = text(row.getCell(20).value);
    records.push(
      recordFromFields({
        id: `${fileName}:${worksheet.name}:${rowNumber}`,
        sourceFile: fileName,
        sourceSheet: worksheet.name,
        sourceRow: rowNumber,
        name,
        spec: text(getValue(row, columns.spec)),
        productCode: text(getValue(row, columns.productCode)),
        model: outputModel || text(row.getCell(11).value),
        brand: outputBrand || text(row.getCell(12).value),
        manufacturer: outputManufacturer || text(row.getCell(13).value),
        unit: text(getValue(row, columns.unit)),
        quantity: numberValue(getValue(row, columns.quantity)),
        price,
        amount: numberValue(row.getCell(17).value),
        quoteDate: text(getValue(row, columns.quoteDate)),
        sourcePriority: 2,
      }),
    );
  }
  return records;
}

const records = [...(await extractGeneralEducation()), ...(await extractHighSchoolQuotation())];
const sourceCounts = new Map();
for (const record of records) {
  const key = `${record.sourceFile}::${record.sourceSheet}`;
  sourceCounts.set(key, (sourceCounts.get(key) ?? 0) + 1);
}
const sources = [...sourceCounts.entries()].map(([key, count]) => {
  const [file, sheet] = key.split("::");
  return { file, sheet, count };
});
const payload = {
  generatedAt: new Date().toISOString(),
  recordCount: records.length,
  uniqueNameCount: new Set(records.map((record) => record.normalizedName)).size,
  sources,
  records,
};

await fs.mkdir(path.dirname(outputPath), { recursive: true });
await fs.writeFile(outputPath, `${JSON.stringify(payload)}\n`, "utf8");
console.log(
  `历史报价数据已生成：${payload.recordCount} 条记录，${payload.uniqueNameCount} 个产品名称。`,
);
