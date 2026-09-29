"use strict";
// 整合前端（vanilla）。讀 /api/* 畫首屏/KPI/分帶/嚴重度/SLA/排行/管線/結案；登入後可推進承辦狀態。
// 「我的部門」記在瀏覽器 localStorage。
const $ = (s) => document.querySelector(s);
const DEPT_KEY = "webvuln.myDept";
const PIPE = ["未申請", "待主管", "待資安", "核准", "完成", "退回補件"];
const NEXT = { "未申請": ["待主管"], "待主管": ["待資安", "退回補件"], "待資安": ["核准", "退回補件"],
  "核准": ["完成"], "退回補件": ["待主管"], "完成": [] };
let curRows = [], sortK = null, sortAsc = true;
let me = { authenticated: false }, rankBy = "owner", rankExpand = false;

const esc = (s) => String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const overdueTxt = (d) => d == null ? "無到期日" : (d > 0 ? `逾期 ${d} 天` : d === 0 ? "今天到期" : `距到期 ${-d} 天`);
const dept = () => $("#dept").value || "全部";
const canWrite = () => me.authenticated && (me.role === "admin" || me.role === "承辦");
async function j(url) { const r = await fetch(url); if (!r.ok) throw new Error(url + " " + r.status); return r.json(); }

// ── 登入 ──
async function loadMe() {
  try { me = await j("/api/me"); } catch { me = { authenticated: false }; }
  const chip = $("#userchip");
  if (me.authenticated) {
    chip.textContent = `${me.display_name || me.username}（${me.role}）`;
    chip.classList.remove("hidden"); $("#btn-login").classList.add("hidden"); $("#btn-logout").classList.remove("hidden");
  } else {
    chip.classList.add("hidden"); $("#btn-login").classList.remove("hidden"); $("#btn-logout").classList.add("hidden");
  }
}
function openLogin() { $("#li-err").classList.add("hidden"); $("#li-user").value = ""; $("#li-pass").value = ""; $("#login-modal").classList.remove("hidden"); $("#li-user").focus(); }
function closeLogin() { $("#login-modal").classList.add("hidden"); }
async function doLogin() {
  const body = { username: $("#li-user").value.trim(), password: $("#li-pass").value };
  const r = await fetch("/api/login", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  if (!r.ok) { const e = await r.json().catch(() => ({})); $("#li-err").textContent = e.detail || "登入失敗"; $("#li-err").classList.remove("hidden"); return; }
  closeLogin(); await loadMe(); await loadCases(lastCaseFilter); // 重繪案件操作欄
}
async function doLogout() { await fetch("/api/logout", { method: "POST" }); await loadMe(); await loadCases(lastCaseFilter); }

// ── 部門 ──
async function loadDepts() {
  const ds = await j("/api/departments");
  const sel = $("#dept");
  sel.innerHTML = '<option value="全部">全部部門</option>' + ds.map(d => `<option>${esc(d)}</option>`).join("");
  const saved = localStorage.getItem(DEPT_KEY);
  if (saved && [...sel.options].some(o => o.value === saved)) sel.value = saved;
}

// ── 首屏 ──
function renderHome(s, top) {
  const el = $("#oneline");
  const bad = s.should_apply + s.gaps.no_owner + s.gaps.no_due + s.overdue;
  el.classList.toggle("g", bad === 0);
  const parts = [];
  if (s.should_apply) parts.push(`${s.should_apply} 件已過申請線`);
  if (s.gaps.no_owner) parts.push(`${s.gaps.no_owner} 件無人負責`);
  if (s.due_soon) parts.push(`${s.due_soon} 件近期到期`);
  const head = bad === 0 ? "目前無急件，狀態良好。" : ("本資料：" + (parts.join("、") || `${s.overdue} 件已逾期`) + "。");
  const t0 = top[0];
  const sub = t0 ? `最急：${esc(t0.owner || "未指派")} ${esc(t0.name || t0.plugin_id || "")}（${esc(t0.severity)}，${overdueTxt(t0.overdue_days)}${t0.should_apply ? "、應提申請未提" : ""}）` : "—";
  el.innerHTML = `${esc(head)}<small>${sub} · 資料 ${s.freshness.days_ago ?? "?"} 天前</small>`;

  $("#top3").innerHTML = top.slice(0, 3).map((r, i) => `
    <div class="tc"><div class="rank">${i + 1}</div>
      <div class="info"><b>${esc(r.owner || "未指派")} · ${esc(r.name || r.plugin_id || "")}</b>
        <div>${esc(r.severity)} · ${overdueTxt(r.overdue_days)} · ${esc(r.stage)}${r.should_apply ? " · 應提申請未提" : ""}</div></div>
    </div>`).join("") || '<div class="muted">無急件</div>';

  $("#rednums").innerHTML =
    nz("應提申請未提", s.should_apply, { status: "未結案", should_apply: true }) +
    nz("無人負責", s.gaps.no_owner, { status: "未結案", no_owner: true }) +
    nz("無到期日", s.gaps.no_due, { status: "未結案", no_due: true }) +
    nz("已逾期", s.overdue, { status: "未結案", band: "已逾期" });
  document.querySelectorAll("#rednums .nz").forEach(e =>
    e.onclick = () => drill(JSON.parse(e.dataset.on), e.dataset.title));
}
const nz = (lab, n, on) => `<span class="nz" data-on='${JSON.stringify(on)}' data-title="${esc(lab)}">${esc(lab)}<b>${n}</b></span>`;

// ── KPI / 分帶 / 嚴重度 ──
function kpi(num, lab, cls, on) {
  return `<div class="kpi ${cls || ""}" data-on='${JSON.stringify(on || {})}' data-title="${esc(lab)}"><div class="num">${num}</div><div class="lab">${esc(lab)}</div></div>`;
}
function renderKpi(s) {
  $("#kpi").innerHTML =
    kpi(s.unresolved, "未結案", "", { status: "未結案" }) +
    kpi(s.overdue, "已逾期", "red", { status: "未結案", band: "已逾期" }) +
    kpi(s.due_soon, "近期到期(30天)", "amber", { status: "未結案", band: "30天內" }) +
    kpi(s.high_risk, "高風險未結", "red", { status: "未結案" }) +
    kpi(s.closed, "已結案", "", { status: "已結案" }) +
    kpi(s.close_rate + "%", "整體結案率", "", { status: "全部" });
  document.querySelectorAll(".kpi").forEach(el =>
    el.onclick = () => drill(JSON.parse(el.dataset.on), el.dataset.title + " 明細"));
}
function renderBands(s) {
  $("#bands").innerHTML = Object.entries(s.bands)
    .map(([k, v]) => `<span class="chip" data-band="${esc(k)}">${esc(k)} <b>${v}</b></span>`).join("");
  document.querySelectorAll("#bands .chip").forEach(el =>
    el.onclick = () => drill({ status: "未結案", band: el.dataset.band }, "未結案 · " + el.dataset.band));
}
function renderSeverity(s) {
  $("#severity").innerHTML = Object.entries(s.severity)
    .map(([k, v]) => `<span class="chip" data-sev="${k}">${k} <b>${v}</b></span>`).join("");
  document.querySelectorAll("#severity .chip").forEach(el =>
    el.onclick = () => drill({ status: "未結案", severity: el.dataset.sev }, "未結案 · " + el.dataset.sev));
}
function renderBanner(s) {
  const b = $("#banner");
  if (s.overdue > 0) { b.classList.remove("hidden"); b.textContent = `⚠ ${dept()} 有 ${s.overdue} 筆已逾期尚未結案`; }
  else b.classList.add("hidden");
}

// ── SLA ──
async function loadSla() {
  const rows = await j("/api/sla?department=" + encodeURIComponent(dept()));
  $("#sla tbody").innerHTML = rows.map(r => `<tr>
    <td class="sev">${esc(r.severity)}</td><td>${r.policy_days ?? "—"}</td><td>${r.unresolved}</td>
    <td class="${r.overdue ? "warnr" : ""}">${r.overdue}</td>
    <td class="${r.met_rate < 80 ? "warnr" : ""}">${r.met_rate}%</td></tr>`).join("");
}

// ── 排行榜 ──
async function loadRank() {
  const url = rankBy === "department" ? "/api/ranking?by=department"
    : "/api/ranking?by=owner&department=" + encodeURIComponent(dept());
  let rows = await j(url);
  if (!rankExpand) rows = rows.slice(0, 8);
  $("#rank tbody").innerHTML = rows.map((r, i) => `<tr>
    <td>${i + 1}</td><td><b>${esc(r.name)}</b></td><td>${r.unresolved}</td>
    <td class="${r.overdue ? "warnr" : ""}">${r.overdue}</td>
    <td class="${r.should_apply ? "warnr" : ""}">${r.should_apply}</td>
    <td>${r.high_risk}</td><td>${r.close_rate}%</td></tr>`).join("");
  $("#rank-expand").textContent = rankExpand ? "收合" : "展開全部";
}

// ── 申請管線 ──
async function loadPipeline() {
  const rows = await j("/api/cases?department=" + encodeURIComponent(dept()));
  const cnt = Object.fromEntries(PIPE.map(k => [k, 0]));
  let suspect = 0, orphan = 0;
  rows.forEach(c => { if (c.status in cnt) cnt[c.status]++; if (c.suspect) suspect++; if (c.is_orphan) orphan++; });
  $("#pipe").innerHTML = PIPE.map(k => {
    const warn = (k === "未申請" || k === "退回補件") && cnt[k] > 0;
    return `<div class="pb ${warn ? "warn" : ""}" data-st="${esc(k)}"><div class="n">${cnt[k]}</div><div class="k">${esc(k)}</div></div>`;
  }).join("");
  document.querySelectorAll("#pipe .pb").forEach(el =>
    el.onclick = () => loadCases({ status: el.dataset.st }));
  const sent = cnt["待主管"] + cnt["待資安"] + cnt["核准"] + cnt["完成"] + cnt["退回補件"];
  $("#pipe-hint").innerHTML = `已申請送出共 <b>${sent}</b> 筆。`
    + (suspect ? ` <span class="warnr">可疑聲稱 ${suspect}</span>。` : "")
    + (orphan ? ` 來源已消失(多半修好) ${orphan}。` : "")
    + " 點任一關看該批案件。";
}

// ── 承辦案件（點管線後顯示，登入可推進） ──
let lastCaseFilter = null;
async function loadCases(filter) {
  lastCaseFilter = filter;
  if (!filter) { $("#sec-cases").classList.add("hidden"); return; }
  const q = new URLSearchParams({ department: dept() });
  if (filter.status) q.set("status", filter.status);
  if (filter.suspect) q.set("suspect", "true");
  const rows = await j("/api/cases?" + q.toString());
  $("#cases-title").textContent = "承辦案件 · " + (filter.status || (filter.suspect ? "可疑聲稱" : "全部")) + `（${rows.length}）`;
  $("#cases tbody").innerHTML = rows.map(c => {
    const acts = canWrite() ? (NEXT[c.status] || []).map(to =>
      `<button class="btn btn-sm" data-id="${c.id}" data-to="${esc(to)}">→${esc(to)}</button>`).join(" ")
      : (me.authenticated ? "—" : '<span class="muted">登入後可操作</span>');
    return `<tr>
      <td>${esc(c.host)}</td><td>${esc(c.plugin_id)}</td><td>${esc(c.owner || "未指派")}</td>
      <td>${esc(c.department)}</td><td>${esc(c.status)}${c.suspect ? ' <span class="warnr">可疑</span>' : ""}${c.is_orphan ? ' <span class="muted">已消失</span>' : ""}</td>
      <td>${c.source_closed ? "已結" : "未結"}</td><td>${acts}</td></tr>`;
  }).join("") || '<tr><td colspan="7" class="muted">無案件</td></tr>';
  $("#sec-cases").classList.remove("hidden");
  document.querySelectorAll("#cases button[data-to]").forEach(b =>
    b.onclick = () => transition(b.dataset.id, b.dataset.to));
  $("#sec-cases").scrollIntoView({ behavior: "smooth", block: "start" });
}
async function transition(id, to) {
  const r = await fetch(`/api/cases/${id}/transition`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ to }) });
  if (!r.ok) { const e = await r.json().catch(() => ({})); alert("失敗：" + (e.detail || r.status)); return; }
  await loadCases(lastCaseFilter); await loadPipeline(); await loadClose();
}

// ── 結案統計 ──
async function loadClose() {
  const s = await j("/api/close-stats?department=" + encodeURIComponent(dept()));
  $("#closerep").innerHTML = `
    <div>本期新結案 <b>${s.new_closed}</b> 筆</div>
    <div>來源(Excel)確認 <b style="color:var(--green)">${s.source_confirmed}</b></div>
    <div><span class="${s.claimed_unconfirmed ? "warnr" : ""}">承辦聲稱但來源未確認 ${s.claimed_unconfirmed}</span>（可疑，待查）</div>
    <div class="muted">比對批次 ${s.prev_batch ?? "—"} → ${s.latest_batch ?? "—"}</div>`;
  $("#closetbl tbody").innerHTML = (s.by_closer || []).map(r =>
    `<tr><td>${esc(r.name)}</td><td>${r.closed}</td></tr>`).join("") || '<tr><td colspan="2" class="muted">無</td></tr>';
}

// ── 明細下鑽 ──
async function drill(params, title) {
  const q = new URLSearchParams({ department: dept(), ...params });
  const kw = $("#kw").value.trim(); if (kw) q.set("keyword", kw);
  curRows = await j("/api/findings?" + q.toString());
  $("#detail-title").textContent = title || "明細";
  sortK = null; renderTable();
  $("#detail-title").scrollIntoView({ behavior: "smooth", block: "start" });
}
function renderTable() {
  let rows = curRows.slice();
  if (sortK) rows.sort((a, b) => { const x = a[sortK] ?? "", y = b[sortK] ?? ""; return (x > y ? 1 : x < y ? -1 : 0) * (sortAsc ? 1 : -1); });
  $("#detail tbody").innerHTML = rows.map(r => `<tr>
    <td>${esc(r.host)}</td><td>${esc(r.owner)}</td><td>${esc(r.severity)}</td>
    <td>${esc(r.name)}</td><td>${esc(r.plugin_id)}</td><td>${esc(r.effective_due)}</td>
    <td class="${r.overdue_days > 0 ? "od" : ""}">${r.overdue_days ?? ""}</td>
    <td class="${r.should_apply ? "od" : ""}">${esc(r.action_line)}</td>
    <td>${esc(r.stage)}</td><td>${esc(r.department)}</td></tr>`).join("");
  $("#detail-count").textContent = `共 ${rows.length} 筆`;
}
function csv() {
  const cols = ["host", "owner", "severity", "name", "plugin_id", "effective_due", "overdue_days", "action_line", "stage", "department"];
  const head = ["主機", "負責人", "嚴重度", "弱點", "PluginID", "到期日", "逾期天數", "行動線", "處置階段", "部門"];
  const lines = [head.join(",")].concat(curRows.map(r => cols.map(c => `"${String(r[c] ?? "").replace(/"/g, '""')}"`).join(",")));
  const blob = new Blob(["﻿" + lines.join("\r\n")], { type: "text/csv" });
  const a = document.createElement("a"); a.href = URL.createObjectURL(blob); a.download = "findings.csv"; a.click();
}

// ── Excel 匯入（沿用單機版解析：window.MultiSheet）──
function isoLocal(d) {
  if (!d) return null;
  if (typeof d === "string") return d.slice(0, 10) || null;
  if (d instanceof Date && !isNaN(d)) {
    const m = String(d.getMonth() + 1).padStart(2, "0"), day = String(d.getDate()).padStart(2, "0");
    return `${d.getFullYear()}-${m}-${day}`;
  }
  return null;
}
// raw 裡若有 Date 物件(cellDates 解析)，轉成 yyyy/mm/dd 再送，否則 JSON 會變 UTC ISO，
// 原封匯出就顯示成怪字串而非來源原貌。其餘型別原樣保留。
function cleanRaw(raw) {
  if (!raw || typeof raw !== "object") return raw;
  const out = {};
  for (const k in raw) {
    const v = raw[k];
    out[k] = (v instanceof Date && !isNaN(v))
      ? `${v.getFullYear()}/${String(v.getMonth() + 1).padStart(2, "0")}/${String(v.getDate()).padStart(2, "0")}`
      : v;
  }
  return out;
}
function recToFinding(r) {
  return {
    sheet_key: r.sheet, plugin_id: r.pluginId || null, name: r.name || null, host: r.host || null,
    severity: (r.severity && r.severity !== "Unknown") ? r.severity : null,
    severity_raw: r.severityRaw || null,
    department: r.unit || null,
    owner: (r.owner && r.owner !== "(未指定)") ? r.owner : null,
    remediation_due: isoLocal(r.fixDeadline || r.otherDue),
    first_extension_due: isoLocal(r.firstExtension),
    exception_due: isoLocal(r.exceptionApproval),
    // 結案分類沿用單機版已驗證的 bucket（open/closed/other），不讓後端重判（如「結案中」=未結）
    close_status: r.closeBucket === "closed" ? "已結案" : r.closeBucket === "other" ? "其他" : "未結案",
    close_date: isoLocal(r.closeDate),
    remark: r.remark || null,
    raw: cleanRaw(r.raw) || null,
  };
}
async function uploadXlsx(file) {
  if (!window.MultiSheet) { alert("解析元件未載入"); return; }
  let sheets;
  try { sheets = window.MultiSheet.parseWorkbook(await file.arrayBuffer()); }
  catch (e) { alert("Excel 解析失敗：" + e.message); return; }
  if (!sheets.length) { alert("找不到「數字-」開頭的弱點工作表"); return; }
  const findings = [], sheet_columns = {}, warns = [];
  sheets.forEach(s => {
    sheet_columns[s.name] = s.headers;               // 原始欄序（供原封匯出 1:1）
    s.records.forEach(r => findings.push(recToFinding(r)));
    (s.warnings || []).forEach(w => warns.push(w));
  });
  if (warns.length && !confirm("解析提醒：\n- " + warns.join("\n- ") + "\n\n仍要匯入嗎？")) return;
  const payload = { source_file: file.name, findings, sheet_columns };
  const rp = await fetch("/api/import", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
  if (!rp.ok) { alert("匯入失敗：" + rp.status); return; }
  const b = await rp.json();
  alert(`匯入成功：${b.row_count} 筆（${sheets.length} 張表，batch ${b.batch_id}）`);
  await loadDepts(); await refresh();
}

async function uploadJson(file) {
  const text = await file.text();
  let payload; try { payload = JSON.parse(text); } catch { alert("不是有效的 JSON"); return; }
  const r = await fetch("/api/import", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
  if (!r.ok) { alert("匯入失敗：" + r.status); return; }
  const b = await r.json(); alert(`匯入成功：${b.row_count} 筆（batch ${b.batch_id}）`);
  await loadDepts(); await refresh();
}

// ── 主刷新 ──
async function refresh() {
  localStorage.setItem(DEPT_KEY, dept());
  const s = await j("/api/summary?department=" + encodeURIComponent(dept()));
  const fresh = $("#fresh");
  const days = s.freshness.days_ago;
  fresh.textContent = days == null ? "尚無資料" : `最新匯入：${days} 天前${days <= 7 ? " ✓" : " ⚠"}`;
  fresh.classList.toggle("stale", days != null && days > 7);
  const top = await j("/api/findings?" + new URLSearchParams({ department: dept(), status: "未結案", should_apply: "true" }));
  top.sort((a, b) => (b.overdue_days ?? -1e9) - (a.overdue_days ?? -1e9));
  renderHome(s, top); renderKpi(s); renderBands(s); renderSeverity(s); renderBanner(s);
  await Promise.all([loadSla(), loadRank(), loadPipeline(), loadClose()]);
  drill({ status: "未結案" }, "未結案明細");
}

// ── 事件 ──
document.querySelectorAll("#detail thead th").forEach(th =>
  th.onclick = () => { const k = th.dataset.k; sortAsc = sortK === k ? !sortAsc : true; sortK = k; renderTable(); });
$("#dept").onchange = refresh;
$("#btn-search").onclick = () => drill({ status: "未結案" }, "查詢結果");
$("#kw").addEventListener("keydown", e => { if (e.key === "Enter") drill({ status: "未結案" }, "查詢結果"); });
$("#btn-csv").onclick = csv;
$("#file").addEventListener("change", e => { if (e.target.files[0]) uploadJson(e.target.files[0]); });
$("#xlsx").addEventListener("change", e => { if (e.target.files[0]) { uploadXlsx(e.target.files[0]); e.target.value = ""; } });
$("#btn-export").onclick = () => { window.location = "/api/export"; };
$("#btn-login").onclick = openLogin;
$("#btn-logout").onclick = doLogout;
$("#li-cancel").onclick = closeLogin;
$("#li-submit").onclick = doLogin;
$("#li-pass").addEventListener("keydown", e => { if (e.key === "Enter") doLogin(); });
$("#rank-expand").onclick = (e) => { e.preventDefault(); rankExpand = !rankExpand; loadRank(); };
document.querySelectorAll(".rt").forEach(b => b.onclick = () => {
  document.querySelectorAll(".rt").forEach(x => x.classList.remove("active")); b.classList.add("active");
  rankBy = b.dataset.by; loadRank();
});
document.querySelectorAll(".vt").forEach(b => b.onclick = () => {
  document.querySelectorAll(".vt").forEach(x => x.classList.remove("active")); b.classList.add("active");
  const sup = b.dataset.view === "supervisor";
  document.querySelectorAll(".view-supervisor").forEach(el => el.classList.toggle("hidden", !sup));
});

(async () => {
  try { await j("/api/health"); $("#ver").textContent = "v0.2"; } catch {}
  await loadMe(); await loadDepts(); await refresh();
})().catch(e => { $("#banner").classList.remove("hidden"); $("#banner").textContent = "載入失敗，請先匯入資料。(" + e.message + ")"; });
