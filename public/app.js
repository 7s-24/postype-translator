const $ = id => document.getElementById(id);
const CATEGORIES = ["人名","地名","技能","称号","物品","组织","称呼","艺名","本名","其他"];
const STORAGE_KEY = "postype_global_glossary";
const PRESET_STORAGE_KEY = "postype_glossary_preset";
const GLOSSARY_MODE_KEY = "postype_glossary_mode";
const BATCH_SIZE = 4; // parallel concurrency

const DEFAULT_GLOSSARY = [
  { ko:"신유",   zh:"申惟",   category:"艺名" },
  { ko:"도훈",   zh:"道勋",   category:"艺名" },
  { ko:"영재",   zh:"英宰",   category:"艺名" },
  { ko:"한진",   zh:"韩振",   category:"艺名" },
  { ko:"지훈",   zh:"志薰",   category:"艺名" },
  { ko:"경민",   zh:"炅潣",   category:"艺名" }
];
let defaultGlossary = [...DEFAULT_GLOSSARY];
let glossaryPresets = [];
let currentGlossaryPreset = "";
let toastTimer = null;
let wakeLock = null;
let processingWakeLock = false;

// ── Utility ──────────────────────────────────────────────
function esc(s) { return String(s).replace(/&/g,"&amp;").replace(/"/g,"&quot;").replace(/</g,"&lt;"); }
function containsKorean(t) { return /[\u3131-\u318E\uAC00-\uD7A3]/.test(t); }
function isFast() { return $("fast-mode").checked; }
function modelTierLabel(tier) { return tier === "light" ? "轻量模型" : "标准模型"; }

function setProgress(label, done, total) {
  $("progress-label").textContent = label;
  $("progress-done").textContent = done;
  $("progress-total").textContent = total;
  $("progress-fill").style.width = (total > 0 ? Math.min(100, Math.round(done/total*100)) : 0) + "%";
}
function scheduleToastAutoHide() {
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    clearNotice();
  }, 5200);
}
const API_ERROR_ACTION = "Please copy the error code and describe what happened to fedrick1plela755@gmail.com";

function getApiError(err) {
  if (err && typeof err === "object") {
    if (err.apiError) return err.apiError;
    if (err.code || err.action) return err;
  }
  return null;
}

function isLoadFailedError(msg) {
  const apiError = getApiError(msg);
  const code = String(apiError?.code || "").toLowerCase();
  const message = String(apiError?.message || (msg instanceof Error ? msg.message : msg) || "").toLowerCase();

  return code === "loadfailed" || code === "load_failed" || message.includes("load failed");
}

function showError(msg)  {
  const e=$("error");
  const apiError = getApiError(msg);
  const loadFailedHint = isLoadFailedError(msg)
    ? "\n提示：翻译过程中请不要切换到其他画面、锁屏或让浏览器进入后台，否则请求可能被系统中断；；请保持本页面在前台后重试。"
    : "";

  if (apiError && (apiError.code || apiError.message)) {
    const code = apiError.code || "UNKNOWN_ERROR";
    const message = apiError.message || "Request failed";
    e.textContent = `Error code: ${code}\nMessage: ${message}${loadFailedHint}\n${API_ERROR_ACTION}`;
  } else {
    e.textContent="错误："+(msg instanceof Error ? msg.message : msg)+loadFailedHint;
  }

  e.classList.add("active");
}
function clearError()    { $("error").classList.remove("active"); }
function showNotice(msg) {
  const n=$("notice");
  n.textContent=msg;
  n.classList.add("active");
  scheduleToastAutoHide();
}
function clearNotice()   {
  const n=$("notice");
  n.classList.remove("active");
  clearTimeout(toastTimer);
}

function downloadJSON(data, filename) {
  const blob = new Blob([JSON.stringify(data, null, 2)], { type:"application/json;charset=utf-8" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(a.href);
}

function cleanGlossaryForExport(arr) {
  return arr
    .filter(t => t && typeof t.ko === "string" && typeof t.zh === "string")
    .map(t => ({
      ko: t.ko.trim(),
      zh: t.zh.trim(),
      category: t.category || "其他",
    }))
    .filter(t => t.ko && t.zh);
}

function getGlossaryMode() {
  const mode = localStorage.getItem(GLOSSARY_MODE_KEY);
  return mode === "user" || mode === "preset" ? mode : "preset";
}

function setGlossaryMode(mode) {
  localStorage.setItem(GLOSSARY_MODE_KEY, mode);
  updateGlossaryModeLabel();
}

function updateGlossaryModeLabel() {
  const title = $("glossary-title");
  if (!title) return;
  title.textContent = getGlossaryMode() === "user" ? "用户术语库" : "预设术语库";
}

function dedupeGlossaryPreferLast(arr) {
  const map = new Map();

  for (const t of arr) {
    if (!t || typeof t.ko !== "string" || typeof t.zh !== "string") continue;

    const ko = t.ko.trim();
    const zh = t.zh.trim();
    if (!ko || !zh) continue;

    map.set(ko, {
      ko,
      zh,
      category: t.category || "其他",
    });
  }

  return Array.from(map.values());
}

function mergeGlossaryPreferImported(current, imported) {
  const map = new Map();

  for (const t of current) {
    if (!t || typeof t.ko !== "string" || typeof t.zh !== "string") continue;

    const ko = t.ko.trim();
    const zh = t.zh.trim();
    if (!ko || !zh) continue;

    map.set(ko, {
      ko,
      zh,
      category: t.category || "其他",
    });
  }

  for (const t of imported) {
    if (!t || typeof t.ko !== "string" || typeof t.zh !== "string") continue;

    const ko = t.ko.trim();
    const zh = t.zh.trim();
    if (!ko || !zh) continue;

    map.set(ko, {
      ko,
      zh,
      category: t.category || "其他",
    });
  }

  return Array.from(map.values());
}

function isTermsReviewOpen() {
  return $("terms-review").classList.contains("active");
}

function updateExtractButtonLabel() {
  $("btn-translate").textContent = isTermsReviewOpen() ? "重新提取术语" : "提取术语并翻译";
}

function setBusy(busy, options = {}) {
  const { notifyComplete = true } = options;
  $("btn-translate").disabled = busy;
  $("btn-direct-translate").disabled = busy;
  $("btn-download").disabled = busy;
  $("file-html").disabled = busy;
  $("manual-html").disabled = busy;
  $("url").disabled = busy;
  $("fast-mode").disabled = busy;

  if (busy) {
    $("btn-translate").textContent = "处理中…";
    scheduleWaitingSnake();
  } else {
    updateExtractButtonLabel();
    stopWaitingSnake({ notifyComplete });
  }
}

function readFile(file) {
  return new Promise((resolve, reject) => {
    if (file.name.toLowerCase().endsWith(".webarchive")) {
      const r = new FileReader();
      r.onload = () => {
        const u8 = new Uint8Array(r.result);
        let bin = "";
        for (let i = 0; i < u8.length; i++) bin += String.fromCharCode(u8[i]);
        resolve({ type: "webarchive", content: btoa(bin) });
      };
      r.onerror = () => reject(new Error("读取 WebArchive 失败"));
      r.readAsArrayBuffer(file);
    } else {
      const r = new FileReader();
      r.onload = () => resolve({ type: "html", content: r.result });
      r.onerror = () => reject(new Error("读取文件失败"));
      r.readAsText(file, "utf-8");
    }
  });
}

function makeApiError(error, fallbackCode, fallbackMessage) {
  let apiError;

  if (error && typeof error === "object") {
    apiError = {
      code: error.code || fallbackCode,
      message: error.message || fallbackMessage,
      action: error.action || API_ERROR_ACTION,
    };
  } else if (typeof error === "string" && error.trim()) {
    apiError = {
      code: fallbackCode,
      message: error,
      action: API_ERROR_ACTION,
    };
  } else {
    apiError = {
      code: fallbackCode,
      message: fallbackMessage,
      action: API_ERROR_ACTION,
    };
  }

  const err = new Error(`${apiError.code}: ${apiError.message}`);
  err.apiError = apiError;
  return err;
}

async function postJSON(body) {
  const res = await fetch("/api/translate", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  const fallbackCode = res.ok ? "API_ERROR" : `HTTP_${res.status}`;
  const fallbackMessage = res.ok ? "Request failed" : `Request failed (HTTP ${res.status})`;

  if (!res.ok || data.ok === false || data.error) {
    throw makeApiError(data.error, fallbackCode, fallbackMessage);
  }

  return data;
}

function catOptions(sel) {
  return CATEGORIES.map(c => `<option value="${c}"${c===sel?" selected":""}>${c}</option>`).join("");
}

function hasEnoughKorean(text) {
  const korean = text.match(/[\u3131-\u318E\uAC00-\uD7A3]/g) || [];
  const letters = text.match(/[\u3131-\u318E\uAC00-\uD7A3A-Za-z\u4E00-\u9FFF]/g) || [];
  return korean.length > 0 && korean.length / Math.max(letters.length, 1) >= 0.5;
}

function isWakeLockSupported() {
  return "wakeLock" in navigator && typeof navigator.wakeLock?.request === "function";
}

async function requestWakeLock(options = {}) {
  const { silent = false, auto = false } = options;

  if (!isWakeLockSupported()) {
    if (!auto) {
      showNotice("当前浏览器不支持画面常亮。Chrome、Edge、部分 Android 浏览器支持较好；iOS/Safari 可能不可用。");
    }
    return;
  }

  if (wakeLock) return;

  try {
    wakeLock = await navigator.wakeLock.request("screen");
    wakeLock.addEventListener("release", () => {
      wakeLock = null;
    });
    if (!silent) {
      showNotice("已开启画面常亮。关闭页面、锁屏或系统省电策略仍可能释放该状态。");
    }
  } catch (err) {
    if (!auto) {
      showNotice("画面常亮开启失败。浏览器可能要求 HTTPS、前台页面或用户手势。");
    } else {
      console.warn("自动画面常亮开启失败", err);
    }
  }
}

async function releaseWakeLock() {
  if (!wakeLock) return;
  try {
    await wakeLock.release();
  } catch {}
  wakeLock = null;
}

async function syncWakeLock() {
  if (processingWakeLock && document.visibilityState === "visible") {
    await requestWakeLock({ silent: true, auto: true });
  } else {
    await releaseWakeLock();
  }
}

async function beginProcessingWakeLock() {
  processingWakeLock = true;
  await syncWakeLock();
}

async function endProcessingWakeLock() {
  processingWakeLock = false;
  await releaseWakeLock();
}

// ══════════════════════════════════════════════════════════
//  GLOBAL GLOSSARY
// ══════════════════════════════════════════════════════════
function loadGlossary() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return null;

    const arr = JSON.parse(raw);
    const normalized = normalizeGlossary(arr);

    return normalized.length ? normalized : null;
  } catch {
    return null;
  }
}

function saveGlossary(arr, options = {}) {
  const { switchToUser = false } = options;
  localStorage.setItem(STORAGE_KEY, JSON.stringify(arr));
  $("glossary-count").textContent = arr.length;

  if (switchToUser) {
    setGlossaryMode("user");
  } else {
    updateGlossaryModeLabel();
  }
}

function getGlossary() {
  return loadGlossary() || [...defaultGlossary];
}

function normalizeGlossary(arr) {
  if (!Array.isArray(arr)) return [];

  return arr
    .filter(t =>
      t &&
      typeof t.ko === "string" &&
      typeof t.zh === "string"
    )
    .map(t => ({
      ko: t.ko,
      zh: t.zh,
      category: t.category || "其他",
    }));
}

async function fetchGlossaryFile(path) {
  const res = await fetch(path);

  if (!res.ok) {
    throw new Error("术语库读取失败");
  }

  const arr = await res.json();

  if (!Array.isArray(arr)) {
    throw new Error("术语库格式错误");
  }

  return normalizeGlossary(arr);
}

async function loadGlossaryPresets() {
  const select = $("glossary-preset");

  try {
    const res = await fetch("data/glossaries.json");

    if (!res.ok) {
      throw new Error("术语库列表读取失败");
    }

    const presets = await res.json();

    if (!Array.isArray(presets) || presets.length === 0) {
      throw new Error("没有可用术语库");
    }

    glossaryPresets = presets.filter(p =>
      p &&
      typeof p.id === "string" &&
      typeof p.name === "string" &&
      typeof p.path === "string"
    );

    if (!glossaryPresets.length) {
      throw new Error("术语库列表格式错误");
    }

    select.innerHTML = glossaryPresets.map(p =>
      `<option value="${esc(p.id)}">${esc(p.name)}</option>`
    ).join("");

    currentGlossaryPreset = localStorage.getItem(PRESET_STORAGE_KEY) || glossaryPresets[0].id;

    if (!glossaryPresets.some(p => p.id === currentGlossaryPreset)) {
      currentGlossaryPreset = glossaryPresets[0].id;
    }

    select.value = currentGlossaryPreset;

    const savedGlossary = loadGlossary();
    const glossaryMode = getGlossaryMode();

    await loadGlossaryPreset(currentGlossaryPreset, {
      overwriteSaved: glossaryMode !== "user" || !savedGlossary,
      silent: true,
    });

    select.addEventListener("change", async () => {
      const nextPreset = select.value;

      if (nextPreset === currentGlossaryPreset && getGlossaryMode() === "preset") return;

      if (
        getGlossaryMode() === "user" &&
        !confirm("当前正在使用用户术语库。切换预设会覆盖当前术语库。建议先导出备份。是否继续？")
      ) {
        select.value = currentGlossaryPreset;
        return;
      }

      try {
        await loadGlossaryPreset(nextPreset, {
          overwriteSaved: true,
          silent: false,
        });
        setGlossaryMode("preset");
      } catch (err) {
        showError(err);
        select.value = currentGlossaryPreset;
      }
    });

  } catch (err) {
    console.warn(err);

    glossaryPresets = [];
    currentGlossaryPreset = "";
    defaultGlossary = [...DEFAULT_GLOSSARY];

    if (select) {
      select.innerHTML = `<option value="">默认术语库</option>`;
    }

    if (!loadGlossary()) {
      saveGlossary([...defaultGlossary]);
    }

    renderGlossaryTable();
  }
}

async function loadGlossaryPreset(id, options = {}) {
  const { overwriteSaved = false, silent = false } = options;
  const preset = glossaryPresets.find(p => p.id === id);

  if (!preset) {
    throw new Error("术语库不存在");
  }

  const arr = await fetchGlossaryFile(preset.path);

  defaultGlossary = arr;
  currentGlossaryPreset = preset.id;
  localStorage.setItem(PRESET_STORAGE_KEY, preset.id);

  if (overwriteSaved || !loadGlossary()) {
    saveGlossary([...defaultGlossary]);
    setGlossaryMode("preset");
  }

  renderGlossaryTable();

  if (!silent) {
    showNotice(`已加载：${preset.name}`);
  }
}

function renderGlossaryTable() {
  const items = getGlossary();
  const tbody = $("glossary-tbody");
  tbody.innerHTML = items.map((it, i) => `
    <tr data-idx="${i}">
      <td><input value="${esc(it.ko)}" data-field="ko" /></td>
      <td><input value="${esc(it.zh)}" data-field="zh" /></td>
      <td><select data-field="category">${catOptions(it.category)}</select></td>
      <td style="text-align:center"><button class="btn-del" onclick="delGlossaryRow(${i})">✕</button></td>
    </tr>`).join("");
  $("glossary-count").textContent = items.length;
  tbody.querySelectorAll("input,select").forEach(el => {
    el.addEventListener("change", () => {
      const idx = +el.closest("tr").dataset.idx;
      const arr = getGlossary();
      arr[idx][el.dataset.field] = el.value;
      saveGlossary(arr, { switchToUser: true });
    });
  });
  updateGlossaryModeLabel();
}

function addGlossaryRow() {
  const arr = getGlossary();
  arr.push({ ko:"", zh:"", category:"其他" });
  saveGlossary(arr, { switchToUser: true });
  renderGlossaryTable();
  const w = $("glossary-body").querySelector(".g-table-wrap");
  w.scrollTop = w.scrollHeight;
}

function delGlossaryRow(i) {
  const a = getGlossary();
  a.splice(i, 1);
  saveGlossary(a, { switchToUser: true });
  renderGlossaryTable();
}

function exportGlossary() {
  downloadJSON(cleanGlossaryForExport(getGlossary()), "glossary.json");
}

$("import-file").addEventListener("change", async e => {
  const f = e.target.files[0];
  if (!f) return;

  try {
    const arr = JSON.parse(await f.text());
    if (!Array.isArray(arr)) throw new Error("格式错误");

    const imported = dedupeGlossaryPreferLast(arr);
    const merged = mergeGlossaryPreferImported(getGlossary(), imported);

    saveGlossary(merged, { switchToUser: true });
    renderGlossaryTable();
    showNotice(`已导入 ${imported.length} 条术语，并切换到用户术语库。重复术语已按导入列表最后一项为准。`);
  } catch(err) {
    showError("导入失败：" + err.message);
  }

  e.target.value = "";
});

function resetGlossary() {
  if (!confirm("确定恢复当前预设术语库？")) return;

  saveGlossary([...defaultGlossary]);
  setGlossaryMode("preset");
  renderGlossaryTable();

  const preset = glossaryPresets.find(p => p.id === currentGlossaryPreset);
  showNotice(preset ? `已恢复：${preset.name}` : "已恢复默认术语库");
}

$("glossary-toggle").addEventListener("click", () => {
  $("glossary-toggle").classList.toggle("open");
  $("glossary-body").classList.toggle("open");
});

// ══════════════════════════════════════════════════════════
//  ARTICLE TERMS REVIEW
// ══════════════════════════════════════════════════════════
let pendingChunks = [];
let mergedGlossary = [];
let currentModelSessionId = "";
let currentModelOrder = [];

function createModelSessionId() {
  if (window.crypto && typeof window.crypto.randomUUID === "function") {
    return window.crypto.randomUUID();
  }
  return `${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

function resetModelOrderDisplay() {
  currentModelOrder = [];
  const el = $("model-order");
  if (!el) return;
  el.textContent = "";
  el.classList.remove("active");
}

function setModelOrderDisplay(status) {
  const order = Array.isArray(status?.modelOrder) ? status.modelOrder : [];
  if (!order.length) return;

  currentModelOrder = order;
  const tier = status?.tier || (isFast() ? "light" : "standard");
  const el = $("model-order");
  if (!el) return;
  el.textContent = `测试：本次${modelTierLabel(tier)}顺序：${order.join(" → ")}`;
  el.classList.add("active");
}

async function loadModelOrderForCurrentSession(fast) {
  if (!currentModelSessionId) currentModelSessionId = createModelSessionId();
  const status = await postJSON({
    action: "model_status",
    fast,
    modelSessionId: currentModelSessionId,
  });
  setModelOrderDisplay(status);
  return status.modelOrder || [];
}

function renderTermsTable() {
  const tbody = $("terms-tbody");
  tbody.innerHTML = mergedGlossary.map((it, i) => `
    <tr data-idx="${i}">
      <td><input value="${esc(it.ko)}" data-field="ko" /></td>
      <td><input value="${esc(it.zh)}" data-field="zh" /></td>
      <td><select data-field="category">${catOptions(it.category)}</select></td>
      <td><span class="source-tag ${it._src||"article"}">${it._src==="global"?"全局":"提取"}</span></td>
      <td style="text-align:center"><button class="btn-del" onclick="delTermRow(${i})">✕</button></td>
    </tr>`).join("");
  $("terms-count").textContent = mergedGlossary.length + " 条";
  tbody.querySelectorAll("input,select").forEach(el => {
    el.addEventListener("change", () => {
      mergedGlossary[+el.closest("tr").dataset.idx][el.dataset.field] = el.value;
    });
  });
}

function delTermRow(i) {
  mergedGlossary.splice(i, 1);
  renderTermsTable();
}

function addTermRow() {
  mergedGlossary.push({ ko:"", zh:"", category:"其他", _src:"article" });
  renderTermsTable();
  const w = $("terms-review").querySelector(".g-table-wrap");
  w.scrollTop = w.scrollHeight;
}

function exportArticleTerms() {
  const clean = cleanGlossaryForExport(mergedGlossary.filter(t => t._src === "article"));

  if (!clean.length) {
    showError("没有可导出的篇章术语。");
    return;
  }

  clearError();
  downloadJSON(clean, "article-terms.json");
}

function exportMergedTerms() {
  const clean = cleanGlossaryForExport(mergedGlossary);

  if (!clean.length) {
    showError("没有可导出的术语。");
    return;
  }

  clearError();
  downloadJSON(clean, "merged-terms.json");
}

function showTermsReview(global, article) {
  const seen = new Set();
  mergedGlossary = [];

  // 全局术语库优先级最高，不可被篇章提取覆盖
  for (const t of global) {
    if (!t.ko) continue;
    seen.add(t.ko);
    mergedGlossary.push({ ...t, _src:"global" });
  }

  // 篇章提取的术语仅补充全局库中没有的
  for (const t of article) {
    if (!t.ko || seen.has(t.ko)) continue;
    seen.add(t.ko);
    mergedGlossary.push({ ...t, _src:"article" });
  }

  renderTermsTable();
  $("terms-review").classList.add("active");
  updateExtractButtonLabel();
}

function hideTermsReview() {
  $("terms-review").classList.remove("active");
  updateExtractButtonLabel();
}

// ══════════════════════════════════════════════════════════
//  MAIN FLOW
// ══════════════════════════════════════════════════════════

// Phase 1: Prepare + Extract
async function prepareAndExtract(options = {}) {
  const { skipTermExtraction = false } = options;
  const url = $("url").value.trim();
  const manual = $("manual-html").value.trim();
  const fileInput = $("file-html");
  const file = fileInput.files && fileInput.files[0];

  if (!url && !manual && !file) {
    showError("请输入 URL、手动粘贴正文或上传文件");
    return;
  }

  if (manual && !hasEnoughKorean(manual)) {
    showError("手动输入框里看起来不是韩文正文。请粘贴韩文原文。");
    return;
  }

  if (file && file.size > 3 * 1024 * 1024) {
    showError("文件太大，请选择小于 3 MB 的文件");
    return;
  }

  currentModelSessionId = createModelSessionId();
  resetModelOrderDisplay();

  clearError();
  clearNotice();
  hideTermsReview();
  $("output").value = "";
  $("progress").classList.add("active");
  setBusy(true);
  await beginProcessingWakeLock();

  try {
    await loadModelOrderForCurrentSession(isFast());

    let prep;

    if (file) {
      setProgress("解析文件", 0, 1);
      prep = await postJSON({ action: "prepare", fileData: await readFile(file) });
    } else if (manual) {
      setProgress("处理文本", 0, 1);
      prep = await postJSON({ action: "prepare", text: manual });
    } else {
      setProgress("获取网页", 0, 1);
      prep = await postJSON({ action: "prepare", url });
    }

    const chunks = prep.chunks || [];
    if (!chunks.length) throw new Error("正文为空");
    pendingChunks = chunks;

    if (skipTermExtraction) {
      await translateWithGlossary(getGlossary().map(g => ({ ...g, _src:"global" })));
      return;
    }

    setProgress("提取术语", 0, 1);
    const ext = await postJSON({
      action: "extract_terms",
      text: chunks.join("\n\n"),
      modelSessionId: currentModelSessionId,
    });
    setModelOrderDisplay(ext);
    const articleTerms = ext.terms || [];

    $("progress").classList.remove("active");
    setBusy(false);
    await endProcessingWakeLock();

    showTermsReview(getGlossary(), articleTerms);
    showNotice(
      articleTerms.length
        ? `从原文中提取了 ${articleTerms.length} 条术语（共 ${chunks.length} 段），请确认后开始翻译。`
        : `未检测到新术语（共 ${chunks.length} 段），将使用全局术语库。请确认后开始翻译。`
    );
  } catch (err) {
    showError(err);
    $("progress-label").textContent = "失败";
    setBusy(false, { notifyComplete: false });
    await endProcessingWakeLock();
  }
}

// Phase 2: Translate
async function translateWithGlossary(glossary) {
  hideTermsReview();
  clearError();
  clearNotice();
  $("output").value = "";
  $("progress").classList.add("active");
  setBusy(true);
  await beginProcessingWakeLock();

  const chunks = pendingChunks;
  const total = chunks.length;
  const fast = isFast();
  const clean = glossary
    .filter(g => g.ko && g.zh)
    .map(g => ({ ko:g.ko, zh:g.zh, category:g.category }));
  let completed = false;

  try {
    setProgress("准备翻译", 0, total);
    await loadModelOrderForCurrentSession(fast);
  
    setProgress("翻译中", 0, total);
    const parts = new Array(total).fill("");
    const fallbackList = [];
    const switchedModels = new Map();

    if (fast && total > 1) {
      // ── PARALLEL BATCH MODE ──
      let lastPrev = "";

      for (let start = 0; start < total; start += BATCH_SIZE) {
        const end = Math.min(start + BATCH_SIZE, total);
        setProgress(`翻译第 ${start + 1}–${end}/${total} 段`, start, total);

        const promises = [];

        for (let j = start; j < end; j++) {
          promises.push(
            postJSON({
              action: "translate",
              chunk: chunks[j],
              index: j + 1,
              total,
              previous: j === start ? lastPrev : "",
              glossary: clean,
              fast: true,
              modelSessionId: currentModelSessionId,
            })
          );
        }

        const results = await Promise.all(promises);

        for (let j = 0; j < results.length; j++) {
          const idx = start + j;
          parts[idx] = results[j].translated || "";
          if (results[j].fallback) fallbackList.push(idx + 1);
          if (results[j].modelOrder) setModelOrderDisplay(results[j]);
          if (results[j].switchedModel && results[j].model) switchedModels.set(idx + 1, results[j].model);
        }

        lastPrev = parts[end - 1];
        $("output").value = parts.filter(Boolean).join("\n\n");
        setProgress("翻译中", end, total);
      }
    } else {
      // ── SEQUENTIAL MODE ──
      for (let i = 0; i < total; i++) {
        const prev = i > 0 ? parts[i - 1] : "";
        setProgress(`翻译第 ${i + 1}/${total} 段`, i, total);

        const data = await postJSON({
          action: "translate",
          chunk: chunks[i],
          index: i + 1,
          total,
          previous: prev,
          glossary: clean,
          fast: false,
          modelSessionId: currentModelSessionId,
        });

        parts[i] = data.translated || "";
        if (data.fallback) fallbackList.push(i + 1);
        if (data.modelOrder) setModelOrderDisplay(data);
        if (data.switchedModel && data.model) switchedModels.set(i + 1, data.model);
        $("output").value = parts.filter(Boolean).join("\n\n");
        setProgress("翻译中", i + 1, total);
      }
    }

    setProgress("完成", total, total);

    const notices = [];
    if (switchedModels.size) {
      console.info("Switched models:", Array.from(switchedModels.entries()));
      notices.push("部分段落已自动切换备用模型完成翻译。");
    }
    if (fallbackList.length) {
      notices.push(`第 ${fallbackList.join(", ")} 段使用了备选翻译（机械翻译 + 术语替换）`);
    }
    if (notices.length) showNotice(notices.join("\n"));

    // Fix Korean residue and review fallback translation issues
    let text = $("output").value;

    const needsReview = containsKorean(text) || fallbackList.length > 0;

    if (needsReview) {
      setProgress("修正译文问题", total, total);
      showNotice(
        containsKorean(text)
          ? "检测到韩文残留，正在自动修正并复核术语/人称…"
          : "检测到备选翻译段落，正在复核术语和人称…"
      );

      const fix = await postJSON({
        action: "fix",
        translated_text: text,
        translated_chunks: parts,
        source_chunks: chunks,
        fallback_indices: fallbackList,
        glossary: clean,
        fast,
        modelSessionId: currentModelSessionId,
      });

      if (fix.modelOrder) setModelOrderDisplay(fix);

      if (fix.fixed_text) {
        text = fix.fixed_text;
        $("output").value = text;
      }

      showNotice(
        containsKorean(text)
          ? "修正已尝试，仍检测到韩文字符，请手动检查。"
          : "已完成译文问题复核，请检查术语和人称是否符合原文。"
      );
    }
    completed = true;
  } catch (err) {
    showError(err);
    $("progress-label").textContent = "失败";
  } finally {
    setBusy(false, { notifyComplete: completed });
    await endProcessingWakeLock();
  }
}


// ══════════════════════════════════════════════════════════
//  WAITING SNAKE GAME
// ══════════════════════════════════════════════════════════
const SNAKE_LEADERBOARD_KEY = "postype_snake_leaderboard";
const SNAKE_PLAYER_NAME_KEY = "postype_snake_player_name";
const SNAKE_AUTO_OPEN_DELAY = 4500;
const SNAKE_GRID_SIZE = 18;
const SNAKE_CELL_SIZE = 20;
const SNAKE_TICK_MS = 135;
const PERSON_CATEGORIES = new Set(["人名", "艺名", "本名", "称呼"]);

let snakeAutoTimer = null;
let snakeInterval = null;
let snakeActive = false;
let snakeDismissedForBusy = false;
let snakeRetryLeft = 1;
let snakeScore = 0;
let snakeDirection = { x: 1, y: 0 };
let snakeNextDirection = { x: 1, y: 0 };
let snakeBody = [];
let snakeFood = null;
let snakeFoodTerms = [];
let snakeGameOver = false;
let snakeScoreRecorded = false;

function getSnakeCanvasContext() {
  return $("snake-board")?.getContext("2d");
}

function getSnakeFoodTerms() {
  const chars = cleanGlossaryForExport(getGlossary())
    .filter(t => PERSON_CATEGORIES.has(t.category))
    .flatMap(t => Array.from(t.zh || t.ko || ""))
    .map(ch => ch.trim())
    .filter(ch => ch && /[\u3131-\u318E\uAC00-\uD7A3\u4E00-\u9FFFA-Za-z0-9]/.test(ch));
  return Array.from(new Set(chars)).slice(0, 120);
}

function getSnakePlayerName() {
  return (localStorage.getItem(SNAKE_PLAYER_NAME_KEY) || "").trim();
}

function setSnakePlayerName(name) {
  const cleanName = String(name || "").trim().slice(0, 16);
  if (cleanName) localStorage.setItem(SNAKE_PLAYER_NAME_KEY, cleanName);
  return cleanName;
}

function readSnakeLeaderboard() {
  try {
    const parsed = JSON.parse(localStorage.getItem(SNAKE_LEADERBOARD_KEY) || "[]");
    return Array.isArray(parsed) ? parsed.filter(r => Number.isFinite(r.score)).slice(0, 5) : [];
  } catch {
    return [];
  }
}

function writeSnakeLeaderboard(rows) {
  localStorage.setItem(SNAKE_LEADERBOARD_KEY, JSON.stringify(rows.slice(0, 5)));
}

async function submitSnakeScoreToMongoDB(_entry) {
  // MongoDB backend is not implemented yet. Keep this hook empty for the future API call.
  return { ok: false, skipped: true };
}

async function addSnakeLeaderboardScore(score, options = {}) {
  const { name = "匿名", submitGlobal = false } = options;
  const entry = {
    name: name || "匿名",
    score,
    food: snakeFood?.label || "术语",
    at: new Date().toLocaleString("zh-CN", { hour12: false }),
  };
  const rows = readSnakeLeaderboard();
  rows.push(entry);
  rows.sort((a, b) => b.score - a.score || String(b.at).localeCompare(String(a.at)));
  writeSnakeLeaderboard(rows);

  if (submitGlobal) {
    await submitSnakeScoreToMongoDB(entry);
  }

  renderSnakeLeaderboard();
}

function renderSnakeLeaderboard() {
  const list = $("snake-leaderboard");
  if (!list) return;

  const rows = readSnakeLeaderboard();
  if (!rows.length) {
    list.innerHTML = '<li class="empty">还没有记录，先来一局。</li>';
    return;
  }

  list.innerHTML = rows.map(row => (
    `<li>${esc(row.name || "匿名")} · ${esc(row.score)} 分 · ${esc(row.food || "术语")} · ${esc(row.at || "刚刚")}</li>`
  )).join("");
}

function prepareSnakeScoreForm() {
  const nameInput = $("snake-player-name");
  const submitButton = $("snake-submit-score");
  if (nameInput) nameInput.value = getSnakePlayerName();
  if (submitButton) {
    submitButton.disabled = false;
    submitButton.textContent = "记录成绩";
  }
}

async function recordSnakeScore() {
  if (snakeScoreRecorded || !snakeGameOver) return;

  const name = setSnakePlayerName($("snake-player-name")?.value || "匿名") || "匿名";
  const submitGlobal = Boolean($("snake-submit-global")?.checked);
  const submitButton = $("snake-submit-score");
  if (submitButton) {
    submitButton.disabled = true;
    submitButton.textContent = "已记录";
  }

  await addSnakeLeaderboardScore(snakeScore, { name, submitGlobal });
  snakeScoreRecorded = true;
  $("snake-rank-note").textContent = submitGlobal
    ? "本局已记录；总榜同步接口暂未实装，已先保存在本机。"
    : "本局已记录在本机排行榜。";
}

function updateSnakeHud() {
  $("snake-score").textContent = snakeScore;
  $("snake-food-name").textContent = snakeFood?.label || "术语";
  $("snake-retry-left").textContent = snakeRetryLeft;
}

function sameSnakePoint(a, b) {
  return a.x === b.x && a.y === b.y;
}

function randomSnakeCell() {
  return {
    x: Math.floor(Math.random() * SNAKE_GRID_SIZE),
    y: Math.floor(Math.random() * SNAKE_GRID_SIZE),
  };
}

function placeSnakeFood() {
  let pos = randomSnakeCell();
  while (snakeBody.some(part => sameSnakePoint(part, pos))) {
    pos = randomSnakeCell();
  }

  const label = snakeFoodTerms.length
    ? snakeFoodTerms[Math.floor(Math.random() * snakeFoodTerms.length)]
    : "术语";
  snakeFood = { ...pos, label };
  updateSnakeHud();
}

function drawSnakeGame() {
  const ctx = getSnakeCanvasContext();
  if (!ctx) return;

  ctx.clearRect(0, 0, SNAKE_GRID_SIZE * SNAKE_CELL_SIZE, SNAKE_GRID_SIZE * SNAKE_CELL_SIZE);
  ctx.fillStyle = "#fff";
  ctx.fillRect(0, 0, SNAKE_GRID_SIZE * SNAKE_CELL_SIZE, SNAKE_GRID_SIZE * SNAKE_CELL_SIZE);

  ctx.strokeStyle = "#eee";
  ctx.lineWidth = 1;
  for (let i = 1; i < SNAKE_GRID_SIZE; i++) {
    const p = i * SNAKE_CELL_SIZE + 0.5;
    ctx.beginPath();
    ctx.moveTo(p, 0);
    ctx.lineTo(p, SNAKE_GRID_SIZE * SNAKE_CELL_SIZE);
    ctx.moveTo(0, p);
    ctx.lineTo(SNAKE_GRID_SIZE * SNAKE_CELL_SIZE, p);
    ctx.stroke();
  }

  if (snakeFood) {
    ctx.fillStyle = "#fff";
    ctx.strokeStyle = "#111";
    ctx.lineWidth = 2;
    ctx.fillRect(snakeFood.x * SNAKE_CELL_SIZE + 2, snakeFood.y * SNAKE_CELL_SIZE + 2, SNAKE_CELL_SIZE - 4, SNAKE_CELL_SIZE - 4);
    ctx.strokeRect(snakeFood.x * SNAKE_CELL_SIZE + 2.5, snakeFood.y * SNAKE_CELL_SIZE + 2.5, SNAKE_CELL_SIZE - 5, SNAKE_CELL_SIZE - 5);
    ctx.fillStyle = "#111";
    ctx.font = "700 10px sans-serif";
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillText(String(snakeFood.label).slice(0, 2), snakeFood.x * SNAKE_CELL_SIZE + 10, snakeFood.y * SNAKE_CELL_SIZE + 10, 16);
  }

  snakeBody.forEach((part, index) => {
    ctx.fillStyle = index === 0 ? "#111" : "#444";
    ctx.fillRect(part.x * SNAKE_CELL_SIZE + 2, part.y * SNAKE_CELL_SIZE + 2, SNAKE_CELL_SIZE - 4, SNAKE_CELL_SIZE - 4);
  });
}

function resetSnakeGame({ resetRetry = false } = {}) {
  if (resetRetry) snakeRetryLeft = 1;
  snakeScore = 0;
  snakeDirection = { x: 1, y: 0 };
  snakeNextDirection = { x: 1, y: 0 };
  snakeBody = [
    { x: 5, y: 9 },
    { x: 4, y: 9 },
    { x: 3, y: 9 },
  ];
  snakeGameOver = false;
  snakeScoreRecorded = false;
  snakeFoodTerms = getSnakeFoodTerms();
  $("snake-gameover").classList.remove("active");
  $("snake-retry").disabled = false;
  prepareSnakeScoreForm();
  $("snake-rank-note").textContent = "输入名字后记录本局成绩；名字会保存在本机，下次自动填入。";
  placeSnakeFood();
  updateSnakeHud();
  drawSnakeGame();
}

function endSnakeGame() {
  snakeGameOver = true;
  clearInterval(snakeInterval);
  snakeInterval = null;
  prepareSnakeScoreForm();
  renderSnakeLeaderboard();
  $("snake-gameover").classList.add("active");
  $("snake-retry").disabled = snakeRetryLeft <= 0;
  $("snake-rank-note").textContent = snakeRetryLeft > 0
    ? "输入名字后记录本局成绩；还可以重试一次。"
    : "输入名字后记录本局成绩；重试机会已用完。";
  updateSnakeHud();
}

function tickSnakeGame() {
  if (snakeGameOver) return;

  snakeDirection = snakeNextDirection;
  const head = snakeBody[0];
  const next = { x: head.x + snakeDirection.x, y: head.y + snakeDirection.y };
  const hitsWall = next.x < 0 || next.y < 0 || next.x >= SNAKE_GRID_SIZE || next.y >= SNAKE_GRID_SIZE;
  const hitsSelf = snakeBody.some(part => sameSnakePoint(part, next));

  if (hitsWall || hitsSelf) {
    endSnakeGame();
    drawSnakeGame();
    return;
  }

  snakeBody.unshift(next);
  if (snakeFood && sameSnakePoint(next, snakeFood)) {
    snakeScore += 10;
    placeSnakeFood();
  } else {
    snakeBody.pop();
  }

  updateSnakeHud();
  drawSnakeGame();
}

function startSnakeLoop() {
  clearInterval(snakeInterval);
  snakeInterval = setInterval(tickSnakeGame, SNAKE_TICK_MS);
}

function openSnakeGame() {
  const modal = $("snake-modal");
  if (!modal || snakeActive) return;

  snakeActive = true;
  modal.classList.add("active");
  modal.setAttribute("aria-hidden", "false");
  resetSnakeGame({ resetRetry: true });
  startSnakeLoop();
}

function closeSnakeGame({ dismissed = true } = {}) {
  const modal = $("snake-modal");
  if (!modal) return;

  if (dismissed) snakeDismissedForBusy = true;
  snakeActive = false;
  modal.classList.remove("active");
  modal.setAttribute("aria-hidden", "true");
  clearInterval(snakeInterval);
  snakeInterval = null;
}

function scheduleWaitingSnake() {
  clearTimeout(snakeAutoTimer);
  snakeDismissedForBusy = false;
  snakeAutoTimer = setTimeout(() => {
    if (!snakeDismissedForBusy) openSnakeGame();
  }, SNAKE_AUTO_OPEN_DELAY);
}

function stopWaitingSnake(options = {}) {
  const { notifyComplete = true } = options;
  clearTimeout(snakeAutoTimer);
  snakeAutoTimer = null;
  snakeDismissedForBusy = false;
  if (!snakeActive || !notifyComplete) return;
  showNotice("处理已完成。小游戏不会被打断，结束后可继续查看结果。");
}

function setSnakeDirection(next) {
  if (!next || !snakeActive || snakeGameOver) return;
  if (next.x + snakeDirection.x === 0 && next.y + snakeDirection.y === 0) return;
  snakeNextDirection = next;
}

function bindSnakeEvents() {
  $("snake-hide").addEventListener("click", () => closeSnakeGame());
  $("snake-submit-score").addEventListener("click", () => {
    recordSnakeScore();
  });

  $("snake-player-name").addEventListener("change", e => {
    setSnakePlayerName(e.target.value);
  });

  document.querySelectorAll(".snake-dir").forEach(button => {
    button.addEventListener("click", () => {
      const dirMap = {
        up: { x: 0, y: -1 },
        down: { x: 0, y: 1 },
        left: { x: -1, y: 0 },
        right: { x: 1, y: 0 },
      };
      setSnakeDirection(dirMap[button.dataset.dir]);
    });
  });

  $("snake-retry").addEventListener("click", async () => {
    if (snakeRetryLeft <= 0) return;
    await recordSnakeScore();
    snakeRetryLeft -= 1;
    resetSnakeGame();
    startSnakeLoop();
  });

  document.addEventListener("keydown", e => {
    const keyMap = {
      ArrowUp: { x: 0, y: -1 },
      KeyW: { x: 0, y: -1 },
      ArrowDown: { x: 0, y: 1 },
      KeyS: { x: 0, y: 1 },
      ArrowLeft: { x: -1, y: 0 },
      KeyA: { x: -1, y: 0 },
      ArrowRight: { x: 1, y: 0 },
      KeyD: { x: 1, y: 0 },
    };
    const next = keyMap[e.code];
    if (!next || !snakeActive) return;
    e.preventDefault();
    setSnakeDirection(next);
  });
}

// ── Events ───────────────────────────────────────────────
$("btn-translate").addEventListener("click", () => prepareAndExtract());
$("btn-direct-translate").addEventListener("click", () => prepareAndExtract({ skipTermExtraction: true }));

$("btn-confirm-translate").addEventListener("click", () => {
  translateWithGlossary(mergedGlossary);
});


$("btn-download").addEventListener("click", () => {
  const text = $("output").value.trim();

  if (!text) {
    showError("请先翻译，再导出。");
    return;
  }

  clearError();

  const blob = new Blob([text], { type:"text/plain;charset=utf-8" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "translated.txt";
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(a.href);
});

$("url").addEventListener("keydown", e => {
  if (e.key === "Enter") prepareAndExtract();
});

// Tabs
function clearOtherTabs(activeTab) {
  if (activeTab !== "url") {
    $("url").value = "";
  }

  if (activeTab !== "manual") {
    $("manual-html").value = "";
  }

  if (activeTab !== "file") {
    $("file-html").value = "";
  }
}

function switchTab(name) {
  const current = document.querySelector(".tab.active")?.dataset.tab;
  if (current === name) return;

  clearOtherTabs(name);

  document.querySelectorAll(".tab").forEach(t => t.classList.remove("active"));
  document.querySelectorAll(".tab-content").forEach(c => c.classList.remove("active"));
  document.querySelector(`[data-tab="${name}"]`).classList.add("active");
  $(`${name}-tab`).classList.add("active");

  clearError();
  clearNotice();
  hideTermsReview();
}

document.querySelectorAll(".tab").forEach(t => {
  t.addEventListener("click", () => switchTab(t.dataset.tab));
});

$("fast-help").addEventListener("click", () => {
  $("fast-help-modal").classList.add("active");
});

$("fast-help-close").addEventListener("click", () => {
  $("fast-help-modal").classList.remove("active");
});

$("fast-help-modal").addEventListener("click", e => {
  if (e.target === $("fast-help-modal")) {
    $("fast-help-modal").classList.remove("active");
  }
});

document.addEventListener("visibilitychange", () => {
  syncWakeLock();
});

// Help Modal
$("help-open").addEventListener("click", () => {
  $("help-modal").classList.add("active");
});

$("help-close").addEventListener("click", () => {
  $("help-modal").classList.remove("active");
});

$("help-modal").addEventListener("click", e => {
  if (e.target === $("help-modal")) {
    $("help-modal").classList.remove("active");
  }
});

// ── Init ─────────────────────────────────────────────────
bindSnakeEvents();
updateGlossaryModeLabel();
loadGlossaryPresets();
