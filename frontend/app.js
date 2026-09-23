"use strict";
// 唯讀查詢前端（vanilla）。讀 /api/*，畫 KPI/分帶/嚴重度，點擊下鑽明細。
// 「我的部門」記在瀏覽器 localStorage（無帳號，決策 no-auth-v1）。
const $ = (s) => document.querySelector(s);
const DEPT_KEY = "webvuln.myDept";
let curRows = [];
let sortK = null, sortAsc = true;

function dept() { return $("#dept").value || "全部"; }

async function j(url) { const r = await fetch(url); if (!r.ok) throw new Error(url + " " + r.status); return r.json(); }

async function loadDepts() {
  const ds = await j("/api/departments");
  const sel = $("#dept");
  sel.innerHTML = '<option value="全部">全部部門</option>' + ds.map(d => `<option>${esc(d)}</option>`).join("");
  const saved = localStorage.getItem(DEPT_KEY);
  if (saved && [...sel.options].some(o => o.value === saved)) sel.value = saved;
}

function esc(s) { return String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])); }

async function refresh() {
  localStorage.setItem(DEPT_KEY, dept());
  const s = await j("/api/summary?department=" + encodeURIComponent(dept()));
  renderKpi(s); renderBands(s); renderSeverity(s); renderBanner(s);
  drill({ status: "未結案" }, "未結案明細");
}

function kpi(num, lab, cls, on) {
  return `<div class="kpi ${cls || ""}" data-on='${JSON.stringify(on || {})}'><div class="num">${num}</div><div class="lab">${lab}</div></div>`;
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
    el.onclick = () => { const on = JSON.parse(el.dataset.on); if (on.status) drill(on, el.querySelector(".lab").textContent + " 明細"); });
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

async function drill(params, title) {
  const q = new URLSearchParams({ department: dept(), ...params });
  const kw = $("#kw").value.trim(); if (kw) q.set("keyword", kw);
  curRows = await j("/api/findings?" + q.toString());
  $("#detail-title").textContent = title || "明細";
  sortK = null; renderTable();
}
function renderTable() {
  let rows = curRows.slice();
  if (sortK) rows.sort((a, b) => {
    const x = a[sortK] ?? "", y = b[sortK] ?? "";
    return (x > y ? 1 : x < y ? -1 : 0) * (sortAsc ? 1 : -1);
  });
  $("#detail tbody").innerHTML = rows.map(r => `<tr>
    <td>${esc(r.host)}</td><td>${esc(r.owner)}</td><td>${esc(r.severity)}</td>
    <td>${esc(r.name)}</td><td>${esc(r.plugin_id)}</td><td>${esc(r.effective_due)}</td>
    <td class="${r.overdue_days > 0 ? "od" : ""}">${r.overdue_days ?? ""}</td>
    <td>${esc(r.stage)}</td><td>${esc(r.department)}</td></tr>`).join("");
  $("#detail-count").textContent = `共 ${rows.length} 筆`;
}
document.querySelectorAll("#detail thead th").forEach(th =>
  th.onclick = () => { const k = th.dataset.k; sortAsc = sortK === k ? !sortAsc : true; sortK = k; renderTable(); });

function csv() {
  const cols = ["host", "owner", "severity", "name", "plugin_id", "effective_due", "overdue_days", "stage", "department"];
  const head = ["主機", "負責人", "嚴重度", "弱點", "PluginID", "到期日", "逾期天數", "處置階段", "部門"];
  const lines = [head.join(",")].concat(curRows.map(r => cols.map(c => `"${String(r[c] ?? "").replace(/"/g, '""')}"`).join(",")));
  const blob = new Blob(["﻿" + lines.join("\r\n")], { type: "text/csv" });
  const a = document.createElement("a"); a.href = URL.createObjectURL(blob); a.download = "findings.csv"; a.click();
}

async function uploadJson(file) {
  const text = await file.text();
  let payload; try { payload = JSON.parse(text); } catch { alert("不是有效的 JSON"); return; }
  const r = await fetch("/api/import", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
  if (!r.ok) { alert("匯入失敗：" + r.status); return; }
  const b = await r.json(); alert(`匯入成功：${b.row_count} 筆（batch ${b.batch_id}）`);
  await loadDepts(); await refresh();
}

$("#dept").onchange = refresh;
$("#btn-search").onclick = () => drill({ status: "未結案" }, "查詢結果");
$("#kw").addEventListener("keydown", e => { if (e.key === "Enter") drill({ status: "未結案" }, "查詢結果"); });
$("#btn-csv").onclick = csv;
$("#file").addEventListener("change", e => { if (e.target.files[0]) uploadJson(e.target.files[0]); });

(async () => {
  try { const h = await j("/api/health"); $("#ver").textContent = "v0.1"; } catch {}
  await loadDepts(); await refresh();
})().catch(e => { $("#banner").classList.remove("hidden"); $("#banner").textContent = "載入失敗，請先匯入資料。(" + e.message + ")"; });
