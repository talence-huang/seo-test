"use strict";
// 本機網頁介面：所有資料都來自 seo-intel web 提供的 API；寫入類請求帶啟動時的令牌。

const TOKEN = document.querySelector('meta[name="token"]').content;
const $ = (s, root = document) => root.querySelector(s);
const $$ = (s, root = document) => [...root.querySelectorAll(s)];
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const STATUS_ZH = { starting: "啟動中", running: "執行中", stepping: "等你按下一步", paused: "已暫停", waiting_approval: "等待核准",
  done: "完成", failed: "失敗" };
const HYP_ZH = { proposed: "提出", testing: "測試中", supported: "成立", refuted: "推翻", inconclusive: "無結論", rejected: "拒收" };
const PHASE_ZH = { discovery: "探勘", sampling: "抽樣", targeted: "定向抓取", verification: "驗證" };
const STEP_ZH = { DEFINE_GOAL: "定義目標", PLAN_INFO_NEEDS: "規劃資訊需求", SELECT_ACTION: "選擇行動", EXECUTE_TOOL: "執行工具",
  PARSE_RESULT: "解析結果", FORM_HYPOTHESES: "形成假設", SEEK_EVIDENCE: "尋找證據", REVISE_BELIEFS: "修正推論",
  DECIDE_NEXT: "決定下一步", REPORT: "撰寫報告", REFLECT: "反思學習", HUMAN_DECISION: "人工決定" };

async function api(path, body) {
  const opts = body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json", "X-Token": TOKEN },
    body: JSON.stringify(body) };
  const r = await fetch(path, opts);
  const data = await r.json().catch(() => ({ error: `HTTP ${r.status}` }));
  if (!r.ok) throw new Error(data.error || `HTTP ${r.status}`);
  return data;
}
const APPROVAL_ZH = { pending: "待決定", approved: "已核准", rejected: "已拒絕", expired: "已過期", used: "已使用" };
const EST_ZH = { fetches: "需要抓取", with_retry_margin: "含重試餘裕", share_of_original_budget: "占原預算",
  probes: "相關探針", testing_hypotheses: "測試中的假設", used: "已用", cap: "上限", extend: "延長",
  evidence_queue: "待處理的證據行動", domain: "網域", tool: "工具", block_rate: "封鎖率", fetched: "已抓取" };
function fmtEstimate(est) {
  return Object.entries(est || {}).map(([k, v]) => {
    let val = Array.isArray(v) ? v.join("、") || "—" : v;
    if (k === "share_of_original_budget" || k === "block_rate") val = `${Math.round(v * 100)}%`;
    return `${esc(EST_ZH[k] || k)}：${esc(val)}`;
  }).join("<br>");
}
const pill = (status) => `<span class="pill ${esc(status)}">${esc(STATUS_ZH[status] || status)}</span>`;
const money = (x) => `US$${Number(x || 0).toFixed(4)}`;
function showError(el, err) { el.textContent = err ? String(err.message || err) : ""; el.hidden = !err; }

// ---------- 分頁 ----------
function switchTab(name) {
  $$(".tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  $$(".tab").forEach((s) => { s.hidden = s.id !== `tab-${name}`; });
  if (name === "episodes") loadEpisodes();
  if (name === "memory") loadMemory();
}
$$(".tabs button").forEach((b) => b.addEventListener("click", () => switchTab(b.dataset.tab)));

// ---------- 狀態列 ----------
let STATUS = {};
async function refreshStatus() {
  try {
    STATUS = await api("/api/status");
    $("#version").textContent = `v${STATUS.version}`;
    const busy = STATUS.busy;
    $("#statusline").innerHTML = busy ? `執行中：${esc(busy.episode_id)} ${pill(busy.status)}` : "閒置";
    if (busy && busy.kind === "research" && RUN.jobId === null) watchJob(busy.id, true);   // 重新整理頁面後接回進行中的研究
    $("#key-hint").innerHTML = STATUS.has_key ? "" : '<span class="error">沒有偵測到 Anthropic 金鑰：請在 .env 填入 ANTHROPIC_API_KEY 後重新啟動 seo-intel web。</span>';
    if (!STATUS.has_key) $("#start-btn").disabled = true;
    updateSerpHint();
    const pending = await api("/api/approvals");
    const badge = $("#pending-badge");
    badge.hidden = pending.length === 0;
    badge.textContent = pending.length;
  } catch (e) { $("#statusline").textContent = `連不上伺服器：${e.message}`; }
}

// ---------- 研究表單 ----------
function updateSerpHint() {
  const src = $("#serp-source").value;
  $("#serp-csv").hidden = src !== "csv";
  $("#serp-hint").innerHTML = src === "serpapi"
    ? (STATUS.has_serp_key ? "會為每個關鍵字查詢一次 Google 台灣（gl=tw）的前 100 名；結果會存檔，下次研究就能看到名次變化。"
      : '<span class="error">沒有偵測到 SerpApi 金鑰：請在 .env 填入 SERPAPI_API_KEY 後重新啟動 seo-intel web。</span>')
    : src === "csv" ? "CSV 需要「關鍵字、排名、網址」三欄（英文欄名 Keyword / Position / URL 也可以）。"
    : "沒有排名資料時，Agent 從 sitemap、頁面內容與歷史版本推斷競品的內容策略。";
}
$("#serp-source").addEventListener("change", updateSerpHint);
const readFile = (file) => new Promise((ok, fail) => { const r = new FileReader(); r.onload = () => ok(r.result); r.onerror = fail; r.readAsText(file, "utf-8"); });
// 以位元組（base64）上傳：Ahrefs 的「Excel 用」匯出是 UTF-16，由伺服器判斷編碼
const readFileB64 = (file) => new Promise((ok, fail) => { const r = new FileReader(); r.onload = () => ok(String(r.result).split(",")[1] || ""); r.onerror = fail; r.readAsDataURL(file); });

// 缺少哪些資料、會影響哪個問題（與伺服器的 data_gaps 同一套說明，開始前就讓使用者知道）
function formGaps() {
  const gaps = [];
  if ($("#serp-source").value === "none") gaps.push("沒有排名資料：Agent 不知道哪些頁面真的有排名，只能從 sitemap 推測主題頁，On-page 策略的結論會弱很多（建議上傳排名 CSV 或使用 SerpApi）");
  if (!$("#backlinks-csv").files.length) gaps.push("沒有反向連結 CSV：無法研究 Off-page 策略");
  if (!$("#own-site").value.trim()) gaps.push("沒有自家網址：優先行動無法和自家網站比較差距");
  return gaps;
}
function updateDataGaps() {
  const gaps = formGaps(), box = $("#data-gaps");
  box.hidden = gaps.length === 0;
  box.innerHTML = `<b>資料缺口</b><ul>${gaps.map((g) => `<li>${esc(g)}</li>`).join("")}</ul>`;
}
["#serp-source", "#backlinks-csv"].forEach((s) => $(s).addEventListener("change", updateDataGaps));
$("#own-site").addEventListener("input", updateDataGaps);
updateDataGaps();
const lines = (v) => String(v || "").split(/[\n,，、]+/).map((x) => x.trim()).filter(Boolean);

$("#research-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = new FormData(e.target);
  const mode = f.get("memory");
  const body = { step: f.get("step") === "on",
    memory: mode === "custom" ? (f.get("memory_path") || "default") : mode, ablate: f.getAll("ablate"),
    max_fetches: f.get("max_fetches") || null, max_llm_usd: f.get("max_llm_usd") || null };
  showError($("#research-error"), null);
  Object.assign(body, { competitors: lines(f.get("competitors")), keywords: lines(f.get("keywords")),
    own_site: f.get("own_site") || null, start: f.get("start") || null, end: f.get("end") || null,
    serp_source: f.get("serp_source") });
  if (!body.competitors.length || !body.keywords.length) { showError($("#research-error"), "請至少填一個競品網址與一個關鍵字"); return; }
  if (body.serp_source === "csv") {
    const file = $("#serp-csv").files[0];
    if (!file) { showError($("#research-error"), "請選擇排名 CSV 檔"); return; }
    body.serp_csv = await readFile(file);
  }
  body.backlinks_csv = await Promise.all([...$("#backlinks-csv").files].map(async (file) => ({ name: file.name, data: await readFileB64(file) })));
  const gaps = formGaps();
  const gapText = gaps.length ? `\n\n資料缺口（研究照樣進行，報告會註明）：\n- ${gaps.join("\n- ")}` : "";
  if (!confirm(`這次研究會呼叫真實 Opus（約 US$0.3）並實際抓取競品網站，確定要開始嗎？${gapText}`)) return;
  try {
    const job = await api("/api/research", body);
    watchJob(job.id, true);
  } catch (err) { showError($("#research-error"), err); }
});

// ---------- 即時觀看研究 ----------
const RUN = { jobId: null, since: 0, timer: null, episode: null, budget: null, last: null };

function watchJob(jobId, reset) {
  clearTimeout(RUN.timer);
  if (jobId !== RUN.jobId) RUN.since = 0;   // 換了一個工作：事件從頭讀（畫面上的步驟依 reset 決定是否保留）
  if (reset) {
    RUN.since = 0; RUN.last = null;
    $("#steps").innerHTML = ""; $("#run-result").innerHTML = "";
  }
  RUN.jobId = jobId;
  $("#run-empty").hidden = true; $("#run-body").hidden = false;
  switchTab("research");
  poll();
}

let polling = false;
async function poll() {
  if (polling) return;   // 同一時間只有一個輪詢，避免同一筆事件被處理兩次
  polling = true;
  clearTimeout(RUN.timer);
  try { await pollOnce(); } finally { polling = false; }
}
async function pollOnce() {
  let data;
  try { data = await api(`/api/jobs/${RUN.jobId}?since=${RUN.since}`); } catch (e) { RUN.timer = setTimeout(poll, 1500); return; }
  for (const ev of data.events) {
    if (ev.i < RUN.since) continue;
    RUN.since = ev.i + 1;
    handleEvent(ev);
  }
  const job = data.job;
  $("#run-status").outerHTML = `<span class="pill ${esc(job.status)}" id="run-status">${esc(STATUS_ZH[job.status] || job.status)}</span>`;
  $("#step-controls").hidden = job.status !== "stepping";
  $("#run-controls").hidden = !(job.status === "running" && job.kind === "research");
  $("#start-btn").disabled = ["starting", "running", "stepping"].includes(job.status);
  const finished = data.events.some((e) => e.type === "finished");
  if (!finished) RUN.timer = setTimeout(poll, job.status === "stepping" ? 700 : 400);   // 每次執行都以「結束」事件收尾
  refreshStatus();
}

function handleEvent(ev) {
  if (ev.type === "start" || ev.type === "resume") {
    RUN.episode = ev.episode_id;
    $("#run-title").textContent = `Episode ${ev.episode_id}${ev.type === "resume" ? "（續跑）" : ""}`;
    if (ev.task) $("#run-question").textContent = `競品：${ev.task.competitors.join("、")}｜關鍵字：${(ev.task.keywords || []).join("、")}`;
    if (ev.budget) RUN.budget = ev.budget;
    if (ev.type === "resume") $("#run-result").innerHTML = "";
    if (ev.gaps && ev.gaps.length) $("#run-result").insertAdjacentHTML("beforeend",
      `<div class="callout"><b>資料缺口</b>（研究照樣進行，報告會註明哪些問題答不了）<ul>${ev.gaps.map((g) => `<li>${esc(g)}</li>`).join("")}</ul></div>`);
  } else if (ev.type === "step") {
    addStep(ev.record);
  } else if (ev.type === "await") {
    $("#btn-next").focus({ preventScroll: true });
  } else if (ev.type === "error") {
    $("#run-result").insertAdjacentHTML("beforeend", `<div class="callout bad"><b>錯誤</b>：${esc(ev.message)}</div>`);
  } else if (ev.type === "finished") {
    renderResult($("#run-result"), ev.summary, true);
  }
}

function addStep(r) {
  RUN.last = r;
  const quiet = r.step === "DECIDE_NEXT" && (r.decision || {}).decision === "continue" && !(r.details || []).length;
  const li = document.createElement("li");
  if (r.step === "HUMAN_DECISION") li.className = "human";
  const color = `var(--${r.phase})`;
  const body = [];
  if (r.reason) body.push(`<div><b>理由</b>：${esc(r.reason)}</div>`);
  if ((r.details || []).length) body.push(`<ul>${r.details.map((d) => `<li>${esc(d)}</li>`).join("")}</ul>`);
  if ((r.candidates || []).length) body.push(`<div><b>候選行動（前 5 名）</b></div><table><tr><th>行動</th><th>分數</th><th>理由</th></tr>`
    + r.candidates.map((c) => `<tr><td>${esc(c.action)}</td><td>${Number(c.score).toFixed(2)}</td><td>${esc(c.reason)}</td></tr>`).join("") + "</table>");
  for (const rec of r.llm || []) body.push(`<div><b>LLM ${esc(rec.role || "")}</b> ${esc(rec.model || "")} · ${money(rec.usd)}`
    + `${rec.error ? ` · <span class="error">${esc(rec.error)}</span>` : ""}</div>`
    + (rec.output ? `<pre class="log">${esc(JSON.stringify(rec.output, null, 2))}</pre>` : ""));
  if ((r.tool_calls || []).length) body.push(`<div><b>HTTP 請求</b></div><ul>${r.tool_calls.map((c) =>
    `<li><code>${esc(c.status || c.error)}</code> ${esc(c.url)}</li>`).join("")}</ul>`);
  li.innerHTML = `<div class="head"><span class="seq">${r.seq}</span><span class="phase" style="background:${color}" title="${esc(PHASE_ZH[r.phase] || r.phase)}"></span>`
    + `<span class="name ${esc(r.step)}">${esc(STEP_ZH[r.step] || r.step)}</span><span class="sum">${esc(r.summary)}`
    + `${(r.lessons_applied || []).length && !["DEFINE_GOAL", "REFLECT"].includes(r.step) ? ` <span class="lessons">套用經驗 ${esc(r.lessons_applied.join("、"))}</span>` : ""}</span></div>`
    + `<div class="body">${body.join("") || '<span class="muted">（沒有更多細節）</span>'}</div>`;
  if (quiet) li.style.opacity = ".55";
  li.querySelector(".head").addEventListener("click", () => li.classList.toggle("open"));
  if (r.step === "HUMAN_DECISION" || (r.step === "SELECT_ACTION" && (r.summary || "").startsWith("暫停"))) li.classList.add("open");
  const list = $("#steps");
  const atBottom = list.scrollHeight - list.scrollTop - list.clientHeight < 40;
  list.appendChild(li);
  if (atBottom) list.scrollTop = list.scrollHeight;
  renderMeters(r.budget || {});
}

function renderMeters(b) {
  const bar = (used, cap) => `<div class="bar"><i style="width:${Math.min(100, cap ? (100 * used) / cap : 0)}%"></i></div>`;
  $("#run-meters").innerHTML = `
    <div class="meter">階段<b>${esc(PHASE_ZH[b.phase] || b.phase || "—")}</b></div>
    <div class="meter">抓取<b>${b.fetches ?? 0} / ${b.fetch_cap ?? "—"}</b>${bar(b.fetches, b.fetch_cap)}</div>
    <div class="meter">LLM 花費<b>${money(b.llm_usd)}</b>${bar(b.llm_usd, b.llm_cap)}</div>
    <div class="meter">API 呼叫<b>${b.api_calls ?? 0} / ${b.api_cap ?? "—"}</b>${bar(b.api_calls, b.api_cap)}</div>`;
}

for (const [id, action] of [["#btn-next", "next"], ["#btn-run", "run"], ["#btn-pause", "pause"], ["#btn-pause-run", "pause"]]) {
  $(id).addEventListener("click", async () => {
    try { await api(`/api/jobs/${RUN.jobId}/control`, { action }); poll(); } catch (e) { alert(e.message); }
  });
}

// ---------- 結果（研究頁與紀錄頁共用） ----------
function renderResult(el, s, live) {
  if (!s || !s.id) { el.innerHTML = ""; return; }
  const parts = [`<div class="result">`];
  if (s.status === "waiting_approval" && s.pending_approval) parts.push(approvalCard(s));
  else if (s.status === "running") parts.push(`<div class="callout warn"><b>已暫停並存檔</b>（第 ${s.seq} 步）。
    <button data-resume="${esc(s.id)}" data-step="0">繼續執行</button> <button data-resume="${esc(s.id)}" data-step="1">逐步繼續</button></div>`);
  else if (s.status === "failed") parts.push(`<div class="callout bad"><b>執行失敗</b>：${esc(s.end_reason)}</div>`);
  if (s.report && s.report.headline) {
    const ok = s.claims.some((c) => c.validated);
    parts.push(`<div class="callout ${ok ? "ok" : "warn"}"><div class="headline">${esc(s.report.headline)}</div>`
      + `<div class="muted">結束原因：${esc(s.end_reason)} · 抓取 ${s.budget.fetches} 次 · LLM ${money(s.budget.llm_usd)} · ${s.seq} 步</div></div>`);
  }
  if (s.has_timeline || s.has_report) {
    parts.push(`<div class="links">${s.has_timeline ? `<a href="/files/runs/${esc(s.id)}/timeline.html" target="_blank">開啟完整時間軸 ↗</a>` : ""}`
      + `${s.has_report ? `<a href="/files/runs/${esc(s.id)}/report.md" target="_blank">報告（Markdown）↗</a>` : ""}</div>`);
  }
  if (s.report && s.report.headline) parts.push(conclusions(s));   // 報告寫完才有標題（由程式產生）
  if (s.hypotheses.length) {
    parts.push(`<h3>假設</h3><table><tr><th>ID</th><th>狀態</th><th>信心</th><th>做法</th><th>內容</th></tr>`
      + s.hypotheses.map((h) => `<tr><td>${esc(h.id)}</td><td>${esc(HYP_ZH[h.status] || h.status)}</td><td>${h.posterior.toFixed(2)}</td>`
        + `<td>${esc(h.practice || "—")}</td><td>${esc(h.statement)}</td></tr>`).join("") + "</table>");
  }
  const caveats = (s.report && s.report.caveats) || [];
  if (caveats.length) parts.push(`<h3>限制與注意事項</h3><ul>${caveats.map((c) => `<li>${esc(c)}</li>`).join("")}</ul>`);
  const done = (s.approvals || []).filter((a) => a.status !== "pending");
  if (done.length) parts.push(`<h3>人工核准紀錄</h3><ul>${done.map((a) => `<li><code>${esc(a.id)}</code> ${esc(a.rule)}：${esc(APPROVAL_ZH[a.status] || a.status)}`
    + `${a.note ? `（${esc(a.note)}）` : ""} — ${esc(a.reason)}</li>`).join("")}</ul>`);
  if (s.reflection && (s.reflection.lines || []).length) {
    parts.push(`<h3>這次學到什麼（REFLECT）</h3><ul>${s.reflection.lines.map((l) => `<li>${esc(l)}</li>`).join("")}</ul>`);
  }
  if (s.status === "done" && s.claims.length) parts.push(feedbackForm(s));
  parts.push("</div>");
  el.innerHTML = parts.join("");
  bindResult(el, s, live);
}

// 研究的三個問題：On-page 策略、Off-page 策略、優先行動；沒有結論時列出原因與資料缺口
function conclusions(s) {
  const out = [];
  const conf = (c) => `${Number(c.confidence).toFixed(2)}${c.validated ? "" : "（未通過驗證）"}`;
  const on = s.claims.filter((c) => c.display.side === "onpage");
  const off = s.claims.filter((c) => c.display.side === "offpage");
  if (s.report.summary) out.push(`<p>${esc(s.report.summary)}</p>`);
  out.push("<h3>1. On-page 策略</h3>" + (on.length
    ? `<div class="table-wrap"><table><tr><th>結論</th><th>競品</th><th>做法</th><th>排名頁</th><th>同網站其他主題頁</th><th>期間內才採用</th><th>信心</th></tr>`
      + on.map((c) => `<tr><td>${esc(c.id)}</td><td>${esc(c.subject_site)}</td><td>${esc(c.display.practice)}</td>`
        + `<td>${esc(c.display.competitor)}</td><td>${esc(c.display.others)}</td><td>${esc(c.display.adopted)}</td><td>${conf(c)}</td></tr>`).join("") + "</table></div>"
    : `<p class="muted">沒有成立的 On-page 策略結論。</p>`));
  out.push("<h3>2. Off-page 策略</h3>" + (off.length
    ? `<div class="table-wrap"><table><tr><th>結論</th><th>競品</th><th>做法</th><th>量測</th><th>期間內新增引用網域</th><th>信心</th></tr>`
      + off.map((c) => `<tr><td>${esc(c.id)}</td><td>${esc(c.subject_site)}</td><td>${esc(c.display.practice)}</td><td>${esc(c.display.competitor)}</td>`
        + `<td>${c.display.new_ref_domains ?? "—"}（平常約 ${c.display.baseline ?? "—"}）</td><td>${conf(c)}</td></tr>`).join("") + "</table></div>"
    : `<p class="muted">${s.task.backlink_source === "none" ? "沒有提供反向連結 CSV，無法研究 Off-page 策略。" : "沒有成立的 Off-page 策略結論。"}</p>`));
  out.push("<h3>3. 優先行動</h3>" + (s.actions.length
    ? `<p class="hint">分數 = 影響（結論信心 × 競品排名頁的普及率）× 自家差距 ÷ 實作難度，由程式計算。</p>`
      + `<div class="table-wrap"><table><tr><th>順序</th><th>行動</th><th>做法</th><th>競品</th><th>自家</th><th>差距</th><th>難度</th><th>分數</th><th>狀態</th><th>依據</th></tr>`
      + s.actions.map((a) => `<tr><td>${esc(a.id)}</td><td>${esc(a.action)}</td><td>${esc(a.label)}</td><td>${esc(a.display.competitor)}</td>`
        + `<td>${esc(a.display.own)}</td><td>${esc(a.display.gap)}</td><td>${esc(a.display.effort)}</td><td>${a.score.toFixed(2)}</td>`
        + `<td>${esc(a.display.status)}</td><td>${esc(a.claim_id)}</td></tr>`).join("") + "</table></div>"
      + (s.task.own_site ? "" : `<p class="hint">沒有提供自家網址：差距以 50% 估計，排序只反映競品的做法。</p>`)
    : `<p class="muted">沒有成立的策略結論，無法排出優先行動。</p>`));
  const recs = s.report.recommendations || [];
  if (recs.length) out.push(`<h3>建議</h3><ul>${recs.map((r) => `<li>${esc(r)}</li>`).join("")}</ul>`);
  const reasons = s.report.inconclusive_reasons || [];
  if (reasons.length) out.push(`<div class="callout warn"><b>為什麼沒有結論</b><ul>${reasons.map((r) => `<li>${esc(r)}</li>`).join("")}</ul></div>`);
  const gaps = s.report.data_gaps || [];
  if (gaps.length) out.push(`<div class="callout"><b>資料缺口</b>（補上這些資料，對應的問題才答得了）<ul>${gaps.map((g) => `<li>${esc(g)}</li>`).join("")}</ul></div>`);
  return out.join("");
}

function approvalCard(s) {
  const a = s.pending_approval;
  return `<div class="callout warn"><div class="headline">需要你核准：${esc(a.reason)}</div>
    <div class="kv"><span>規則</span><code>${esc(a.rule)}</code><span>預估</span><span>${fmtEstimate(a.estimate)}</span>
    <span>期限</span><span>${esc(a.expires_at)}</span></div>
    <p class="hint">核准：允許這一次（預算延長只延長這次申請的量）。拒絕：Agent 用現有證據下結論，並在報告寫明限制，之後同樣的請求不再詢問。</p>
    <input placeholder="備註（可留空）" data-note>
    <div class="links"><button class="primary" data-decide="1">核准並繼續</button><button class="danger" data-decide="0">拒絕並繼續</button></div></div>`;
}

function feedbackForm(s) {
  return `<details class="card"><summary>覺得結論不對？糾正它（會改變之後的研究）</summary>
    <form data-feedback class="form">
      <label>主張 <select name="claim">${s.claims.map((c) => `<option>${esc(c.id)}</option>`).join("")}</select></label>
      <label>判定 <select name="verdict"><option value="wrong">錯誤</option><option value="partial">部分正確</option><option value="correct">正確</option></select></label>
      <label>理由（會被轉成經驗，例如「xxx 的 /tag/ 是自動產生的標籤頁，不是原因，應忽略」）<textarea name="reason" required></textarea></label>
      <label class="inline"><input type="checkbox" name="no_llm"> 不呼叫 LLM：只保存理由原文（不產生程式套用的偏置）</label>
      <button class="primary" type="submit">送出糾正</button>
      <div data-feedback-out></div>
    </form></details>`;
}

function bindResult(el, s, live) {
  $$("[data-decide]", el).forEach((b) => b.addEventListener("click", async () => {
    const note = ($("[data-note]", el) || {}).value || null;
    $$("[data-decide]", el).forEach((x) => { x.disabled = true; });
    try {
      const r = await api(`/api/episodes/${s.id}/decide`, { approve: b.dataset.decide === "1", note });
      if (r.job) watchJob(r.job.id, false); else loadEpisodes();
    } catch (e) { alert(e.message); $$("[data-decide]", el).forEach((x) => { x.disabled = false; }); }
  }));
  $$("[data-resume]", el).forEach((b) => b.addEventListener("click", async () => {
    try {
      const job = await api(`/api/episodes/${b.dataset.resume}/resume`, { step: b.dataset.step === "1" });
      watchJob(job.id, !live);
    } catch (e) { alert(e.message); }
  }));
  const fb = $("[data-feedback]", el);
  if (fb) fb.addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = new FormData(fb);
    const out = $("[data-feedback-out]", fb);
    try {
      const r = await api("/api/feedback", { episode: s.id, claim: f.get("claim"), verdict: f.get("verdict"), reason: f.get("reason"), no_llm: f.get("no_llm") === "on" });
      out.innerHTML = `<div class="callout ok"><ul>${r.lines.map((l) => `<li>${esc(l)}</li>`).join("")}</ul></div>`;
    } catch (err) { out.innerHTML = `<p class="error">${esc(err.message)}</p>`; }
  });
}

// ---------- 紀錄與核准 ----------
async function loadEpisodes() {
  const [eps, pending] = await Promise.all([api("/api/episodes"), api("/api/approvals")]);
  $("#approvals-list").innerHTML = pending.length ? `<table><tr><th>核准 ID</th><th>Episode</th><th>規則</th><th>理由</th><th>期限</th><th></th></tr>`
    + pending.map((a) => `<tr><td><code>${esc(a.id)}</code></td><td>${esc(a.episode)}</td><td><code>${esc(a.rule)}</code></td><td>${esc(a.reason)}</td>`
      + `<td>${esc(a.expires_at)}</td><td><button data-open="${esc(a.episode)}">處理</button></td></tr>`).join("") + "</table>"
    : `<p class="muted">目前沒有待核准的請求。</p>`;
  $("#episodes-table").innerHTML = `<tr><th>Episode</th><th>網站｜關鍵字</th><th>狀態</th><th>結束原因</th><th>結論</th><th>抓取</th><th>LLM</th></tr>`
    + eps.map((e) => `<tr class="clickable" data-open="${esc(e.id)}"><td>${esc(e.id)}</td><td>${esc(e.label)}</td>`
      + `<td>${pill(e.status)}</td><td>${esc(e.end_reason || "—")}</td><td>${esc(e.claims.join("、") || "—")}</td><td>${e.fetches}</td>`
      + `<td>${money(e.llm_usd)}</td></tr>`).join("");
  $$("[data-open]").forEach((x) => x.addEventListener("click", () => openEpisode(x.dataset.open)));
}
$("#refresh-episodes").addEventListener("click", loadEpisodes);

async function openEpisode(id) {
  const s = await api(`/api/episodes/${id}`);
  const el = $("#episode-detail");
  el.hidden = false;
  el.innerHTML = `<div class="row-between"><h2>${esc(id)} ${pill(s.status)}</h2><button data-close>關閉</button></div>`
    + `<p class="muted">${esc(s.question)}</p><div data-result></div>`;
  $("[data-close]", el).addEventListener("click", () => { el.hidden = true; });
  renderResult($("[data-result]", el), s, false);
  el.scrollIntoView({ behavior: "smooth", block: "start" });
}

// ---------- 記憶 ----------
const LESSON_ZH = { active: "生效", candidate: "候選", retired: "淘汰" };
const SOURCE_ZH = { success: "成功經驗", failure: "失敗經驗", human_correction: "人工糾正" };
async function loadMemory() {
  const path = $("#memory-file").value.trim();
  const m = await api(`/api/memory${path ? `?path=${encodeURIComponent(path)}` : ""}`);
  const q = path ? `?path=${encodeURIComponent(path)}` : "";
  if (!m.exists) { $("#memory-body").innerHTML = `<p class="muted">${esc(m.path)} 還不存在：跑一次使用記憶的研究後就會建立。</p>`; return; }
  const lessons = m.lessons.length ? `<table><tr><th>ID</th><th>狀態</th><th>來源</th><th>支持</th><th>套用 / 成功</th><th>衝突</th><th>經驗</th><th>程式套用的偏置</th><th></th></tr>`
    + m.lessons.map((x) => `<tr class="clickable" data-lesson="${esc(x.id)}"><td>${esc(x.id)}</td><td>${esc(LESSON_ZH[x.status])}</td>`
      + `<td>${esc(SOURCE_ZH[x.source])}</td><td>${x.support}</td><td>${x.applied} / ${x.applied_success}</td><td>${x.conflicts}</td><td>${esc(x.lesson)}`
      + `${x.retired_reason ? `<div class="muted">淘汰原因：${esc(x.retired_reason)}</div>` : ""}</td><td>${esc(x.biases_text.join("；") || "僅文字")}</td>`
      + `<td>${x.status !== "retired" ? `<button class="danger" data-retire="${esc(x.id)}">淘汰</button>` : ""}</td></tr>`).join("") + "</table>"
    : `<p class="muted">還沒有經驗。多做幾次研究，Agent 會從成功與失敗中萃取經驗（需要 2 次支持才生效），你的糾正會立即生效。</p>`;
  const outcomes = m.outcomes.length ? `<table><tr><th>Episode</th><th>網站｜關鍵字</th><th>成功</th><th>人工糾偏</th><th>抓取</th><th>LLM</th><th>套用經驗</th><th>未成功的原因</th></tr>`
    + m.outcomes.map((o) => `<tr><td>${esc(o.episode_id)}</td><td>${esc(o.scenario)}</td><td>${o.success ? "✓" : "✗"}</td><td>${o.corrected ? "是" : ""}</td>`
      + `<td>${o.fetches}</td><td>${money(o.llm_usd)}</td><td>${esc(JSON.parse(o.lessons_json || "[]").join("、"))}</td>`
      + `<td>${JSON.parse(o.notes_json || "[]").map((n) => `<div>${esc(n)}</div>`).join("") || "—"}</td></tr>`).join("") + "</table>" : "";
  const stats = m.stats.length ? `<table><tr><th>層級</th><th>鍵</th><th>命中</th><th>未命中</th><th>價值</th></tr>`
    + m.stats.map((r) => `<tr><td>${esc(r.label)}</td><td><code>${esc(r.key)}</code></td><td>${Number(r.alpha).toFixed(1)}</td>`
      + `<td>${Number(r.beta).toFixed(1)}</td><td>${Number(r.mean).toFixed(2)}</td></tr>`).join("") + "</table>" : "";
  $("#memory-body").innerHTML = `<p class="muted">${esc(m.path)}</p><h3>M1 經驗庫</h3><div class="table-wrap">${lessons}</div><div id="lesson-detail"></div>`
    + (m.corrections.length ? `<h3>人工糾偏紀錄</h3><ul>${m.corrections.map((c) => `<li>${esc(c.episode_id)} ${esc(c.claim_id)}：${esc(c.verdict)} — ${esc(c.reason)} → ${esc(c.lesson_id || "—")}</li>`).join("")}</ul>` : "")
    + (outcomes ? `<h3>每次研究的成效</h3><div class="table-wrap">${outcomes}</div>` : "")
    + (stats ? `<h3>M2 範本價值（前 30）</h3><div class="table-wrap">${stats}</div>` : "");
  $$("[data-lesson]").forEach((tr) => tr.addEventListener("click", async (e) => {
    if (e.target.dataset.retire) return;
    const x = await api(`/api/memory/lesson/${tr.dataset.lesson}${q}`);
    $("#lesson-detail").innerHTML = `<div class="card"><h3>${esc(x.id)} 稽核軌跡</h3><p>${esc(x.trigger)}：${esc(x.lesson)}</p><table><tr><th>時間</th><th>Episode</th><th>事件</th><th>說明</th></tr>`
      + x.events.map((ev) => `<tr><td>${esc(ev.ts.slice(0, 19))}</td><td>${esc(ev.episode_id || "—")}</td><td>${esc(ev.event)}</td><td>${esc(ev.detail)}</td></tr>`).join("") + "</table></div>";
  }));
  $$("[data-retire]").forEach((b) => b.addEventListener("click", async () => {
    const reason = prompt(`淘汰 ${b.dataset.retire} 的理由：`);
    if (reason === null) return;
    try { await api("/api/memory/retire", { lesson: b.dataset.retire, reason, path: path || null }); loadMemory(); } catch (e) { alert(e.message); }
  }));
}
$("#memory-form").addEventListener("submit", (e) => { e.preventDefault(); loadMemory(); });

// ---------- 啟動 ----------
updateSerpHint();
refreshStatus();
setInterval(refreshStatus, 5000);
