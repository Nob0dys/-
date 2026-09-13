"use client";

import { type FormEvent, type ReactNode, useCallback, useEffect, useRef, useState } from "react";

type Role = "admin" | "quote";
type ActiveView = "tasks" | "new" | "customers" | "history" | "governance" | "accounts" | "databases";
type RowFilter = "all" | "pending" | "suggested" | "review" | "unmatched" | "unit_conflict" | "parameter_conflict" | "price_anomaly" | "score_tie" | "low_confidence" | "confirmed";
type LineSort = "score_asc" | "score_desc" | "source_row";

type User = { id: number; username: string; display_name: string; role: Role };
type AccountUser = { id: number; username: string; display_name: string; role: Role; active: boolean; created_at: string };
type Requirement = {
  id: number;
  attribute_name: string;
  operator: string;
  value: string;
  unit: string;
  required: boolean;
  notes: string;
};
type Customer = {
  id: number;
  name: string;
  customer_type: "ordinary" | "special" | "vip";
  discount_percent: number;
  minimum_margin_percent: number;
  preferred_manufacturers: string[];
  notes: string;
  created_at: string;
  requirements: Requirement[];
  price_sheet_count: number;
};
type Job = {
  id: string;
  file_name: string;
  display_name: string;
  status: string;
  progress: number;
  requested_option_count: number;
  tax_rate: number;
  total_lines: number;
  matched_lines: number;
  review_lines: number;
  unmatched_lines: number;
  confirmed_lines: number;
  error_message: string;
  created_at: string;
  updated_at: string;
  customer: Customer | null;
  created_by: User;
};
type HistoryRecord = {
  id: string;
  name: string;
  spec: string;
  model: string;
  brand: string;
  manufacturer: string;
  unit: string;
  price: number;
  quote_date: string;
  source_file?: string;
  source_sheet?: string;
  source_row?: number;
};
type QuoteOption = {
  id: number;
  rank: number;
  score: number;
  confidence: string;
  component_scores: Record<string, number>;
  reasons: string[];
  warnings: string[];
  unit_status: string;
  normalized_price: number | null;
  selected: boolean;
  final_price: number;
  manual_note: string;
  record: HistoryRecord;
};
type QuoteLine = {
  id: number;
  source_row: number;
  sheet_name: string;
  name: string;
  spec: string;
  product_code: string;
  model: string;
  brand: string;
  manufacturer: string;
  unit: string;
  quantity: number | null;
  pricing_quantity: number | null;
  status: string;
  confidence: string;
  recommended_score: number;
  warnings: string[];
  confirmed: boolean;
  selected_option_count: number;
  primary_option: QuoteOption | null;
  options?: QuoteOption[];
};
type PageResult = { items: QuoteLine[]; total: number; page: number; page_size: number; page_count: number };
type Governance = {
  record_count: number;
  unique_name_count: number;
  duplicate_name_groups: number;
  unit_conflict_groups: number;
  price_conflict_groups: number;
  missing_manufacturer_count: number;
  audit_event_count: number;
  matching_weights: Record<string, number>;
};
type HistoryResult = HistoryRecord & { source: string };
type DatabaseInfo = { name: string; exists: boolean; size_mb: number; history_count: number; job_count: number };
type DatabaseList = { current: string; databases: DatabaseInfo[] };

const STATUS_COPY: Record<string, string> = {
  queued: "排队中",
  reserved: "准备处理",
  matching: "匹配中",
  review: "待复核",
  confirmed: "已确认",
  exported: "已导出",
  failed: "处理失败",
  suggested: "建议匹配",
  unmatched: "无可靠匹配",
  pending: "待确认",
};

const CUSTOMER_COPY = { ordinary: "普通客户", special: "特殊要求", vip: "VIP客户" };
const ATTRIBUTE_SUGGESTIONS = ["产品名称", "型号", "制造商", "品牌", "参数", "单位"];
const CUSTOMER_GROUPS: Array<{ type: Customer["customer_type"]; title: string; detail: string }> = [
  { type: "ordinary", title: "普通客户", detail: "标准价与常规策略" },
  { type: "vip", title: "VIP 客户", detail: "协议折扣与最低毛利线" },
  { type: "special", title: "特殊要求客户", detail: "结构化要求逐条校验" },
];
const ROW_FILTER_TABS: Array<[RowFilter, string]> = [
  ["all", "全部"], ["pending", "待确认"], ["suggested", "高置信"], ["review", "需复核"], ["unmatched", "无匹配"], ["unit_conflict", "单位冲突"], ["parameter_conflict", "参数冲突"], ["price_anomaly", "价格异常"], ["score_tie", "同分多价"], ["low_confidence", "低置信"], ["confirmed", "已确认"],
];
const SORT_CYCLE: Record<LineSort, LineSort> = { score_asc: "score_desc", score_desc: "source_row", source_row: "score_asc" };
const SORT_LABEL: Record<LineSort, string> = { score_asc: "评分 ↑", score_desc: "评分 ↓", source_row: "评分 ⇅" };
const CONFIDENCE_COPY: Record<string, string> = {
  high: "高",
  medium: "中",
  low: "低",
  review: "需复核",
  unreliable: "不可靠",
  manual: "手工",
};
const STATUS_HINT: Record<string, string> = {
  review: "有候选但置信度不足或存在阻断风险",
  unmatched: "无候选或最佳匹配低于55分",
};

function optionKey(option: QuoteOption): string {
  return [
    (option.record.manufacturer || option.record.brand).trim(),
    String(option.final_price),
    option.record.spec.trim(),
    option.record.model.trim(),
  ].join("|");
}

function dedupeOptions(options: QuoteOption[]): QuoteOption[] {
  const seen = new Set<string>();
  return options.filter((item) => {
    const key = optionKey(item);
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

function isManualOption(option: QuoteOption): boolean {
  return option.confidence === "manual" || option.record.source_file === "手工方案";
}

class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, { credentials: "include", ...init });
  if (!response.ok) {
    let message = `请求失败（${response.status}）`;
    try {
      const payload = (await response.json()) as { detail?: string };
      if (payload.detail) message = payload.detail;
    } catch {
      // Keep the HTTP fallback message when the response is not JSON.
    }
    throw new ApiError(response.status, message);
  }
  return response.json() as Promise<T>;
}

function money(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return "—";
  return new Intl.NumberFormat("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(value);
}

function formatDate(value: string): string {
  if (!value) return "—";
  return new Intl.DateTimeFormat("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" }).format(new Date(value));
}

function blockingWarnings(warnings: string[]): string[] {
  return warnings.filter((item) => item.startsWith("BLOCK:"));
}

export function QuoteApp() {
  const [user, setUser] = useState<User | null | undefined>(undefined);
  const [activeView, setActiveView] = useState<ActiveView>("tasks");
  const [customers, setCustomers] = useState<Customer[]>([]);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [selectedJobId, setSelectedJobId] = useState<string | null>(null);
  const [governance, setGovernance] = useState<Governance | null>(null);
  const [showPasswordModal, setShowPasswordModal] = useState(false);
  const [notice, setNotice] = useState("");
  const noticeTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const notify = useCallback((message: string) => {
    setNotice(message);
    if (noticeTimer.current) clearTimeout(noticeTimer.current);
    noticeTimer.current = setTimeout(() => setNotice(""), 4200);
  }, []);

  const loadAppData = useCallback(async () => {
    const [customerData, jobData, governanceData] = await Promise.all([
      api<Customer[]>("/api/customers"),
      api<Job[]>("/api/quote-jobs"),
      api<Governance>("/api/governance/summary"),
    ]);
    setCustomers(customerData);
    setJobs(jobData);
    setGovernance(governanceData);
  }, []);

  useEffect(() => {
    api<User>("/api/auth/me")
      .then((value) => {
        setUser(value);
        return loadAppData();
      })
      .catch((error: unknown) => {
        if (error instanceof ApiError && error.status === 401) setUser(null);
        else {
          setUser(null);
          notify(error instanceof Error ? error.message : "系统连接失败");
        }
      });
  }, [loadAppData, notify]);

  const selectedJob = jobs.find((job) => job.id === selectedJobId) ?? null;

  async function handleLogin(username: string, password: string) {
    const loggedIn = await api<User>("/api/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password }),
    });
    setUser(loggedIn);
    await loadAppData();
  }

  async function handleLogout() {
    await api<{ ok: boolean }>("/api/auth/logout", { method: "POST" });
    setUser(null);
    setJobs([]);
    setSelectedJobId(null);
  }

  async function refreshJobs(focusId?: string) {
    const nextJobs = await api<Job[]>("/api/quote-jobs");
    setJobs(nextJobs);
    if (focusId) setSelectedJobId(focusId);
  }

  if (user === undefined) {
    return <div className="boot-screen"><div className="boot-mark">价</div><strong>正在连接报价工作台…</strong></div>;
  }
  if (!user) return <LoginScreen onLogin={handleLogin} />;

  return (
    <main className="app-shell">
      <header className="topbar">
        <div className="brand-block">
          <div className="brand-mark" aria-hidden="true">价</div>
          <div><p className="eyebrow">QUOTATION CONTROL DESK</p><h1>智能报价工作台</h1></div>
        </div>
        <div className="topbar-actions">
          <span className="system-state"><i />服务器数据已同步</span>
          <div className="user-chip"><span>{user.display_name}</span><b>{user.role === "admin" ? "管理员" : "报价员"}</b></div>
          <button className="ghost-button" onClick={() => setShowPasswordModal(true)}>修改密码</button>
          <button className="ghost-button" onClick={handleLogout}>退出</button>
        </div>
      </header>

      <section className="workspace">
        <aside className="sidebar" aria-label="功能导航">
          <nav>
            <NavButton active={activeView === "tasks"} number="01" title="报价任务" detail="进度、复核、导出" onClick={() => setActiveView("tasks")} />
            <NavButton active={activeView === "new"} number="02" title="新建报价" detail="客户、文件、多方案" onClick={() => setActiveView("new")} />
            <NavButton active={activeView === "customers"} number="03" title="客户档案" detail="普通、特殊、VIP" onClick={() => setActiveView("customers")} />
            <NavButton active={activeView === "history"} number="04" title="历史报价" detail="价格、厂家、来源" onClick={() => setActiveView("history")} />
            <NavButton active={activeView === "governance"} number="05" title="数据治理" detail="冲突、权重、审计" onClick={() => setActiveView("governance")} />
            {user.role === "admin" && <NavButton active={activeView === "databases"} number="06" title="数据库管理" detail="新建、切换、导入" onClick={() => setActiveView("databases")} />}
            {user.role === "admin" && <NavButton active={activeView === "accounts"} number="07" title="账号管理" detail="用户、角色、状态" onClick={() => setActiveView("accounts")} />}
          </nav>
          <div className="sidebar-stat">
            <span>历史报价库</span>
            <strong>{governance?.record_count.toLocaleString("zh-CN") ?? "—"}</strong>
            <small>{governance?.unique_name_count.toLocaleString("zh-CN") ?? "—"} 个产品名称</small>
            <div><b>{governance?.unit_conflict_groups ?? "—"}</b><em>单位冲突组</em></div>
          </div>
        </aside>

        <section className="content-area">
          {activeView === "tasks" && !selectedJob && (
            <TaskCenter jobs={jobs} onOpen={(id) => setSelectedJobId(id)} onCreate={() => setActiveView("new")} onChanged={loadAppData} notify={notify} />
          )}
          {activeView === "tasks" && selectedJob && (
            <JobWorkspace
              job={selectedJob}
              onBack={() => setSelectedJobId(null)}
              onRefresh={() => refreshJobs(selectedJob.id)}
              notify={notify}
            />
          )}
          {activeView === "new" && (
            <NewQuote
              customers={customers}
              onCreated={async (job) => {
                await refreshJobs(job.id);
                setActiveView("tasks");
                notify("报价任务已创建，系统正在匹配历史记录。");
              }}
            />
          )}
          {activeView === "customers" && <CustomerDirectory customers={customers} user={user} onUpdated={loadAppData} notify={notify} />}
          {activeView === "history" && <HistorySearch user={user} notify={notify} />}
          {activeView === "governance" && <GovernanceView data={governance} user={user} onChanged={loadAppData} notify={notify} />}
          {activeView === "databases" && user.role === "admin" && <DatabaseManager onChanged={loadAppData} notify={notify} />}
          {activeView === "accounts" && user.role === "admin" && <AccountAdmin notify={notify} />}
        </section>
      </section>

      {showPasswordModal && <ChangePasswordModal onClose={() => setShowPasswordModal(false)} notify={notify} />}
      {notice && <div className="global-toast" role="status">{notice}</div>}
    </main>
  );
}

function LoginScreen({ onLogin }: { onLogin: (username: string, password: string) => Promise<void> }) {
  const [username, setUsername] = useState("admin");
  const [password, setPassword] = useState("admin123");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      await onLogin(username, password);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "登录失败");
    } finally {
      setBusy(false);
    }
  }
  return (
    <main className="login-shell">
      <section className="login-story">
        <div className="brand-mark large">价</div>
        <p className="eyebrow">PRIVATE QUOTATION SYSTEM</p>
        <h1>让每一次报价<br />都有依据，也有边界。</h1>
        <p>参数优先、单位校验、多制造商方案与完整审计，服务内部报价人员的正式工作流。</p>
        <div className="login-features"><span>参数权重 45%</span><span>单位冲突阻断</span><span>任务持久化</span></div>
      </section>
      <form className="login-card" onSubmit={submit}>
        <div><p className="eyebrow">STAFF ACCESS</p><h2>登录工作台</h2><p>仅限公司内部授权人员使用</p></div>
        <label><span>用户名</span><input value={username} onChange={(event) => setUsername(event.target.value)} autoComplete="username" /></label>
        <label><span>密码</span><input type="password" value={password} onChange={(event) => setPassword(event.target.value)} autoComplete="current-password" /></label>
        {error && <div className="form-error">{error}</div>}
        <button className="primary-button large-button" disabled={busy}>{busy ? "正在登录…" : "进入报价工作台"}</button>
        <small>演示账号 admin / admin123；正式部署必须在环境配置中修改默认密码。</small>
      </form>
    </main>
  );
}

function NavButton({ active, number, title, detail, onClick }: { active: boolean; number: string; title: string; detail: string; onClick: () => void }) {
  return <button className={`nav-item ${active ? "active" : ""}`} onClick={onClick}><span>{number}</span><div><strong>{title}</strong><small>{detail}</small></div></button>;
}

function PageHeading({ eyebrow, title, detail, action }: { eyebrow: string; title: string; detail: string; action?: ReactNode }) {
  return <div className="page-heading"><div><p className="eyebrow">{eyebrow}</p><h2>{title}</h2><p>{detail}</p></div>{action}</div>;
}

function TaskCenter({ jobs, onOpen, onCreate, onChanged, notify }: { jobs: Job[]; onOpen: (id: string) => void; onCreate: () => void; onChanged: () => Promise<void>; notify: (message: string) => void }) {
  const activeJobs = jobs.filter((job) => ["queued", "reserved", "matching", "review"].includes(job.status)).length;
  const needReview = jobs.reduce((sum, job) => sum + job.review_lines + job.unmatched_lines, 0);
  const [renamingId, setRenamingId] = useState<string | null>(null);
  const [renameDraft, setRenameDraft] = useState("");

  async function submitRename(job: Job) {
    const value = renameDraft.trim();
    try {
      if (value && value !== (job.display_name || job.file_name)) {
        await api<Job>(`/api/quote-jobs/${job.id}`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ display_name: value }),
        });
        notify("任务已重命名。");
      }
      setRenamingId(null);
      await onChanged();
    } catch (reason) {
      notify(reason instanceof Error ? reason.message : "重命名失败");
    }
  }

  async function removeJob(job: Job) {
    if (!window.confirm(`确定删除任务「${job.display_name || job.file_name}」吗？该任务的全部报价明细将一并删除，且不可恢复。`)) return;
    try {
      await api<{ ok: boolean }>(`/api/quote-jobs/${job.id}`, { method: "DELETE" });
      notify("任务及其明细已删除。");
      await onChanged();
    } catch (reason) {
      notify(reason instanceof Error ? reason.message : "删除失败");
    }
  }

  return (
    <section>
      <PageHeading eyebrow="QUOTE OPERATIONS" title="报价任务" detail="保存每次上传、匹配、复核与导出记录，可随时继续处理。" action={<button className="primary-button" onClick={onCreate}>＋ 新建报价任务</button>} />
      <div className="hero-metrics">
        <Metric label="进行中的任务" value={activeJobs} tone="teal" />
        <Metric label="待人工复核行" value={needReview} tone="amber" />
        <Metric label="累计任务" value={jobs.length} tone="slate" />
      </div>
      <div className="section-title"><div><h3>最近任务</h3><p>服务器会保留处理进度和人工选择</p></div></div>
      {jobs.length === 0 ? (
        <div className="empty-state"><strong>还没有报价任务</strong><span>选择客户并上传一份 .xlsx 询价单开始。</span><button className="primary-button" onClick={onCreate}>创建第一个任务</button></div>
      ) : (
        <div className="job-list">
          {jobs.map((job) => (
            <div className="job-card" key={job.id} role="button" tabIndex={0} onClick={() => onOpen(job.id)} onKeyDown={(event) => { if (event.key === "Enter" && renamingId !== job.id) onOpen(job.id); }}>
              <div className={`job-icon status-${job.status}`}>XLSX</div>
              <div className="job-main">
                {renamingId === job.id ? (
                  <div className="job-rename">
                    <input value={renameDraft} onChange={(event) => setRenameDraft(event.target.value)} onClick={(event) => event.stopPropagation()} onKeyDown={(event) => { if (event.key === "Enter") void submitRename(job); if (event.key === "Escape") setRenamingId(null); }} />
                    <button className="card-action-button" onClick={(event) => { event.stopPropagation(); void submitRename(job); }}>保存</button>
                    <button className="card-action-button" onClick={(event) => { event.stopPropagation(); setRenamingId(null); }}>取消</button>
                  </div>
                ) : (
                  <div><strong>{job.display_name || job.file_name}</strong><StatusPill status={job.status} /></div>
                )}
                <span>{job.customer ? `${job.customer.name} · ${CUSTOMER_COPY[job.customer.customer_type]} · ` : "一次性报价 · "}{formatDate(job.created_at)}</span>
              </div>
              <div className="job-count"><b>{job.total_lines || "—"}</b><span>产品行</span></div>
              <div className="job-count warning"><b>{job.review_lines + job.unmatched_lines || 0}</b><span>需复核</span></div>
              <div className="job-count success"><b>{job.confirmed_lines || 0}</b><span>已确认</span></div>
              <div className="job-card-actions">
                <button className="card-action-button" onClick={(event) => { event.stopPropagation(); setRenamingId(job.id); setRenameDraft(job.display_name || job.file_name); }}>重命名</button>
                <button className="card-action-button danger" onClick={(event) => { event.stopPropagation(); void removeJob(job); }}>删除</button>
              </div>
              <div className="job-arrow">→</div>
              {job.status === "matching" && <div className="job-progress"><i style={{ width: `${job.progress}%` }} /></div>}
            </div>
          ))}
        </div>
      )}
    </section>
  );
}

function Metric({ label, value, tone }: { label: string; value: number; tone: string }) {
  return <div className={`metric-card tone-${tone}`}><span>{label}</span><strong>{value.toLocaleString("zh-CN")}</strong></div>;
}

function StatusPill({ status }: { status: string }) {
  return <span className={`status-pill status-${status}`} title={STATUS_HINT[status]}>{STATUS_COPY[status] ?? status}</span>;
}

function NewQuote({ customers, onCreated }: { customers: Customer[]; onCreated: (job: Job) => void }) {
  const [customerId, setCustomerId] = useState<number | null>(null);
  const [customerGroup, setCustomerGroup] = useState<Customer["customer_type"]>("ordinary");
  const [customerSearch, setCustomerSearch] = useState("");
  const [optionCount, setOptionCount] = useState(() => {
    const saved = Number(window.localStorage.getItem("quote-option-count"));
    return Number.isInteger(saved) && saved >= 1 && saved <= 5 ? saved : 3;
  });
  const [taxPercent, setTaxPercent] = useState(() => {
    const saved = Number(window.localStorage.getItem("quote-tax-rate"));
    return Number.isFinite(saved) && saved >= 0 && saved <= 100 ? saved : 10;
  });
  const [file, setFile] = useState<File | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const selectedCustomer = customers.find((customer) => customer.id === customerId) ?? null;
  const keyword = customerSearch.trim().toLowerCase();
  const groupCustomers = customers.filter((customer) => customer.customer_type === customerGroup && (!keyword || customer.name.toLowerCase().includes(keyword)));
  function pickOptionCount(value: number) {
    setOptionCount(value);
    window.localStorage.setItem("quote-option-count", String(value));
  }
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!file) return;
    setBusy(true);
    setError("");
    try {
      const data = new FormData();
      if (customerId != null) data.set("customer_id", String(customerId));
      data.set("requested_option_count", String(optionCount));
      data.set("tax_rate", String(taxPercent / 100));
      data.set("file", file);
      const response = await api<Job>("/api/quote-jobs", { method: "POST", body: data });
      onCreated(response);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "任务创建失败");
    } finally {
      setBusy(false);
    }
  }
  return (
    <section>
      <PageHeading eyebrow="NEW QUOTATION" title="新建报价任务" detail="先确定客户策略，再上传询价单。匹配工作在服务器后台完成。" />
      <form className="new-quote-layout" onSubmit={submit}>
        <div className="form-card">
          <div className="step-label"><b>01</b><span>选择客户</span></div>
          <div className="customer-group-tabs">{CUSTOMER_GROUPS.map((item) => <button type="button" key={item.type} className={customerGroup === item.type ? "active" : ""} onClick={() => setCustomerGroup(item.type)}>{item.title}</button>)}</div>
          <div className="row-search customer-picker-search"><span>⌕</span><input value={customerSearch} onChange={(event) => setCustomerSearch(event.target.value)} placeholder="按客户名称过滤" /></div>
          <div className="customer-picker-list">
            <button type="button" className={customerId === null ? "active" : ""} onClick={() => setCustomerId(null)}><strong>不关联客户（一次性报价）</strong><span>不套用任何客户折扣与特殊要求</span></button>
            {groupCustomers.map((customer) => <button type="button" key={customer.id} className={customerId === customer.id ? "active" : ""} onClick={() => setCustomerId(customer.id)}><strong>{customer.name}{customer.price_sheet_count > 0 && <em className="price-sheet-badge">专属价目</em>}</strong><span>{customer.notes || "暂无说明"}</span></button>)}
            {groupCustomers.length === 0 && <small className="picker-empty">该分组下没有匹配的客户</small>}
          </div>
          {selectedCustomer && <CustomerPolicy customer={selectedCustomer} />}
          <div className="step-label"><b>02</b><span>多报价设置</span></div>
          <div className="option-picker"><div><strong>每个商品推荐几个制造商方案？</strong><span>候选不足时按实际可用数量展示</span></div><div>{[1, 2, 3, 4, 5].map((value) => <button type="button" className={optionCount === value ? "active" : ""} key={value} onClick={() => pickOptionCount(value)}>{value}</button>)}</div></div>
          <div className="option-picker"><div><strong>税后价格税率？（%）</strong><span>导出时按 确认单价×(1+税率) 计算 税后单价 / 税后总价</span></div><div><input className="tax-rate-input" type="number" min={0} max={100} step={0.1} value={taxPercent} onChange={(event) => { const value = Number(event.target.value); if (Number.isFinite(value)) { setTaxPercent(value); window.localStorage.setItem("quote-tax-rate", String(value)); } }} /></div></div>
        </div>
        <div className="form-card upload-section">
          <div className="step-label"><b>03</b><span>上传询价单</span></div>
          <label className={`upload-card ${file ? "has-file" : ""}`}>
            <input type="file" accept=".xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" onChange={(event) => setFile(event.target.files?.[0] ?? null)} />
            <span className="upload-icon">↑</span>
            <strong>{file ? file.name : "选择待报价 Excel"}</strong>
            <span>{file ? `${(file.size / 1024 / 1024).toFixed(2)} MB · 可随时重新选择` : "支持 .xlsx，单文件不超过 80MB"}</span>
            <em>{file ? "更换文件" : "浏览文件"}</em>
          </label>
          {error && <div className="form-error">{error}</div>}
          <button className="primary-button submit-task" disabled={!file || busy}>{busy ? "正在上传…" : customerId != null ? `创建任务并推荐 ${optionCount} 个方案` : `创建一次性报价并推荐 ${optionCount} 个方案`}</button>
        </div>
      </form>
    </section>
  );
}

function CustomerPolicy({ customer }: { customer: Customer }) {
  return (
    <div className={`customer-policy type-${customer.customer_type}`}>
      <div><span>{CUSTOMER_COPY[customer.customer_type]}</span><strong>{customer.name}</strong></div>
      <p>{customer.notes || "暂无客户策略说明"}</p>
      {customer.discount_percent > 0 && <small>协议折扣 {customer.discount_percent}% · 需核对最低毛利</small>}
      {customer.price_sheet_count > 0 && <small>已关联专属报价单（{customer.price_sheet_count} 条），匹配时优先使用该价目，价格不再叠加协议折扣</small>}
      {customer.requirements.length > 0 && <ul>{customer.requirements.map((item) => <li key={item.id}><b>{item.required ? "必选" : "偏好"}</b>{item.attribute_name} {item.operator} {item.value}{item.unit}</li>)}</ul>}
    </div>
  );
}

function JobWorkspace({ job, onBack, onRefresh, notify }: { job: Job; onBack: () => void; onRefresh: () => Promise<void>; notify: (message: string) => void }) {
  const [result, setResult] = useState<PageResult>({ items: [], total: 0, page: 1, page_size: 50, page_count: 1 });
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(50);
  const [rowFilter, setRowFilter] = useState<RowFilter>("all");
  const [sort, setSort] = useState<LineSort>("score_asc");
  const [filterCounts, setFilterCounts] = useState<Partial<Record<RowFilter, number>>>({});
  const [query, setQuery] = useState("");
  const [searchDraft, setSearchDraft] = useState("");
  const [pageDraft, setPageDraft] = useState("1");
  const [loading, setLoading] = useState(false);
  const [selectedLineId, setSelectedLineId] = useState<number | null>(null);
  const [taxDraft, setTaxDraft] = useState(() => String(Math.round((job.tax_rate ?? 0.1) * 1000) / 10));

  const saveTax = useCallback(async () => {
    const value = Number(taxDraft);
    if (!Number.isFinite(value) || value < 0 || value > 100) {
      setTaxDraft(String(Math.round((job.tax_rate ?? 0.1) * 1000) / 10));
      return;
    }
    const rate = value / 100;
    if (Math.abs(rate - (job.tax_rate ?? 0.1)) < 1e-9) return;
    await api(`/api/quote-jobs/${job.id}`, { method: "PATCH", body: JSON.stringify({ tax_rate: rate }) });
    await onRefresh();
  }, [job.id, job.tax_rate, taxDraft, onRefresh]);

  const loadLines = useCallback(async () => {
    if (["queued", "reserved", "matching"].includes(job.status)) return;
    setLoading(true);
    try {
      const params = new URLSearchParams({ page: String(page), page_size: String(pageSize), row_filter: rowFilter, query });
      if (sort !== "source_row") params.set("sort", sort);
      const data = await api<PageResult>(`/api/quote-jobs/${job.id}/lines?${params}`);
      setResult(data);
      if (data.page !== page) setPage(data.page);
      setPageDraft(String(data.page));
    } finally {
      setLoading(false);
    }
  }, [job.id, job.status, page, pageSize, query, rowFilter, sort]);

  const loadFilterCounts = useCallback(async () => {
    if (["queued", "reserved", "matching"].includes(job.status)) return;
    const entries = await Promise.all(
      ROW_FILTER_TABS.map(async ([value]) => {
        const data = await api<PageResult>(`/api/quote-jobs/${job.id}/lines?page=1&page_size=25&row_filter=${value}`);
        return [value, data.total] as const;
      }),
    );
    setFilterCounts(Object.fromEntries(entries));
  }, [job.id, job.status]);

  // Loading the selected server page is the synchronization purpose of this effect.
  // eslint-disable-next-line react-hooks/set-state-in-effect
  useEffect(() => { void loadLines(); }, [loadLines]);
  // Filter counts refresh whenever the job record changes on the server (updated_at bump).
  // eslint-disable-next-line react-hooks/set-state-in-effect
  useEffect(() => { void loadFilterCounts(); }, [loadFilterCounts, job.updated_at]);
  useEffect(() => {
    if (!["queued", "reserved", "matching"].includes(job.status)) return;
    const timer = setInterval(() => void onRefresh(), 1500);
    return () => clearInterval(timer);
  }, [job.status, onRefresh]);

  async function batchConfirm(scope: "current" | "filtered") {
    const response = await api<{ confirmed: number; skipped: number }>(`/api/quote-jobs/${job.id}/batch-confirm`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ scope, page, page_size: pageSize, row_filter: rowFilter, query }),
    });
    notify(`已安全确认 ${response.confirmed} 行；${response.skipped} 行因置信度或风险被跳过。`);
    await Promise.all([loadLines(), onRefresh()]);
  }

  async function autoExport(variant: "internal" | "customer") {
    const response = await fetch(`/api/quote-jobs/${job.id}/auto-confirm-and-export/${variant}`, { method: "POST", credentials: "include" });
    if (!response.ok) {
      const payload = (await response.json()) as { detail?: string };
      throw new Error(payload.detail || "一键导出失败");
    }
    await onRefresh();
    const blob = await response.blob();
    const disposition = response.headers.get("content-disposition") ?? "";
    const encoded = disposition.match(/filename\*=utf-8''([^;]+)/i)?.[1];
    const name = encoded ? decodeURIComponent(encoded) : `${job.file_name}_${variant}_auto.xlsx`;
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = name;
    link.click();
    URL.revokeObjectURL(url);
    notify("一键导出完成：自动带价行已确认，未带价行留待复核。");
  }

  async function download(variant: "internal" | "customer") {
    const response = await fetch(`/api/quote-jobs/${job.id}/export/${variant}`, { method: "POST", credentials: "include" });
    if (!response.ok) {
      const payload = (await response.json()) as { detail?: string };
      throw new Error(payload.detail || "导出失败");
    }
    const blob = await response.blob();
    const disposition = response.headers.get("content-disposition") ?? "";
    const encoded = disposition.match(/filename\*=utf-8''([^;]+)/i)?.[1];
    const name = encoded ? decodeURIComponent(encoded) : `${job.file_name}_${variant}.xlsx`;
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = name;
    link.click();
    URL.revokeObjectURL(url);
    notify(variant === "internal" ? "内部复核版已导出。" : "客户版已导出，内部评分与来源已隐藏。");
    await onRefresh();
  }

  async function reprocess() {
    await api<{ id: string; status: string }>(`/api/quote-jobs/${job.id}/reprocess`, { method: "POST" });
    notify("已重新提交匹配任务，原文件无需重复上传。");
    await onRefresh();
  }

  function applySearch(event: FormEvent) {
    event.preventDefault();
    setPage(1);
    setQuery(searchDraft.trim());
  }

  function jumpPage() {
    const parsed = Number.parseInt(pageDraft, 10);
    const safe = Number.isFinite(parsed) ? Math.min(Math.max(parsed, 1), result.page_count) : 1;
    setPage(safe);
    setPageDraft(String(safe));
  }

  if (["queued", "reserved", "matching"].includes(job.status)) {
    return (
      <section><button className="back-button" onClick={onBack}>← 返回任务列表</button><div className="processing-card"><div className="processing-orbit"><span>{job.progress}%</span></div><p className="eyebrow">SERVER MATCHING</p><h2>正在进行参数与单位校验</h2><p>页面可以关闭，服务器会继续处理，完成后可从任务中心继续复核。</p><div className="wide-progress"><i style={{ width: `${Math.max(job.progress, 4)}%` }} /></div></div></section>
    );
  }

  if (job.status === "failed") {
    return <section><button className="back-button" onClick={onBack}>← 返回任务列表</button><div className="failure-card"><strong>任务处理失败</strong><p>{job.error_message}</p><div className="review-actions"><button className="primary-button" onClick={() => void reprocess()}>重新处理</button><button className="secondary-button" onClick={() => onRefresh()}>刷新状态</button></div></div></section>;
  }

  return (
    <section>
      <button className="back-button" onClick={onBack}>← 返回任务列表</button>
      <PageHeading eyebrow="QUOTATION REVIEW" title={job.display_name || job.file_name} detail={`${job.customer?.name ?? "一次性报价"} · 每商品最多 ${job.requested_option_count} 个制造商方案· 税后税率 ${Math.round((job.tax_rate ?? 0.1) * 1000) / 10}%`} action={<StatusPill status={job.status} />} />
      <div className="tax-notice"><label>税后价格税率</label><input type="number" min={0} max={100} step={0.1} value={taxDraft} onChange={(event) => setTaxDraft(event.target.value)} onBlur={() => void saveTax()} onKeyDown={(event) => { if (event.key === "Enter") void saveTax(); }} /><span>% · 导出时按 确认单价×(1+税率) 计算 税后单价 / 税后总价（回车或失焦保存）</span></div>
      <div className="confirm-progress">
        <span>已确认 {job.confirmed_lines} / {job.total_lines} 行</span>
        <i><b style={{ width: `${job.total_lines ? Math.min(100, (job.confirmed_lines / job.total_lines) * 100) : 0}%` }} /></i>
      </div>
      <div className="summary-grid">
        <Metric label="产品行" value={job.total_lines} tone="slate" />
        <Metric label="高置信建议" value={job.matched_lines} tone="teal" />
        <Metric label="需人工复核" value={job.review_lines} tone="amber" />
        <Metric label="无可靠匹配" value={job.unmatched_lines} tone="red" />
        <Metric label="已确认" value={job.confirmed_lines} tone="blue" />
      </div>

      <div className="review-card">
        <div className="review-toolbar">
          <form className="row-search" onSubmit={applySearch}><span>⌕</span><input value={searchDraft} onChange={(event) => setSearchDraft(event.target.value)} placeholder="名称、编码、型号或原表行号" /><button>定位</button></form>
          <div className="review-actions">
            <button className="secondary-button" onClick={() => void batchConfirm("current")}>安全确认当前页</button>
            <button className="secondary-button" onClick={() => void batchConfirm("filtered")}>安全确认筛选结果</button>
            <div className="export-menu"><button className="primary-button">导出 ▾</button><div><button onClick={() => void download("internal")}>内部复核版</button><button onClick={() => void download("customer")}>客户版</button><button onClick={() => void autoExport("customer")}>一键导出客户版（自动确认）</button></div></div>
          </div>
        </div>
        <div className="exception-tabs" aria-label="报价行筛选">
          {ROW_FILTER_TABS.map(([value, label]) => (
            <button key={value} title={STATUS_HINT[value]} className={rowFilter === value ? "active" : ""} onClick={() => { setRowFilter(value); setPage(1); }}>
              {label}{filterCounts[value] != null && <b className="filter-badge">{filterCounts[value]}</b>}
            </button>
          ))}
        </div>
        <div className={`table-scroll ${loading ? "loading" : ""}`}>
          <table className="quote-table">
            <thead><tr><th>原表行</th><th>产品 / 参数</th><th>型号 / 单位</th><th>数量</th><th>首选报价</th><th><button type="button" className="sort-button" title="点击切换排序：评分升序 → 评分降序 → 原表行号" onClick={() => { setSort(SORT_CYCLE[sort]); setPage(1); }}>{SORT_LABEL[sort]}</button></th><th>风险状态</th><th>方案</th></tr></thead>
            <tbody>
              {result.items.map((line) => {
                const unitConflict = line.warnings.some((item) => item.includes("单位不可直接换算"));
                return (
                  <tr key={line.id} className={unitConflict ? "row-blocked" : line.status === "unmatched" ? "row-unmatched" : ""}>
                    <td className="mono">{line.source_row}</td>
                    <td className="product-cell"><strong>{line.name}</strong><small title={line.spec}>{line.spec || "无参数描述"}</small></td>
                    <td><span>{line.model || "—"}</span><small className={unitConflict ? "unit-danger" : ""}>{line.unit || "无单位"}{unitConflict ? " · 冲突" : ""}</small></td>
                    <td>{line.pricing_quantity ?? "—"}</td>
                    <td><strong className="price">¥ {money(line.primary_option?.final_price)}</strong><small>{line.primary_option?.record.manufacturer || "尚未选择"}</small></td>
                    <td><span className={`confidence confidence-${line.confidence}`}>{CONFIDENCE_COPY[line.confidence] ?? line.confidence}</span><small>{line.recommended_score.toFixed(1)} 分</small></td>
                    <td>{line.confirmed ? <StatusPill status="confirmed" /> : <StatusPill status={line.status} />}<small className={blockingWarnings(line.warnings).length ? "danger-text" : ""}>{line.warnings[0]?.replace("BLOCK: ", "") || "无阻断风险"}</small></td>
                    <td><button className="text-button" onClick={() => setSelectedLineId(line.id)}>{line.selected_option_count ? `${line.selected_option_count} 个方案` : "选择方案"} →</button></td>
                  </tr>
                );
              })}
              {!loading && result.items.length === 0 && <tr><td colSpan={8}><div className="table-empty">当前筛选条件下没有报价行</div></td></tr>}
            </tbody>
          </table>
        </div>
        <div className="pagination">
          <div><span>共 {result.total.toLocaleString("zh-CN")} 条</span><label>每页<select value={pageSize} onChange={(event) => { setPageSize(Number(event.target.value)); setPage(1); }}>{[25, 50, 100].map((size) => <option key={size}>{size}</option>)}</select></label></div>
          <div className="page-controls"><button disabled={page <= 1} onClick={() => setPage(1)}>首页</button><button disabled={page <= 1} onClick={() => setPage((value) => value - 1)}>上一页</button><label>第<input aria-label="跳转页码" value={pageDraft} onChange={(event) => setPageDraft(event.target.value)} onBlur={jumpPage} onKeyDown={(event) => { if (event.key === "Enter") jumpPage(); }} />页 / {result.page_count}</label><button disabled={page >= result.page_count} onClick={() => setPage((value) => value + 1)}>下一页</button><button disabled={page >= result.page_count} onClick={() => setPage(result.page_count)}>末页</button></div>
        </div>
      </div>
      {selectedLineId != null && <CandidateDrawer lineId={selectedLineId} maxOptions={job.requested_option_count} onClose={() => setSelectedLineId(null)} onChanged={async () => { await Promise.all([loadLines(), onRefresh()]); }} />}
    </section>
  );
}

function CandidateDrawer({ lineId, maxOptions, onClose, onChanged }: { lineId: number; maxOptions: number; onClose: () => void; onChanged: () => Promise<void> }) {
  const [line, setLine] = useState<QuoteLine | null>(null);
  const [selected, setSelected] = useState<number[]>([]);
  const [prices, setPrices] = useState<Record<string, number>>({});
  const [note, setNote] = useState("");
  const [overrideReason, setOverrideReason] = useState("");
  const [drawerNotice, setDrawerNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [showManualForm, setShowManualForm] = useState(false);
  const [manualManufacturer, setManualManufacturer] = useState("");
  const [manualBrand, setManualBrand] = useState("");
  const [manualModel, setManualModel] = useState("");
  const [manualSpec, setManualSpec] = useState("");
  const [manualUnit, setManualUnit] = useState("");
  const [manualPrice, setManualPrice] = useState("");
  const [manualError, setManualError] = useState("");
  const [manualBusy, setManualBusy] = useState(false);
  const [loadError, setLoadError] = useState("");

  const load = useCallback(async () => {
    try {
      const data = await api<QuoteLine>(`/api/quote-lines/${lineId}`);
      setLine(data);
      const options = data.options ?? [];
      setSelected(options.filter((item) => item.selected).map((item) => item.id));
      setPrices(Object.fromEntries(options.map((item) => [String(item.id), item.final_price])));
      setLoadError("");
    } catch (reason) {
      setLoadError(reason instanceof Error ? reason.message : "候选方案加载失败");
    }
  }, [lineId]);
  // Fetching the server-owned candidate state is the synchronization purpose of this effect.
  // eslint-disable-next-line react-hooks/set-state-in-effect
  useEffect(() => { void load(); }, [load]);

  const options = dedupeOptions(line?.options ?? []);
  const selectedOptions = options.filter((item) => selected.includes(item.id));
  const hasBlocking = selectedOptions.some((item) => blockingWarnings(item.warnings).length > 0);
  const historicalPrices = options.map((item) => item.normalized_price ?? item.record.price).filter((price) => price > 0);
  const priceRange = historicalPrices.length
    ? `¥${money(Math.min(...historicalPrices))} — ¥${money(Math.max(...historicalPrices))}`
    : "暂无有效价格";
  const medianPrice = historicalPrices.length
    ? (() => { const s = [...historicalPrices].sort((a, b) => a - b); const m = Math.floor(s.length / 2); return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2; })()
    : null;
  const sourceGroups = Object.entries(
    options.reduce<Record<string, number[]>>((acc, item) => {
      const price = item.normalized_price ?? item.record.price;
      if (!(price > 0)) return acc;
      const key = item.record.manufacturer || item.record.source_file || "其他";
      (acc[key] ??= []).push(price);
      return acc;
    }, {}),
  )
    .map(([name, prices]) => ({ name: String(name).slice(0, 12), count: prices.length, min: Math.min(...prices), max: Math.max(...prices) }))
    .sort((a, b) => b.count - a.count);

  function toggle(option: QuoteOption) {
    setDrawerNotice("");
    setSelected((current) => {
      if (current.includes(option.id)) return current.filter((id) => id !== option.id);
      if (current.length >= maxOptions) {
        setDrawerNotice(`每个商品最多选择 ${maxOptions} 个方案。`);
        return current;
      }
      const key = optionKey(option);
      if (options.some((item) => current.includes(item.id) && optionKey(item) === key)) {
        setDrawerNotice("不能重复选择完全相同的方案。");
        return current;
      }
      return [...current, option.id];
    });
  }

  async function submitManual(event: FormEvent) {
    event.preventDefault();
    const parsed = Number(manualPrice);
    if (!manualManufacturer.trim() && !manualBrand.trim()) { setManualError("制造商和品牌至少填写一项。"); return; }
    if (!Number.isFinite(parsed) || parsed <= 0) { setManualError("请填写有效的价格（大于 0 的数字）。"); return; }
    setManualBusy(true);
    setManualError("");
    try {
      const payload: Record<string, string | number> = { price: parsed };
      if (manualManufacturer.trim()) payload.manufacturer = manualManufacturer.trim();
      if (manualBrand.trim()) payload.brand = manualBrand.trim();
      if (manualModel.trim()) payload.model = manualModel.trim();
      if (manualSpec.trim()) payload.spec = manualSpec.trim();
      if (manualUnit.trim()) payload.unit = manualUnit.trim();
      const created = await api<QuoteOption>(`/api/quote-lines/${lineId}/options`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const previousSelected = selected;
      await Promise.all([load(), onChanged()]);
      // load() resets the selection from the server; keep local unsaved picks and add the new option.
      setSelected(Array.from(new Set([...previousSelected, created.id])));
      setPrices((current) => ({ ...current, [String(created.id)]: created.final_price }));
      setManualManufacturer("");
      setManualBrand("");
      setManualModel("");
      setManualSpec("");
      setManualUnit("");
      setManualPrice("");
      setShowManualForm(false);
      setDrawerNotice("手工方案已创建并选用；该行已回到待复核状态。");
    } catch (reason) {
      setManualError(reason instanceof Error ? reason.message : "手工方案创建失败");
    } finally {
      setManualBusy(false);
    }
  }

  async function removeManualOption(option: QuoteOption) {
    const maker = option.record.manufacturer || option.record.brand || "未命名";
    if (!window.confirm(`确定删除手工方案「${maker} · ¥${money(option.final_price)}」吗？`)) return;
    try {
      await api<{ ok: boolean }>(`/api/quote-options/${option.id}`, { method: "DELETE" });
      setSelected((current) => current.filter((id) => id !== option.id));
      setDrawerNotice("手工方案已删除。");
      await Promise.all([load(), onChanged()]);
    } catch (reason) {
      setDrawerNotice(reason instanceof Error ? reason.message : "删除失败");
    }
  }

  async function save(confirm: boolean) {
    if (!selected.length) { setDrawerNotice("请至少选择一个制造商方案。"); return; }
    setBusy(true);
    setDrawerNotice("");
    try {
      await api<QuoteLine>(`/api/quote-lines/${lineId}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ selected_option_ids: selected, final_prices: prices, manual_note: note }),
      });
      if (confirm) {
        await api<QuoteLine>(`/api/quote-lines/${lineId}/confirm`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ override_reason: overrideReason }),
        });
        setDrawerNotice("当前商品已确认，所有操作已写入审计记录。");
      } else {
        setDrawerNotice("方案已保存，可稍后继续确认。");
      }
      await Promise.all([load(), onChanged()]);
      if (confirm) setTimeout(onClose, 900);
    } catch (reason) {
      setDrawerNotice(reason instanceof Error ? reason.message : "保存失败");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="drawer-backdrop">
      <button className="drawer-dismiss" aria-label="关闭候选对比" onClick={onClose} />
      <aside className="candidate-drawer" role="dialog" aria-modal="true" aria-label="报价候选对比">
        <header><div><p className="eyebrow">CANDIDATE COMPARISON</p><h2>{line?.name ?? "正在加载…"}</h2><span>最多选择 {maxOptions} 个不同制造商方案</span></div><button onClick={onClose} aria-label="关闭">×</button></header>
        {loadError && <div className="drawer-alert danger"><strong>加载失败</strong><span>{loadError}</span><button className="secondary-button" onClick={() => void load()}>重试</button></div>}
        {line && <div className="target-brief"><div><span>待报价参数</span><p>{line.spec || "无参数描述"}</p></div><div><span>目标型号 / 单位</span><strong>{line.model || "—"} / {line.unit || "无单位"}</strong></div><div><span>候选历史价区间{medianPrice != null ? ` · 中位 ¥${money(medianPrice)}` : ""}</span><strong>{priceRange}</strong><small className="price-chips">{sourceGroups.map((group) => <em key={group.name} title={`${group.name}：${group.count} 条候选`}>{group.name}×{group.count} ¥{money(group.min)}-{money(group.max)}</em>)}</small></div></div>}
        {line && line.warnings.length > 0 && <div className={`drawer-alert ${blockingWarnings(line.warnings).length ? "danger" : "warning"}`}><strong>{blockingWarnings(line.warnings).length ? "存在阻断风险" : "请人工核对"}</strong><span>{line.warnings.join("；").replaceAll("BLOCK: ", "")}</span></div>}
        <div className="candidate-list">
          {options.map((option) => {
            const isSelected = selected.includes(option.id);
            const blocked = blockingWarnings(option.warnings).length > 0;
            const manual = isManualOption(option);
            return (
              <article className={`candidate-card ${isSelected ? "selected" : ""} ${blocked ? "blocked" : ""} ${manual ? "manual" : ""}`} key={option.id}>
                <button className="candidate-select" onClick={() => toggle(option)} aria-pressed={isSelected}>{isSelected ? "✓" : "+"}</button>
                <div className="candidate-head"><div>{manual ? <span className="manual-badge">手工方案</span> : <span className="rank">#{option.rank}</span>}<strong>{option.record.manufacturer || "未记录制造商"}</strong><small>{option.record.brand || "无品牌"}</small></div><div>{manual && <button className="candidate-delete" title="删除该手工方案" aria-label="删除该手工方案" onClick={() => void removeManualOption(option)}>×</button>}<b>¥ {money(prices[String(option.id)])}</b><span>{manual ? CONFIDENCE_COPY.manual : `${option.score.toFixed(1)} 分`}</span></div></div>
                <div className="candidate-facts"><span><b>型号</b>{option.record.model || "—"}</span><span className={`unit-${option.unit_status}`}><b>单位</b>{option.record.unit || "—"}{option.unit_status === "convertible" ? ` → ${line?.unit}` : ""}</span><span><b>报价日期</b>{option.record.quote_date || "未记录"}</span></div>
                <p className="candidate-spec">{option.record.spec || "无参数描述"}</p>
                {Object.keys(option.component_scores).length > 0 && <div className="score-bars">{Object.entries(option.component_scores).map(([label, score]) => <div key={label}><span>{label}</span><i><b style={{ width: `${Math.min(100, score / ({ 参数: 45, 型号: 15, 名称: 15, "制造商/品牌": 10, 单位: 10, 数据质量: 5 } as Record<string, number>)[label] * 100)}%` }} /></i><em>{score}</em></div>)}</div>}
                {option.reasons.length > 0 && <div className="reason-list">{option.reasons.map((reason) => <span key={reason}>{reason}</span>)}</div>}
                {option.warnings.length > 0 && <div className={`option-warning ${blocked ? "danger" : ""}`}>{option.warnings.join("；").replaceAll("BLOCK: ", "")}</div>}
                <footer><label>方案报价（元）<input type="number" min="0.01" step="0.01" value={prices[String(option.id)] ?? ""} onChange={(event) => setPrices((current) => ({ ...current, [String(option.id)]: Number(event.target.value) }))} /></label><span>{manual ? "手工录入" : `${option.record.source_file} · ${option.record.source_sheet} · 第${option.record.source_row}行`}</span></footer>
              </article>
            );
          })}
          {line && options.length === 0 && <div className="empty-state compact"><strong>没有可靠候选</strong><span>请补充历史数据或人工报价。</span></div>}
        </div>
        <div className="manual-option-block">
          {!showManualForm ? (
            <button className="secondary-button" onClick={() => { setShowManualForm(true); setManualError(""); }}>＋ 新建手工方案</button>
          ) : (
            <form className="manual-option-form" onSubmit={submitManual}>
              <label><span>制造商 *</span><input value={manualManufacturer} onChange={(event) => setManualManufacturer(event.target.value)} placeholder="与品牌至少填一项" /></label>
              <label><span>品牌 *</span><input value={manualBrand} onChange={(event) => setManualBrand(event.target.value)} placeholder="与制造商至少填一项" /></label>
              <label><span>型号</span><input value={manualModel} onChange={(event) => setManualModel(event.target.value)} /></label>
              <label><span>参数</span><input value={manualSpec} onChange={(event) => setManualSpec(event.target.value)} /></label>
              <label><span>单位</span><input value={manualUnit} onChange={(event) => setManualUnit(event.target.value)} /></label>
              <label><span>价格（元）*</span><input type="number" min="0.01" step="0.01" value={manualPrice} onChange={(event) => setManualPrice(event.target.value)} required /></label>
              <div className="manual-option-actions">
                <button type="button" className="secondary-button" onClick={() => setShowManualForm(false)}>取消</button>
                <button className="primary-button" disabled={manualBusy}>{manualBusy ? "正在保存…" : "保存并选用"}</button>
              </div>
              {manualError && <div className="form-error">{manualError}</div>}
            </form>
          )}
        </div>
        <div className="drawer-footer">
          {drawerNotice && <div className="drawer-notice" role="status">{drawerNotice}</div>}
          <div className="drawer-fields"><label><span>调整说明</span><input value={note} onChange={(event) => setNote(event.target.value)} placeholder="选填：改价或选厂家的原因" /></label>{hasBlocking && <label><span>阻断风险覆盖原因（选填）</span><input value={overrideReason} onChange={(event) => setOverrideReason(event.target.value)} placeholder="说明为何仍可确认" /></label>}</div>
          <div className="drawer-actions"><button className="secondary-button" onClick={() => void save(false)} disabled={busy}>保存方案</button><button className="primary-button confirm-action" onClick={() => void save(true)} disabled={busy || !selected.length}>{busy ? "正在保存…" : "确认当前商品"}</button></div>
        </div>
      </aside>
    </div>
  );
}

function CustomerDirectory({ customers, user, onUpdated, notify }: { customers: Customer[]; user: User; onUpdated: () => Promise<void>; notify: (message: string) => void }) {
  const isAdmin = user.role === "admin";
  const [group, setGroup] = useState<Customer["customer_type"] | null>(null);
  const [search, setSearch] = useState("");
  const [showForm, setShowForm] = useState(false);
  const [editing, setEditing] = useState<Customer | null>(null);

  async function removeCustomer(customer: Customer) {
    if (!window.confirm(`确定删除客户「${customer.name}」吗？删除后该客户不再出现在客户列表中。`)) return;
    try {
      await api<{ ok: boolean }>(`/api/customers/${customer.id}`, { method: "DELETE" });
      notify("客户档案已删除。");
      await onUpdated();
    } catch (reason) {
      notify(reason instanceof Error ? reason.message : "删除失败");
    }
  }

  if (group === null) {
    return (
      <section>
        <PageHeading eyebrow="CUSTOMER POLICIES" title="客户档案" detail="客户类型不仅是标签，还会影响特殊要求校验和报价策略。" />
        <div className="customer-grid">
          {CUSTOMER_GROUPS.map((item) => (
            <button className={`group-card type-${item.type}`} key={item.type} onClick={() => { setGroup(item.type); setSearch(""); setShowForm(false); setEditing(null); }}>
              <span>{item.title}</span>
              <strong>{customers.filter((customer) => customer.customer_type === item.type).length}</strong>
              <small>{item.detail} · 点击查看名单 →</small>
            </button>
          ))}
        </div>
      </section>
    );
  }

  const groupMeta = CUSTOMER_GROUPS.find((item) => item.type === group);
  const groupTotal = customers.filter((customer) => customer.customer_type === group).length;
  const keyword = search.trim().toLowerCase();
  const visible = customers.filter((customer) => customer.customer_type === group && (!keyword || customer.name.toLowerCase().includes(keyword)));

  return (
    <section>
      <button className="back-button" onClick={() => { setGroup(null); setShowForm(false); setEditing(null); }}>← 返回分组</button>
      <PageHeading
        eyebrow="CUSTOMER POLICIES"
        title={groupMeta?.title ?? "客户档案"}
        detail={`共 ${groupTotal} 位客户 · ${groupMeta?.detail ?? ""}`}
        action={isAdmin ? <button className="primary-button" onClick={() => { setEditing(null); setShowForm((value) => !value); }}>{showForm && !editing ? "取消" : "＋ 新建客户"}</button> : undefined}
      />
      <div className="row-search customer-search"><span>⌕</span><input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="按客户名称过滤" /></div>
      {(showForm || editing) && (
        <CustomerForm
          key={editing ? `edit-${editing.id}` : `new-${group}`}
          initial={editing}
          defaultType={group}
          onSaved={async (message) => { setShowForm(false); setEditing(null); await onUpdated(); notify(message); }}
        />
      )}
      {visible.length === 0 ? (
        <div className="empty-state compact"><strong>{keyword ? "没有匹配的客户" : "该分组还没有客户"}</strong><span>{isAdmin ? "点击右上角「新建客户」添加。" : "请联系管理员添加客户档案。"}</span></div>
      ) : (
        <div className="customer-grid">{visible.map((customer) => (
          <div className={`customer-card type-${customer.customer_type}`} key={customer.id}>
            <header><span>{CUSTOMER_COPY[customer.customer_type]}</span><h3>{customer.name}</h3></header>
            <p>{customer.notes || "暂无说明"}</p>
            <dl><div><dt>协议折扣</dt><dd>{customer.discount_percent ? `${customer.discount_percent}%` : "标准价"}</dd></div><div><dt>最低毛利线</dt><dd>{customer.minimum_margin_percent ? `${customer.minimum_margin_percent}%` : "未设置"}</dd></div></dl>
            <div className="requirement-block"><strong>结构化要求</strong>{customer.requirements.length ? customer.requirements.map((item) => <span key={item.id}><b>{item.required ? "必选" : "偏好"}</b>{item.attribute_name} {item.operator} {item.value}{item.unit}</span>) : <small>暂无特殊要求</small>}</div>
            {isAdmin && (
              <footer className="customer-card-actions">
                <button className="card-action-button" onClick={() => { setEditing(customer); setShowForm(false); }}>编辑</button>
                <button className="card-action-button danger" onClick={() => void removeCustomer(customer)}>删除</button>
              </footer>
            )}
          </div>
        ))}</div>
      )}
    </section>
  );
}

function CustomerForm({ initial, defaultType, onSaved }: { initial: Customer | null; defaultType: Customer["customer_type"]; onSaved: (message: string) => void | Promise<void> }) {
  const editing = initial !== null;
  const [name, setName] = useState(initial?.name ?? "");
  const [type, setType] = useState<Customer["customer_type"]>(initial?.customer_type ?? defaultType);
  const [notes, setNotes] = useState(initial?.notes ?? "");
  const [attribute, setAttribute] = useState("");
  const [operator, setOperator] = useState("contains");
  const [value, setValue] = useState("");
  const [unit, setUnit] = useState("");
  const [required, setRequired] = useState(true);
  const [requirements, setRequirements] = useState<Array<Omit<Requirement, "id">>>(initial?.requirements.map(({ attribute_name, operator: itemOperator, value: itemValue, unit: itemUnit, required: itemRequired, notes: itemNotes }) => ({ attribute_name, operator: itemOperator, value: itemValue, unit: itemUnit, required: itemRequired, notes: itemNotes })) ?? []);
  const [discount, setDiscount] = useState(initial?.discount_percent ?? 0);
  const [minimumMargin, setMinimumMargin] = useState(initial?.minimum_margin_percent ?? 0);
  const [preferredManufacturers, setPreferredManufacturers] = useState(initial?.preferred_manufacturers.join(", ") ?? "");
  const [priceSheetFile, setPriceSheetFile] = useState<File | null>(null);
  const [error, setError] = useState("");

  function currentRequirement(): Omit<Requirement, "id"> | null {
    if (!attribute.trim() || !value.trim()) return null;
    return { attribute_name: attribute.trim(), operator, value: value.trim(), unit: unit.trim(), required, notes: "" };
  }

  function addRequirement() {
    const item = currentRequirement();
    if (!item) {
      setError("请先填写属性名和目标值。");
      return;
    }
    setRequirements((items) => [...items, item]);
    setAttribute("");
    setValue("");
    setUnit("");
    setError("");
  }

  async function removePriceSheet() {
    if (!initial) return;
    if (!window.confirm("确定删除该客户的专属报价单？")) return;
    try {
      await api<{ ok: boolean; deleted: number }>(`/api/customers/${initial.id}/price-sheet`, { method: "DELETE" });
      await onSaved("专属报价单已删除。");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "删除失败");
    }
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    try {
      const draft = currentRequirement();
      const allRequirements = [...requirements, ...(draft ? [draft] : [])];
      if (type === "special" && allRequirements.length === 0) {
        setError("特殊要求客户至少需要一条结构化要求。");
        return;
      }
      const payload = {
        name,
        customer_type: editing ? type : defaultType,
        notes,
        requirements: type === "ordinary" ? [] : allRequirements,
        discount_percent: type === "vip" ? discount : 0,
        minimum_margin_percent: type === "vip" ? minimumMargin : 0,
        preferred_manufacturers: type === "vip"
          ? preferredManufacturers.split(/[,，]/).map((item) => item.trim()).filter(Boolean)
          : [],
      };
      let customerId: number;
      if (editing) {
        await api<Customer>(`/api/customers/${initial.id}`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });
        customerId = initial.id;
      } else {
        const created = await api<Customer>("/api/customers", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });
        customerId = created.id;
      }
      if (priceSheetFile) {
        try {
          const formData = new FormData();
          formData.set("file", priceSheetFile);
          const result = await api<{ inserted: number; skipped_duplicates: number; skipped_invalid: number }>(`/api/customers/${customerId}/price-sheet`, { method: "POST", body: formData });
          await onSaved(editing
            ? `客户档案已更新，专属报价单已整表替换（导入 ${result.inserted} 条 / 重复跳过 ${result.skipped_duplicates} 条 / 无效 ${result.skipped_invalid} 条）。`
            : `客户档案已创建，专属报价单已上传（导入 ${result.inserted} 条 / 重复跳过 ${result.skipped_duplicates} 条 / 无效 ${result.skipped_invalid} 条）。`);
        } catch (reason) {
          const message = reason instanceof Error ? reason.message : "上传失败";
          await onSaved(editing
            ? `客户档案已更新，但专属报价单上传失败，可重新上传：${message}`
            : `客户档案已创建，但专属报价单上传失败，可在编辑中重新上传：${message}`);
        }
        return;
      }
      await onSaved(editing ? "客户档案已更新。" : "客户档案已创建。");
    } catch (reason) { setError(reason instanceof Error ? reason.message : "保存失败"); }
  }
  return (
    <form className="inline-form customer-editor" onSubmit={submit}>
      <label><span>客户名称</span><input value={name} onChange={(event) => setName(event.target.value)} required /></label>
      {editing ? (
        <label><span>客户类型</span><select value={type} onChange={(event) => setType(event.target.value as Customer["customer_type"])}><option value="ordinary">普通客户</option><option value="special">特殊要求</option><option value="vip">VIP客户</option></select></label>
      ) : (
        <label><span>客户类型</span><b className="type-static">{CUSTOMER_COPY[defaultType]}</b></label>
      )}
      <label className="wide-field"><span>策略说明 / 自由文本要求</span><input value={notes} onChange={(event) => setNotes(event.target.value)} placeholder="选填，用于补充无法结构化的说明" /></label>

      {type !== "ordinary" && (
        <div className="requirement-editor">
          <label><span>参数名</span><input list="attribute-suggestions" value={attribute} onChange={(event) => setAttribute(event.target.value)} placeholder="例：容量" /><datalist id="attribute-suggestions">{ATTRIBUTE_SUGGESTIONS.map((item) => <option key={item} value={item} />)}</datalist></label>
          <label><span>比较关系</span><select value={operator} onChange={(event) => setOperator(event.target.value)}><option value="contains">包含</option><option value="≥">大于等于</option><option value="≤">小于等于</option><option value="=">等于</option></select></label>
          <label><span>目标值</span><input value={value} onChange={(event) => setValue(event.target.value)} placeholder="例：100" /></label>
          <label><span>单位</span><input value={unit} onChange={(event) => setUnit(event.target.value)} placeholder="例：ml" /></label>
          <label><span>约束级别</span><select value={required ? "required" : "preferred"} onChange={(event) => setRequired(event.target.value === "required")}><option value="required">必须满足</option><option value="preferred">偏好</option></select></label>
          <button type="button" className="secondary-button" onClick={addRequirement}>＋ 添加要求</button>
          {requirements.length > 0 && <div className="requirement-drafts">{requirements.map((item, index) => <span key={`${item.attribute_name}-${index}`}><b>{item.required ? "必选" : "偏好"}</b>{item.attribute_name} {item.operator} {item.value}{item.unit}<button type="button" aria-label={`删除 ${item.attribute_name}`} onClick={() => setRequirements((items) => items.filter((_, itemIndex) => itemIndex !== index))}>×</button></span>)}</div>}
        </div>
      )}

      {type === "vip" && <>
        <label><span>协议折扣（%）</span><input type="number" min="0" max="100" step="0.1" value={discount} onChange={(event) => setDiscount(Number(event.target.value))} /></label>
        <label><span>最低毛利线（%）</span><input type="number" min="0" max="100" step="0.1" value={minimumMargin} onChange={(event) => setMinimumMargin(Number(event.target.value))} /></label>
        <label className="wide-field"><span>偏好制造商</span><input value={preferredManufacturers} onChange={(event) => setPreferredManufacturers(event.target.value)} placeholder="多个制造商用逗号分隔" /></label>
      </>}

      <div className="price-sheet-block">
        <strong>专属报价单（可选）</strong>
        {editing && initial.price_sheet_count > 0 && (
          <span className="price-sheet-current">
            当前专属报价单：{initial.price_sheet_count} 条
            <button type="button" className="card-action-button danger" onClick={() => void removePriceSheet()}>删除</button>
          </span>
        )}
        <label className="import-file">
          <input type="file" accept=".xlsx,.xls,.xlsm" onChange={(event) => setPriceSheetFile(event.target.files?.[0] ?? null)} />
          <span>{priceSheetFile ? priceSheetFile.name : editing && initial.price_sheet_count > 0 ? `重新上传（整表替换现有 ${initial.price_sheet_count} 条）` : "选择专属报价单 Excel"}</span>
        </label>
        <small>上传后，该客户的报价任务将优先使用此价目（价格不叠加协议折扣）</small>
      </div>

      <button className="primary-button">{editing ? "保存修改" : "保存客户"}</button>
      {error && <div className="form-error">{error}</div>}
    </form>
  );
}

function HistorySearch({ user, notify }: { user: User; notify: (message: string) => void }) {
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<HistoryResult[]>([]);
  const [loading, setLoading] = useState(false);
  const [searchError, setSearchError] = useState("");
  const [showEntry, setShowEntry] = useState(false);

  async function runSearch(keyword: string) {
    if (!keyword.trim()) return;
    setLoading(true);
    setSearchError("");
    try {
      setResults(await api<HistoryResult[]>(`/api/history/search?q=${encodeURIComponent(keyword.trim())}`));
    } catch (reason) {
      setResults([]);
      setSearchError(reason instanceof Error ? reason.message : "查询失败");
    } finally {
      setLoading(false);
    }
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    await runSearch(query);
  }
  return (
    <section>
      <PageHeading eyebrow="HISTORICAL PRICES" title="历史报价查询" detail="从服务器数据库查询产品、参数、型号、品牌和制造商。" action={user.role === "admin" ? <button className="primary-button" onClick={() => setShowEntry(true)}>＋ 手工录入</button> : undefined} />
      <form className="large-search" onSubmit={submit}><span>⌕</span><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="输入产品名称、参数、型号、品牌或制造商" /><button className="primary-button">{loading ? "查询中…" : "查询"}</button></form>
      {searchError && <div className="form-error">{searchError}</div>}
      <div className="history-results">{results.map((item) => <article key={item.id}><div><h3>{item.name}</h3><strong>¥ {money(item.price)}</strong></div><span>{item.model || "无型号"} · {item.brand || "无品牌"} · {item.manufacturer || "无制造商"} · {item.unit || "无单位"}</span><p>{item.spec || "无参数描述"}</p><footer>{item.quote_date || "日期未记录"}<b>{item.source}</b></footer></article>)}{query && !loading && !searchError && !results.length && <div className="empty-state compact"><strong>没有找到相关历史报价</strong><span>可缩短关键词或改用产品名称。</span></div>}</div>
      {showEntry && <HistoryEntryModal onClose={() => setShowEntry(false)} onSaved={async () => { setShowEntry(false); notify("历史报价已录入。"); await runSearch(query); }} />}
    </section>
  );
}

function HistoryEntryModal({ onClose, onSaved }: { onClose: () => void; onSaved: () => Promise<void> }) {
  const [name, setName] = useState("");
  const [price, setPrice] = useState("");
  const [spec, setSpec] = useState("");
  const [unit, setUnit] = useState("");
  const [manufacturer, setManufacturer] = useState("");
  const [brand, setBrand] = useState("");
  const [model, setModel] = useState("");
  const [quoteDate, setQuoteDate] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    const parsed = Number(price);
    if (!name.trim()) { setError("请填写产品名称。"); return; }
    if (!Number.isFinite(parsed) || parsed <= 0) { setError("请填写有效的价格（大于 0 的数字）。"); return; }
    setBusy(true);
    setError("");
    try {
      const payload: Record<string, string | number> = { name: name.trim(), price: parsed };
      if (spec.trim()) payload.spec = spec.trim();
      if (unit.trim()) payload.unit = unit.trim();
      if (manufacturer.trim()) payload.manufacturer = manufacturer.trim();
      if (brand.trim()) payload.brand = brand.trim();
      if (model.trim()) payload.model = model.trim();
      if (quoteDate) payload.quote_date = quoteDate;
      await api<HistoryRecord>("/api/history", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      await onSaved();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "录入失败");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="modal-backdrop">
      <button className="drawer-dismiss" aria-label="关闭手工录入" onClick={onClose} />
      <form className="modal-card" onSubmit={submit}>
        <header><h3>手工录入历史报价</h3><button type="button" onClick={onClose} aria-label="关闭">×</button></header>
        <label><span>产品名称 *</span><input value={name} onChange={(event) => setName(event.target.value)} required /></label>
        <label><span>价格（元）*</span><input type="number" min="0.01" step="0.01" value={price} onChange={(event) => setPrice(event.target.value)} required /></label>
        <div className="modal-grid">
          <label><span>参数</span><input value={spec} onChange={(event) => setSpec(event.target.value)} /></label>
          <label><span>单位</span><input value={unit} onChange={(event) => setUnit(event.target.value)} /></label>
          <label><span>制造商</span><input value={manufacturer} onChange={(event) => setManufacturer(event.target.value)} /></label>
          <label><span>品牌</span><input value={brand} onChange={(event) => setBrand(event.target.value)} /></label>
          <label><span>型号</span><input value={model} onChange={(event) => setModel(event.target.value)} /></label>
          <label><span>报价日期</span><input type="date" value={quoteDate} onChange={(event) => setQuoteDate(event.target.value)} /></label>
        </div>
        {error && <div className="form-error">{error}</div>}
        <div className="modal-actions"><button type="button" className="secondary-button" onClick={onClose}>取消</button><button className="primary-button" disabled={busy}>{busy ? "正在保存…" : "保存记录"}</button></div>
      </form>
    </div>
  );
}

function GovernanceView({ data, user, onChanged, notify }: { data: Governance | null; user: User; onChanged: () => Promise<void>; notify: (message: string) => void }) {
  const [dedupBusy, setDedupBusy] = useState(false);
  const [importFile, setImportFile] = useState<File | null>(null);
  const [importBusy, setImportBusy] = useState(false);
  const [importResult, setImportResult] = useState("");

  async function dedup() {
    setDedupBusy(true);
    try {
      const result = await api<{ removed: number }>("/api/governance/dedup-history", { method: "POST" });
      notify(`本次合并 ${result.removed} 条重复记录。`);
      await onChanged();
    } catch (reason) {
      notify(reason instanceof Error ? reason.message : "去重失败");
    } finally {
      setDedupBusy(false);
    }
  }

  async function importHistory() {
    if (!importFile) return;
    setImportBusy(true);
    setImportResult("");
    try {
      const formData = new FormData();
      formData.set("file", importFile);
      const result = await api<{ inserted: number; skipped_duplicates: number; skipped_invalid: number }>("/api/governance/import-history", { method: "POST", body: formData });
      const summary = `新增 ${result.inserted} 条 / 重复跳过 ${result.skipped_duplicates} 条 / 无效 ${result.skipped_invalid} 条`;
      setImportResult(summary);
      notify(`历史报价导入完成：${summary}。`);
      setImportFile(null);
      await onChanged();
    } catch (reason) {
      setImportResult(reason instanceof Error ? reason.message : "导入失败");
    } finally {
      setImportBusy(false);
    }
  }

  if (!data) return <section><div className="empty-state">正在加载数据治理摘要…</div></section>;
  return <section><PageHeading eyebrow="DATA GOVERNANCE" title="数据治理中心" detail="在报价之前先暴露重复、单位和价格口径问题。" action={user.role === "admin" ? <button className="primary-button" disabled={dedupBusy} onClick={() => void dedup()}>{dedupBusy ? "正在去重…" : "一键去重历史报价"}</button> : undefined} /><div className="governance-grid"><Metric label="历史记录" value={data.record_count} tone="teal" /><Metric label="同名重复组" value={data.duplicate_name_groups} tone="slate" /><Metric label="同名多单位组" value={data.unit_conflict_groups} tone="red" /><Metric label="同名多价格组" value={data.price_conflict_groups} tone="amber" /><Metric label="缺制造商记录" value={data.missing_manufacturer_count} tone="blue" /><Metric label="审计事件" value={data.audit_event_count} tone="slate" /></div>{user.role === "admin" && <div className="form-card import-card"><div><h3>导入历史报价</h3><p>支持 .xlsx / .xlsm / .xls 文件，重复与无效行会自动跳过并计数。</p></div><label className="import-file"><input type="file" accept=".xlsx,.xlsm,.xls" onChange={(event) => { setImportFile(event.target.files?.[0] ?? null); setImportResult(""); }} /><span>{importFile ? importFile.name : "选择历史报价 Excel"}</span></label><button className="primary-button" disabled={!importFile || importBusy} onClick={() => void importHistory()}>{importBusy ? "正在导入…" : "上传导入"}</button>{importResult && <span className="import-result">{importResult}</span>}</div>}<div className="governance-panels"><article><p className="eyebrow">MATCHING POLICY</p><h3>当前匹配权重</h3><div className="weight-list">{Object.entries(data.matching_weights).map(([label, value]) => <div key={label}><span>{label}</span><i><b style={{ width: `${value * 2}%` }} /></i><strong>{value}%</strong></div>)}</div><p>编码精确命中优先；低于55分不作为可靠匹配；阻断风险不能被批量确认。</p></article><article><p className="eyebrow">UNIT POLICY</p><h3>单位校验边界</h3><ul><li><b>绿色</b>单位完全一致</li><li><b>橙色</b>克/千克、毫升/升等可换算</li><li><b>红色</b>瓶/盒/套与克等不可直接换算</li></ul><p>包装单位只有在记录净含量后才能折算，避免错误单价进入客户报价。</p></article></div></section>;
}

function ChangePasswordModal({ onClose, notify }: { onClose: () => void; notify: (message: string) => void }) {
  const [oldPassword, setOldPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (newPassword.length < 6) { setError("新密码至少 6 位。"); return; }
    if (newPassword !== confirmPassword) { setError("两次输入的新密码不一致。"); return; }
    setBusy(true);
    setError("");
    try {
      await api<{ ok: boolean }>("/api/auth/change-password", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ old_password: oldPassword, new_password: newPassword }),
      });
      notify("密码已修改，下次登录请使用新密码。");
      onClose();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "修改失败");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="modal-backdrop">
      <button className="drawer-dismiss" aria-label="关闭修改密码" onClick={onClose} />
      <form className="modal-card" onSubmit={submit}>
        <header><h3>修改密码</h3><button type="button" onClick={onClose} aria-label="关闭">×</button></header>
        <label><span>旧密码</span><input type="password" value={oldPassword} onChange={(event) => setOldPassword(event.target.value)} autoComplete="current-password" required /></label>
        <label><span>新密码（至少 6 位）</span><input type="password" value={newPassword} onChange={(event) => setNewPassword(event.target.value)} autoComplete="new-password" required /></label>
        <label><span>确认新密码</span><input type="password" value={confirmPassword} onChange={(event) => setConfirmPassword(event.target.value)} autoComplete="new-password" required /></label>
        {error && <div className="form-error">{error}</div>}
        <div className="modal-actions"><button type="button" className="secondary-button" onClick={onClose}>取消</button><button className="primary-button" disabled={busy}>{busy ? "正在提交…" : "确认修改"}</button></div>
      </form>
    </div>
  );
}

function DatabaseManager({ onChanged, notify }: { onChanged: () => Promise<void>; notify: (message: string) => void }) {
  const [data, setData] = useState<DatabaseList | null>(null);
  const [newName, setNewName] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    try {
      setData(await api<DatabaseList>("/api/databases"));
      setError("");
    } catch {
      setError("无法连接服务器，请确认后端已启动后点重试");
    }
  }, []);
  // Fetching the server-owned database list is the synchronization purpose of this effect.
  // eslint-disable-next-line react-hooks/set-state-in-effect
  useEffect(() => { void load(); }, [load]);

  async function switchTo(name: string) {
    setBusy(true);
    setError("");
    try {
      await api<{ ok: boolean }>("/api/databases/switch", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name }),
      });
      notify(`已切换到数据库「${name}」`);
      await load();
      await onChanged();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "切换失败");
    } finally {
      setBusy(false);
    }
  }

  async function createNew(event: FormEvent) {
    event.preventDefault();
    const name = newName.trim();
    if (!name) {
      setError("请先输入数据库名称");
      return;
    }
    if (!/^[A-Za-z0-9_-]+$/.test(name)) {
      setError("数据库名称只能包含字母、数字、下划线、连字符（如 saitel_demo）");
      return;
    }
    setBusy(true);
    setError("");
    try {
      await api<{ ok: boolean }>("/api/databases/switch", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name }),
      });
      notify(`已新建并切换到空库「${name}」，请到「数据治理」导入价目本`);
      setNewName("");
      await load();
      await onChanged();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "新建失败");
    } finally {
      setBusy(false);
    }
  }

  return (
    <section>
      <PageHeading eyebrow="DATABASE ADMINISTRATION" title="数据库管理" detail="新建空库、切换历史库。切换后请到「数据治理」导入价目本。" />
      {error && <div className="form-error">{error}</div>}
      <div className="form-card import-card">
        <div>
          <h3>新建空库</h3>
          <p>输入库名（字母/数字/下划线，如 saitel_demo）→ 新建并自动切换。随后在「数据治理」上传赛特尔25年.xls 等价目本。</p>
        </div>
        <form onSubmit={createNew} style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
          <input
            value={newName}
            onChange={(event) => { setNewName(event.target.value); if (error) setError(""); }}
            placeholder="输入库名，如 saitel_demo"
            autoFocus
            style={{ flex: 1, minWidth: 220 }}
          />
          <button className="primary-button" type="submit" disabled={busy}>{busy ? "处理中…" : "新建并切换"}</button>
        </form>
      </div>
      <div className="governance-grid" style={{ gridTemplateColumns: "repeat(auto-fill, minmax(240px, 1fr))" }}>
        {data === null && error && (
          <article className="db-card" style={{ padding: 16, border: "1px solid var(--line)", borderRadius: 12, background: "white" }}>
            <p style={{ margin: 0, color: "var(--muted)", fontSize: 10 }}>数据库列表加载失败</p>
            <button className="secondary-button" style={{ marginTop: 10 }} onClick={() => void load()}>重试</button>
          </article>
        )}
        {(data?.databases ?? []).map((db) => {
          const active = db.name === data?.current;
          return (
            <article key={db.name} className="db-card" style={{ padding: 16, border: active ? "2px solid var(--teal)" : "1px solid var(--line)", borderRadius: 12, background: "white" }}>
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                <h3 style={{ margin: 0 }}>{db.name}{active && <span style={{ color: "var(--teal)", fontSize: 10, marginLeft: 8 }}>● 当前</span>}</h3>
                {db.exists && <strong style={{ color: "#994a10" }}>{db.history_count.toLocaleString("zh-CN")} 条</strong>}
              </div>
              <p style={{ margin: "8px 0 0", color: "var(--muted)", fontSize: 10 }}>
                {db.exists ? `${db.size_mb} MB · ${db.job_count} 个任务` : "尚未创建"}
              </p>
              {!active && (
                <button className="secondary-button" style={{ marginTop: 10 }} disabled={busy} onClick={() => void switchTo(db.name)}>
                  切换到此库
                </button>
              )}
            </article>
          );
        })}
      </div>
    </section>
  );
}

function AccountAdmin({ notify }: { notify: (message: string) => void }) {
  const [accounts, setAccounts] = useState<AccountUser[]>([]);
  const [username, setUsername] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [password, setPassword] = useState("");
  const [role, setRole] = useState<Role>("quote");
  const [error, setError] = useState("");
  const [resetTarget, setResetTarget] = useState<AccountUser | null>(null);

  const loadAccounts = useCallback(async () => {
    try {
      setAccounts(await api<AccountUser[]>("/api/users"));
      setError("");
    } catch {
      setError("无法连接服务器，请确认后端已启动后点重试");
    }
  }, []);
  // Fetching the server-owned account list is the synchronization purpose of this effect.
  // eslint-disable-next-line react-hooks/set-state-in-effect
  useEffect(() => { void loadAccounts(); }, [loadAccounts]);

  async function createAccount(event: FormEvent) {
    event.preventDefault();
    setError("");
    try {
      await api<AccountUser>("/api/users", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username: username.trim(), password, display_name: displayName.trim(), role }),
      });
      setUsername("");
      setDisplayName("");
      setPassword("");
      setRole("quote");
      notify("账号已创建。");
      await loadAccounts();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "创建失败");
    }
  }

  async function toggleActive(account: AccountUser) {
    try {
      await api<AccountUser>(`/api/users/${account.id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ active: !account.active }),
      });
      notify(account.active ? `账号「${account.username}」已停用。` : `账号「${account.username}」已启用。`);
      await loadAccounts();
    } catch (reason) {
      notify(reason instanceof Error ? reason.message : "操作失败");
    }
  }

  return (
    <section>
      <PageHeading eyebrow="ACCOUNT ADMINISTRATION" title="账号管理" detail="创建报价账号、重置密码、停用或启用访问权限。" />
      <form className="inline-form account-form" onSubmit={createAccount}>
        <label><span>用户名</span><input value={username} onChange={(event) => setUsername(event.target.value)} autoComplete="off" required /></label>
        <label><span>显示名</span><input value={displayName} onChange={(event) => setDisplayName(event.target.value)} required /></label>
        <label><span>初始密码</span><input type="password" value={password} onChange={(event) => setPassword(event.target.value)} autoComplete="new-password" required /></label>
        <label><span>角色</span><select value={role} onChange={(event) => setRole(event.target.value as Role)}><option value="quote">报价员</option><option value="admin">管理员</option></select></label>
        <button className="primary-button">＋ 新建账号</button>
        {error && <div className="form-error">{error}</div>}
      </form>
      <div className="review-card">
        {accounts.length === 0 && error && (
          <div style={{ padding: "13px 14px", borderBottom: "1px solid var(--line)" }}>
            <button className="secondary-button" onClick={() => void loadAccounts()}>重试</button>
          </div>
        )}
        <div className="table-scroll account-table">
          <table className="quote-table">
            <thead><tr><th>用户名</th><th>显示名</th><th>角色</th><th>状态</th><th>创建时间</th><th>操作</th></tr></thead>
            <tbody>
              {accounts.map((account) => (
                <tr key={account.id}>
                  <td className="mono">{account.username}</td>
                  <td>{account.display_name}</td>
                  <td>{account.role === "admin" ? "管理员" : "报价员"}</td>
                  <td><span className={`status-pill ${account.active ? "status-confirmed" : "status-failed"}`}>{account.active ? "启用中" : "已停用"}</span></td>
                  <td className="mono">{formatDate(account.created_at)}</td>
                  <td>
                    <div className="job-card-actions">
                      <button className="card-action-button" onClick={() => setResetTarget(account)}>重置密码</button>
                      <button className={`card-action-button ${account.active ? "danger" : ""}`} onClick={() => void toggleActive(account)}>{account.active ? "停用" : "启用"}</button>
                    </div>
                  </td>
                </tr>
              ))}
              {accounts.length === 0 && <tr><td colSpan={6}><div className="table-empty">暂无账号数据</div></td></tr>}
            </tbody>
          </table>
        </div>
      </div>
      {resetTarget && <ResetPasswordModal account={resetTarget} onClose={() => setResetTarget(null)} notify={notify} />}
    </section>
  );
}

function ResetPasswordModal({ account, onClose, notify }: { account: AccountUser; onClose: () => void; notify: (message: string) => void }) {
  const [newPassword, setNewPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (newPassword.length < 6) { setError("新密码至少 6 位。"); return; }
    setBusy(true);
    setError("");
    try {
      await api<AccountUser>(`/api/users/${account.id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ password: newPassword }),
      });
      notify(`账号「${account.username}」的密码已重置。`);
      onClose();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "重置失败");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="modal-backdrop">
      <button className="drawer-dismiss" aria-label="关闭重置密码" onClick={onClose} />
      <form className="modal-card" onSubmit={submit}>
        <header><h3>重置密码 · {account.display_name || account.username}</h3><button type="button" onClick={onClose} aria-label="关闭">×</button></header>
        <label><span>新密码（至少 6 位）</span><input type="password" value={newPassword} onChange={(event) => setNewPassword(event.target.value)} autoComplete="new-password" required /></label>
        {error && <div className="form-error">{error}</div>}
        <div className="modal-actions"><button type="button" className="secondary-button" onClick={onClose}>取消</button><button className="primary-button" disabled={busy}>{busy ? "正在提交…" : "确认重置"}</button></div>
      </form>
    </div>
  );
}
