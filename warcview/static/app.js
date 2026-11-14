"use strict";

const $ = (sel, el = document) => el.querySelector(sel);
const $$ = (sel, el = document) => [...el.querySelectorAll(sel)];

async function api(path, opts = {}) {
  const r = await fetch(path, opts);
  if (!r.ok) {
    let msg = r.statusText;
    try { msg = (await r.json()).error || msg; } catch (e) { /* ignore */ }
    throw new Error(msg);
  }
  return r.json();
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g,
    c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
const short = (s, n = 12) => s && s.length > n ? s.slice(0, n) + "…" : (s || "");
const fmtSize = n => n > 1 << 20 ? (n / (1 << 20)).toFixed(1) + " MB" : (n / 1024).toFixed(1) + " KB";

function badge(row) {
  if (row.type === "error") return `<span class="badge error">损坏</span>`;
  if (row.type === "revisit") return `<span class="badge revisit">revisit</span>`;
  if (row.status === "decode_error") return `<span class="badge decode_error">解码错误</span>`;
  if (row.type === "response") return `<span class="badge ok">response</span>`;
  return `<span class="badge other">${esc(row.type)}</span>`;
}

function toIso(local) {
  if (!local) return null;
  return local.length === 16 ? local + ":00Z" : local + "Z";
}

const state = {
  archives: [],
  currentArchive: null,
  recOffset: 0,
  currentUrl: null,
  captures: [],
  selected: new Set(),
  pollTimer: null,
};

// ============================== 标签页 ==============================
$$(".tab-btn").forEach(btn => btn.addEventListener("click", () => {
  $$(".tab-btn").forEach(b => b.classList.toggle("active", b === btn));
  ["archives", "pages", "runs"].forEach(t => {
    $("#tab-" + t).hidden = t !== btn.dataset.tab;
  });
  if (btn.dataset.tab === "archives") loadArchives();
  if (btn.dataset.tab === "pages") { fillArchiveSelect(); }
  if (btn.dataset.tab === "runs") loadRuns();
}));

// ============================== 档案 ==============================
async function loadArchives() {
  const data = await api("/api/archives");
  state.archives = data.archives;
  const tbody = $("#archives-table tbody");
  tbody.innerHTML = "";
  for (const a of data.archives) {
    const pct = a.size ? Math.min(100, 100 * a.scan_offset / a.size) : 0;
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${a.id}</td>
      <td>${esc(a.name)}</td>
      <td>${fmtSize(a.size)}</td>
      <td title="${a.sha256}">${short(a.sha256)}</td>
      <td>${esc(a.scan_status)}${a.scan_error ? " ⚠" : ""}</td>
      <td><div class="progress"><div style="width:${pct}%"></div></div>
          <span class="muted">${pct.toFixed(0)}%</span></td>
      <td>${a.record_count}</td>
      <td>${a.error_count || ""}</td>
      <td></td>`;
    const ops = tr.lastElementChild;
    const scanLabel = a.scan_status === "pending" ? "扫描"
      : a.scan_status === "done" ? "重新扫描" : "继续扫描";
    const scanBtn = document.createElement("button");
    scanBtn.textContent = scanLabel;
    scanBtn.onclick = () => startScan(a.id, a.scan_status === "done");
    const stopBtn = document.createElement("button");
    stopBtn.textContent = "停止";
    stopBtn.onclick = () => api(`/api/archives/${a.id}/stop`, { method: "POST" });
    const viewBtn = document.createElement("button");
    viewBtn.textContent = "记录";
    viewBtn.onclick = () => showRecords(a.id);
    ops.append(scanBtn, " ", stopBtn, " ", viewBtn);
    tbody.appendChild(tr);
  }
  const anyScanning = data.archives.some(a => a.scanning || a.scan_status === "scanning");
  clearTimeout(state.pollTimer);
  if (anyScanning) state.pollTimer = setTimeout(loadArchives, 800);
}

async function startScan(id, force) {
  await api(`/api/archives/${id}/scan`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ force }),
  });
  loadArchives();
}

$("#upload-input").addEventListener("change", async ev => {
  const file = ev.target.files[0];
  if (!file) return;
  $("#upload-status").textContent = `上传中: ${file.name} …`;
  try {
    const r = await fetch(`/api/archives?name=${encodeURIComponent(file.name)}`, {
      method: "POST", body: file,
    });
    if (!r.ok) throw new Error((await r.json()).error || r.statusText);
    $("#upload-status").textContent = `已上传: ${file.name}`;
    loadArchives();
  } catch (e) {
    $("#upload-status").textContent = `上传失败: ${e.message}`;
  } finally {
    ev.target.value = "";
  }
});

$("#fixture-btn").addEventListener("click", async () => {
  $("#upload-status").textContent = "生成示例档案…";
  const a = await api("/api/fixtures", { method: "POST" });
  $("#upload-status").textContent = `已生成 ${a.name}(含重复正文/缺失引用/损坏记录)`;
  loadArchives();
});

// ============================== 记录列表 ==============================
async function showRecords(archiveId, offset = 0) {
  state.currentArchive = archiveId;
  state.recOffset = offset;
  const a = state.archives.find(x => x.id === archiveId);
  $("#records-panel").hidden = false;
  $("#records-archive-name").textContent = a ? `— ${a.name}` : "";
  const params = new URLSearchParams({ limit: "50", offset: String(offset) });
  const url = $("#flt-url").value.trim();
  const type = $("#flt-type").value, status = $("#flt-status").value;
  const from = toIso($("#flt-from").value), to = toIso($("#flt-to").value);
  if (url) params.set("url", url);
  if (type) params.set("type", type);
  if (status) params.set("status", status);
  if (from) params.set("from", from);
  if (to) params.set("to", to);
  const data = await api(`/api/archives/${archiveId}/records?${params}`);
  const tbody = $("#records-table tbody");
  tbody.innerHTML = "";
  for (const r of data.records) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${esc(r.timestamp || "—")}</td>
      <td>${badge(r)}</td>
      <td>${esc(r.url || (r.error ? `(偏移 ${r.file_offset})` : ""))}</td>
      <td title="${esc(r.error || "")}">${esc(r.status)}</td>
      <td title="${esc(r.digest || "")}">${short(r.digest ? r.digest.replace("sha256:", "") : "")}</td>
      <td>${r.file_offset}</td>
      <td><button class="rec-detail">详情</button></td>`;
    tr.querySelector(".rec-detail").onclick = () => showRecordDetail(r.id);
    tbody.appendChild(tr);
  }
  $("#rec-page-info").textContent =
    `共 ${data.total} 条,第 ${Math.floor(offset / 50) + 1} 页`;
  $("#rec-prev").disabled = offset === 0;
  $("#rec-next").disabled = offset + 50 >= data.total;
}

$("#flt-apply").addEventListener("click", () => showRecords(state.currentArchive, 0));
$("#rec-prev").addEventListener("click", () =>
  showRecords(state.currentArchive, Math.max(0, state.recOffset - 50)));
$("#rec-next").addEventListener("click", () =>
  showRecords(state.currentArchive, state.recOffset + 50));

async function showRecordDetail(id) {
  const r = await api(`/api/records/${id}`);
  const box = $("#record-detail");
  box.hidden = false;
  let html = `<h3>记录 #${r.id} ${badge(r)}</h3>`;
  if (r.error) html += `<p class="diff-removed">错误: ${esc(r.error)}</p>`;
  html += `<p>捕获编号: ${esc(r.record_id || "—")}<br>
    偏移: ${r.file_offset}(长度 ${r.member_length})<br>
    指纹: ${esc(r.digest || "—")}${r.declared_digest ? `<br>声明摘要: ${esc(r.declared_digest)}` : ""}
    ${r.refers_to ? `<br>引用记录: ${esc(r.refers_to)}(${esc(r.refers_to_date || "")})` : ""}</p>`;
  html += `<details><summary>WARC 头</summary><pre>${esc(JSON.stringify(r.warc_headers, null, 2))}</pre></details>`;
  if (r.http_headers) {
    html += `<details><summary>HTTP 头</summary><pre>${esc(JSON.stringify(r.http_headers, null, 2))}</pre></details>`;
  }
  if (r.type !== "error") {
    html += `<p><a href="/api/records/${r.id}/preview" target="_blank" rel="noopener">预览</a> |
      <a href="/api/records/${r.id}/content?raw=1">下载原始正文</a></p>`;
  }
  box.innerHTML = html;
}

// ============================== 页面 / 捕获 ==============================
async function fillArchiveSelect() {
  const data = await api("/api/archives");
  state.archives = data.archives;
  const sel = $("#page-archive-select");
  sel.innerHTML = "";
  for (const a of data.archives) {
    const opt = document.createElement("option");
    opt.value = a.id;
    opt.textContent = `${a.name}(${a.record_count} 条)`;
    sel.appendChild(opt);
  }
  if (data.archives.length) searchUrls();
}

async function searchUrls() {
  const aid = $("#page-archive-select").value;
  if (!aid) return;
  const q = $("#url-search").value.trim();
  const data = await api(`/api/archives/${aid}/urls?q=${encodeURIComponent(q)}`);
  const tbody = $("#urls-table tbody");
  tbody.innerHTML = "";
  for (const u of data.urls) {
    const tr = document.createElement("tr");
    tr.className = "clickable";
    tr.innerHTML = `<td>${esc(u.url)}</td><td>${u.captures}</td>
      <td>${u.bodies}</td><td class="muted">${esc(u.first || "")} ~ ${esc(u.last || "")}</td>`;
    tr.onclick = () => showCaptures(u.url);
    tbody.appendChild(tr);
  }
}

$("#url-search-btn").addEventListener("click", searchUrls);
$("#page-archive-select").addEventListener("change", () => {
  $("#captures-panel").hidden = true;
  searchUrls();
});

async function showCaptures(url) {
  state.currentUrl = url;
  state.selected.clear();
  const aid = $("#page-archive-select").value;
  const data = await api(
    `/api/archives/${aid}/records?url=${encodeURIComponent(url)}&limit=500`);
  state.captures = data.records;
  $("#captures-panel").hidden = false;
  $("#captures-url").textContent = `— ${url}`;
  $("#compare-result").innerHTML = "";
  $("#run-result").innerHTML = "";
  $("#preview-panel").hidden = true;
  const tbody = $("#captures-table tbody");
  tbody.innerHTML = "";
  for (const r of data.records) {
    const tr = document.createElement("tr");
    const canSelect = r.type !== "error";
    tr.innerHTML = `
      <td>${canSelect ? `<input type="checkbox" data-id="${r.id}">` : ""}</td>
      <td>${esc(r.timestamp || "—")}</td>
      <td>${badge(r)}</td>
      <td title="${esc(r.error || "")}">${esc(r.status)}</td>
      <td title="${esc(r.digest || "")}">${short(r.digest ? r.digest.replace("sha256:", "") : "")}</td>
      <td>${r.file_offset}</td>
      <td></td>`;
    const ops = tr.lastElementChild;
    if (canSelect) {
      const prev = document.createElement("button");
      prev.textContent = "预览";
      prev.onclick = () => showPreview(r.id);
      ops.appendChild(prev);
      const cb = tr.querySelector("input[type=checkbox]");
      cb.addEventListener("change", () => {
        cb.checked ? state.selected.add(r.id) : state.selected.delete(r.id);
        $("#compare-btn").disabled = state.selected.size !== 2;
      });
    }
    tbody.appendChild(tr);
  }
}

// ============================== 预览 ==============================
async function showPreview(id) {
  const panel = $("#preview-panel");
  panel.hidden = false;
  $("#preview-source").hidden = true;
  $("#preview-frame").hidden = false;
  const meta = await api(`/api/records/${id}`);
  $("#preview-meta").textContent =
    `— ${meta.timestamp || ""} ${meta.type}${meta.refers_to ? " → 引用 " + meta.refers_to : ""}`;
  $("#preview-frame").src = `/api/records/${id}/preview`;
  $("#preview-source-btn").onclick = async () => {
    const c = await api(`/api/records/${id}/content`);
    const src = $("#preview-source");
    src.hidden = false;
    $("#preview-frame").hidden = true;
    src.textContent = c.ok
      ? `字符集: ${c.charset}${c.charset_error ? "\n字符集警告: " + c.charset_error : ""}` +
        `${c.content_encoding_error ? "\n内容编码警告: " + c.content_encoding_error : ""}` +
        `${c.resolved_from ? "\n解析自记录: " + c.resolved_from : ""}\n\n${c.text}`
      : `无法解码: ${c.error}`;
  };
}
$("#preview-close").addEventListener("click", () => { $("#preview-panel").hidden = true; });

// ============================== 抽取 ==============================
function collectRules() {
  const rules = { title: $("#rule-title").checked, tables: $("#rule-tables").checked };
  const cf = $("#custom-fields").value.trim();
  if (cf) {
    rules.fields = cf.split(",").map(s => s.trim()).filter(Boolean).map(pair => {
      const i = pair.indexOf("=");
      return i > 0 ? { name: pair.slice(0, i).trim(), pattern: pair.slice(i + 1).trim() } : null;
    }).filter(Boolean);
  }
  return rules;
}

async function runExtract() {
  const aid = $("#page-archive-select").value;
  const body = {
    archive_id: Number(aid),
    url: state.currentUrl,
    time_from: toIso($("#ex-from").value),
    time_to: toIso($("#ex-to").value),
    rules: collectRules(),
  };
  const data = await api("/api/runs", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  renderRunResult($("#run-result"), data.run, data.results);
}
$("#run-extract").addEventListener("click", () =>
  runExtract().catch(e => { $("#run-result").innerHTML = `<p class="diff-removed">${esc(e.message)}</p>`; }));

function renderRunResult(box, run, results) {
  let html = `<h3>任务 #${run.id} — ${results.length} 条结果</h3>
    <p><a href="/api/runs/${run.id}/download?format=csv">下载 CSV</a> |
       <a href="/api/runs/${run.id}/download?format=json">下载 JSON</a> |
       <button class="rerun-btn" data-run="${run.id}">重跑规则</button></p>
    <table class="grid"><thead><tr>
      <th>时间</th><th>捕获编号</th><th>正文指纹</th><th>偏移</th>
      <th>标题</th><th>表格</th><th>自定义字段</th><th>错误</th>
    </tr></thead><tbody>`;
  for (const r of results) {
    const f = r.fields || {};
    const tables = (f.tables || []).map(t => `${t.rows.length} 行`).join(", ");
    const custom = f.fields ? Object.entries(f.fields)
      .map(([k, v]) => `${k}: ${Array.isArray(v) ? v.join(" / ") : JSON.stringify(v)}`)
      .join("; ") : "";
    html += `<tr>
      <td>${esc(r.timestamp || "—")}</td>
      <td title="${esc(r.capture_id || "")}">${short((r.capture_id || "").replace(/.*:/, ""), 10)}</td>
      <td title="${esc(r.digest || "")}">${short((r.digest || "").replace("sha256:", ""))}</td>
      <td>${r.file_offset}</td>
      <td>${esc(f.title ?? "")}</td>
      <td>${esc(tables)}</td>
      <td>${esc(custom)}</td>
      <td class="${r.error ? "diff-removed" : ""}">${esc(r.error || "")}</td>
    </tr>`;
  }
  html += "</tbody></table>";
  box.innerHTML = html;
  const rerun = box.querySelector(".rerun-btn");
  if (rerun) rerun.onclick = async () => {
    const d = await api(`/api/runs/${run.id}/rerun`, { method: "POST" });
    renderRunResult(box, d.run, d.results);
  };
}

// ============================== 比较 ==============================
$("#compare-btn").addEventListener("click", async () => {
  const ids = [...state.selected];
  const data = await api("/api/compare", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ record_ids: ids.map(Number), rules: collectRules() }),
  });
  renderCompare(data);
});

function renderCompare(data) {
  const box = $("#compare-result");
  const label = k => {
    const r = data[k].record;
    return `${k.toUpperCase()}: ${esc(r.timestamp || "?")}(${esc(r.type)})`;
  };
  let html = `<h3>字段比较</h3>`;
  if (data.a.error || data.b.error) {
    html += `<p class="diff-removed">${esc(data.a.error || "")} ${esc(data.b.error || "")}</p>`;
  }
  html += `<table class="grid"><thead><tr><th>字段</th><th>${label("a")}</th><th>${label("b")}</th></tr></thead><tbody>`;
  const d = data.diff || {};
  if (d.title) {
    html += `<tr><td>标题</td>
      <td class="${d.title.changed ? "diff-changed" : ""}">${esc(d.title.from ?? "")}</td>
      <td class="${d.title.changed ? "diff-changed" : ""}">${esc(d.title.to ?? "")}</td></tr>`;
  }
  (d.tables || []).forEach(t => {
    const fmtRows = rows => rows.map(r => r.join(" | ")).join("<br>");
    html += `<tr><td>表格 #${t.index}</td>
      <td>${t.removed.length ? `<span class="diff-removed">- ${fmtRows(t.removed)}</span>` : '<span class="muted">无删除</span>'}</td>
      <td>${t.added.length ? `<span class="diff-added">+ ${fmtRows(t.added)}</span>` : '<span class="muted">无新增</span>'}</td></tr>`;
  });
  Object.entries(d.fields || {}).forEach(([k, v]) => {
    html += `<tr><td>${esc(k)}</td>
      <td class="${v.changed ? "diff-changed" : ""}">${esc(JSON.stringify(v.from))}</td>
      <td class="${v.changed ? "diff-changed" : ""}">${esc(JSON.stringify(v.to))}</td></tr>`;
  });
  html += "</tbody></table>";
  box.innerHTML = html;
}

// ============================== 抽取任务 ==============================
async function loadRuns() {
  const data = await api("/api/runs");
  const tbody = $("#runs-table tbody");
  tbody.innerHTML = "";
  for (const r of data.runs) {
    const a = state.archives.find(x => x.id === r.archive_id);
    const rules = JSON.parse(r.rules_json);
    const ruleDesc = [rules.title && "标题", rules.tables && "表格",
      (rules.fields || []).length && `${rules.fields.length} 自定义`].filter(Boolean).join("+");
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${r.id}</td><td>${esc(a ? a.name : r.archive_id)}</td>
      <td>${esc(r.url)}</td>
      <td class="muted">${esc(r.time_from || "…")} ~ ${esc(r.time_to || "…")}</td>
      <td>${esc(ruleDesc)}</td><td>${r.result_count}</td><td>${r.executions}</td>
      <td class="muted">${esc(r.executed_at || "")}</td><td></td>`;
    const ops = tr.lastElementChild;
    const open = document.createElement("button");
    open.textContent = "打开";
    open.onclick = async () => {
      const d = await api(`/api/runs/${r.id}`);
      renderRunResult($("#run-detail"), d.run, d.results);
    };
    const csvA = document.createElement("a");
    csvA.href = `/api/runs/${r.id}/download?format=csv`;
    csvA.textContent = "CSV";
    const jsonA = document.createElement("a");
    jsonA.href = `/api/runs/${r.id}/download?format=json`;
    jsonA.textContent = "JSON";
    ops.append(open, " ", csvA, " ", jsonA);
    tbody.appendChild(tr);
  }
}

// ============================== 启动 ==============================
loadArchives();
