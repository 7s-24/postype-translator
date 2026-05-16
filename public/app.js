const $ = id => document.getElementById(id);
const CATEGORIES = ["人名","地名","技能","称号","物品","组织","称呼","艺名","本名","其他"];
const STORAGE_KEY = "postype_global_glossary";
const PRESET_STORAGE_KEY = "postype_glossary_preset";
const GLOSSARY_MODE_KEY = "postype_glossary_mode";
const GLOSSARY_SUBMITTER_NICKNAME_KEY = "postype_glossary_submitter_nickname";
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

let progressTraceStartedAt = 0;
let progressTraceLines = [];
const PROGRESS_TRACE_LIMIT = 12;

function resetProgressTrace() {
  progressTraceStartedAt = performance.now();
  progressTraceLines = [];
  const el = $("progress-detail");
  if (el) el.textContent = "";
}

function appendProgressTrace(message) {
  const el = $("progress-detail");
  if (!el) return;

  if (!progressTraceStartedAt) progressTraceStartedAt = performance.now();
  const elapsed = ((performance.now() - progressTraceStartedAt) / 1000).toFixed(1).padStart(5, " ");
  progressTraceLines.push(`[+${elapsed}s] ${message}`);
  if (progressTraceLines.length > PROGRESS_TRACE_LIMIT) {
    progressTraceLines = progressTraceLines.slice(-PROGRESS_TRACE_LIMIT);
  }
  el.textContent = progressTraceLines.join("\n");
}
function scheduleToastAutoHide() {
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    clearNotice();
  }, 5200);
}
const API_ERROR_ACTION = "如果方便的话，可以复制错误码，并描述错误产生的情况，提交给 fedrick1plela755@gmail.com 来帮助改进";

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
  const isUserGlossary = getGlossaryMode() === "user";
  const title = $("glossary-title");
  const submitButton = $("btn-submit-glossary");

  if (title) {
    title.textContent = isUserGlossary ? "我的术语库" : "预设术语库";
  }

  if (submitButton) {
    submitButton.hidden = !isUserGlossary;
  }
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
  $("btn-translate").textContent = isTermsReviewOpen() ? "重新提取术语" : "提取术语后翻译";
}

function setBusy(busy) {
  $("btn-translate").disabled = busy;
  $("btn-direct-translate").disabled = busy;
  if ($("btn-submit-glossary")) $("btn-submit-glossary").disabled = busy;
  if ($("btn-submit-terms")) $("btn-submit-terms").disabled = busy;
  $("file-html").disabled = busy;
  $("manual-html").disabled = busy;
  $("url").disabled = busy;
  $("fast-mode").disabled = busy;

  if (busy) {
    $("btn-translate").disabled = false;
    $("btn-direct-translate").disabled = false;
    $("btn-translate").textContent = "重新提取术语";
    $("btn-direct-translate").textContent = "重新直接翻译";
  } else {
    $("btn-translate").disabled = false;
    $("btn-direct-translate").disabled = false;
    updateExtractButtonLabel();
    $("btn-direct-translate").textContent = "直接翻译";
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

async function postJSON(body, options = {}) {
  const res = await fetch("/api/translate", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal: options.signal,
  });
  const data = await res.json().catch(() => ({}));
  const fallbackCode = res.ok ? "API_ERROR" : `HTTP_${res.status}`;
  const fallbackMessage = res.ok ? "Request failed" : `Request failed (HTTP ${res.status})`;

  if (!res.ok || data.ok === false || data.error) {
    throw makeApiError(data.error, fallbackCode, fallbackMessage);
  }

  return data;
}

async function postJSONStream(body, onDelta, callbacks = {}) {
  const { onRestart, onMeta, onDebug, signal } = callbacks;
  const debug = (message, extra = {}) => {
    if (typeof onDebug === "function") onDebug({ message, ...extra });
  };

  debug("请求已发出，等待服务器响应头", { phase: "request_start" });
  const requestStartedAt = performance.now();
  const headerTimer = setInterval(() => {
    const idleSeconds = Math.round((performance.now() - requestStartedAt) / 1000);
    debug(`仍在等待服务器响应头（${idleSeconds}s）`, { phase: "waiting_headers", idleSeconds });
  }, 5000);

  let res;
  try {
    res = await fetch("/api/translate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ...body, stream: true }),
      signal,
    });
  } finally {
    clearInterval(headerTimer);
  }

  debug(`收到响应头 HTTP ${res.status}`, { phase: "response_headers", status: res.status });

  if (!res.ok || !res.body) {
    const data = await res.json().catch(() => ({}));
    throw makeApiError(data.error, `HTTP_${res.status}`, `Request failed (HTTP ${res.status})`);
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buffer = "";
  let currentEvent = "message";
  let currentData = [];
  let donePayload = null;
  let receivedChars = 0;
  let sawFirstDelta = false;
  let lastDeltaDebugAt = 0;
  let lastActivityAt = performance.now();
  let readPhase = "等待首个流式事件";
  let streamClosed = false;
  let stoppedAfterDone = false;

  const markActivity = (phase) => {
    lastActivityAt = performance.now();
    readPhase = phase;
  };

  const stallTimer = setInterval(() => {
    const idleSeconds = Math.round((performance.now() - lastActivityAt) / 1000);
    if (idleSeconds >= 5) {
      debug(`${readPhase}，已 ${idleSeconds}s 未收到新数据`, { phase: "stall", idleSeconds });
    }
  }, 5000);

  const dispatchEvent = () => {
    if (!currentData.length) {
      currentEvent = "message";
      return;
    }

    const raw = currentData.join("\n");
    const payload = raw ? JSON.parse(raw) : {};

    if (currentEvent === "delta") {
      const delta = payload.delta || "";
      receivedChars += delta.length;
      onDelta(delta);
      const now = performance.now();
      readPhase = "等待后续译文或 done";
      if (!sawFirstDelta || now - lastDeltaDebugAt >= 2000) {
        debug(`${sawFirstDelta ? "继续接收译文" : "收到首段译文"}（累计 ${receivedChars} 字）`, {
          phase: "delta",
          receivedChars,
        });
        sawFirstDelta = true;
        lastDeltaDebugAt = now;
      }
    } else if (currentEvent === "done") {
      donePayload = payload;
      readPhase = "已收到 done，准备结束本段请求";
      debug(`收到 done 事件，后端已完成本段（fallback=${payload.fallback ? "yes" : "no"}）`, {
        phase: "done",
        fallback: Boolean(payload.fallback),
        fallbackType: payload.fallbackType || "",
      });
    } else if (currentEvent === "error") {
      throw makeApiError(payload.error, "STREAM_ERROR", "Streaming request failed");
    } else if (currentEvent === "restart") {
      readPhase = "等待重试后的译文";
      debug(`后端要求重试/切换模型：${payload.reason || "restart"}`, {
        phase: "restart",
        payload,
      });
      // 后端在流式翻译中断、切换模型重试前发送，要求前端丢弃当前 chunk 的累积。
      if (typeof onRestart === "function") onRestart(payload);
    } else if (currentEvent === "meta") {
      readPhase = "等待首段译文";
      debug("收到流式 meta，开始等待译文", { phase: "meta", payload });
      if (typeof onMeta === "function") onMeta(payload);
    }

    currentEvent = "message";
    currentData = [];
  };

  try {
    while (true) {
      const { value, done } = await reader.read();
      streamClosed = done;
      markActivity(done ? "响应流关闭" : "正在解析服务端事件");
      buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
      const lines = buffer.split(/\r?\n/);
      buffer = lines.pop() || "";

      for (const line of lines) {
        if (!line) {
          dispatchEvent();
        } else if (line.startsWith("event:")) {
          currentEvent = line.slice(6).trim() || "message";
        } else if (line.startsWith("data:")) {
          currentData.push(line.slice(5).replace(/^ /, ""));
        }
      }

      if (donePayload && !done) {
        stoppedAfterDone = true;
        break;
      }
      if (done) break;
    }
  } finally {
    clearInterval(stallTimer);
    if (stoppedAfterDone && !streamClosed) {
      void reader.cancel().catch(() => {});
    }
  }

  if (stoppedAfterDone) {
    debug("收到 done 后已主动结束本段请求，不再等待响应流关闭", { phase: "stopped_after_done" });
  } else {
    debug("响应流已关闭，准备检查 done 结果", { phase: "stream_closed" });

    if (buffer) {
      if (buffer.startsWith("data:")) currentData.push(buffer.slice(5).replace(/^ /, ""));
      else if (buffer.startsWith("event:")) currentEvent = buffer.slice(6).trim() || "message";
    }
    dispatchEvent();
  }

  if (!donePayload || donePayload.ok === false || donePayload.error) {
    debug("未拿到有效 done，准备抛出 STREAM_ERROR", { phase: "missing_done" });
    throw makeApiError(donePayload?.error, "STREAM_ERROR", "Streaming request failed");
  }

  debug("本段流式请求结束，返回翻译结果", { phase: "return_done" });
  return donePayload;
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
        !confirm("当前正在使用你的术语库。切换预设会覆盖当前术语库。建议先导出备份。是否继续？")
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
    showNotice(`已导入 ${imported.length} 条术语，并切换到个人术语库。重复术语已按导入列表最后一项为准。`);
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
let currentAbortController = null;
let currentRunId = 0;
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

function isCurrentRun(runId) {
  return runId === currentRunId;
}

function isAbortError(err) {
  return err?.name === "AbortError";
}

async function loadModelOrderForCurrentSession(fast, options = {}) {
  const { runId = currentRunId, signal } = options;
  if (!currentModelSessionId) currentModelSessionId = createModelSessionId();
  const status = await postJSON({
    action: "model_status",
    fast,
    modelSessionId: currentModelSessionId,
  }, { signal });
  if (!isCurrentRun(runId)) return [];
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

function getOrCreateGlossarySubmitterId() {
  const key = "postype_glossary_submitter_id";
  let id = localStorage.getItem(key);

  if (!id) {
    id = window.crypto && typeof window.crypto.randomUUID === "function"
      ? window.crypto.randomUUID()
      : `user-${Date.now()}-${Math.random().toString(36).slice(2)}`;
    localStorage.setItem(key, id);
  }

  return id;
}

function getSavedGlossarySubmitterNickname() {
  return localStorage.getItem(GLOSSARY_SUBMITTER_NICKNAME_KEY) || "";
}

function saveGlossarySubmitterNickname(nickname) {
  const clean = String(nickname || "").trim();

  if (clean) {
    localStorage.setItem(GLOSSARY_SUBMITTER_NICKNAME_KEY, clean);
  } else {
    localStorage.removeItem(GLOSSARY_SUBMITTER_NICKNAME_KEY);
  }

  return clean;
}

function closeGlossarySubmitModal() {
  const modal = $("glossary-submit-modal");
  if (modal) {
    modal.classList.remove("active");
    modal.setAttribute("aria-hidden", "true");
  }
}

function openGlossarySubmitModal(entriesCount, label) {
  const modal = $("glossary-submit-modal");
  const form = $("glossary-submit-form");
  const count = $("glossary-submit-count");
  const nickname = $("glossary-submit-nickname");
  const notes = $("glossary-submit-notes");
  const cancel = $("glossary-submit-cancel");

  if (!modal || !form || !nickname || !notes || !cancel) {
    const fallbackNotes = prompt(`准备提交 ${entriesCount} 条${label}，给你的术语库起个名字，并且告诉我们应该如何称呼你吧：`, "");
    if (fallbackNotes === null) return Promise.resolve(null);
    return Promise.resolve({ nickname: "", notes: String(fallbackNotes || "").trim() });
  }

  if (count) count.textContent = `${entriesCount} 条${label}`;
  nickname.value = getSavedGlossarySubmitterNickname();
  notes.value = "";
  modal.classList.add("active");
  modal.setAttribute("aria-hidden", "false");

  return new Promise(resolve => {
    const finish = result => {
      form.removeEventListener("submit", onSubmit);
      cancel.removeEventListener("click", onCancel);
      modal.removeEventListener("click", onBackdropClick);
      document.removeEventListener("keydown", onKeydown);
      closeGlossarySubmitModal();
      resolve(result);
    };

    const onSubmit = e => {
      e.preventDefault();
      const submitterNickname = saveGlossarySubmitterNickname(nickname.value);
      finish({
        nickname: submitterNickname,
        notes: String(notes.value || "").trim(),
      });
    };

    const onCancel = () => finish(null);

    const onBackdropClick = e => {
      if (e.target === modal) finish(null);
    };

    const onKeydown = e => {
      if (e.key === "Escape") finish(null);
    };

    form.addEventListener("submit", onSubmit);
    cancel.addEventListener("click", onCancel);
    modal.addEventListener("click", onBackdropClick);
    document.addEventListener("keydown", onKeydown);

    requestAnimationFrame(() => {
      (nickname.value ? notes : nickname).focus();
    });
  });
}

function safeHttpUrl(value) {
  const raw = String(value || "").trim();
  if (!raw) return "";

  try {
    const url = new URL(raw);
    return url.protocol === "http:" || url.protocol === "https:" ? url.href : "";
  } catch {
    return "";
  }
}

function getGlossarySubmitEntries(scope) {
  if (scope === "article") {
    const articleTerms = mergedGlossary.filter(t => t._src === "article");
    const preferred = articleTerms.length ? articleTerms : mergedGlossary;
    return cleanGlossaryForExport(preferred);
  }

  return cleanGlossaryForExport(getGlossary());
}

async function submitGlossaryUpload(scope = "global") {
  if (scope === "global" && getGlossaryMode() !== "user") {
    showError("只有我的术语库可以分享。请先添加、编辑或导入术语哦！");
    updateGlossaryModeLabel();
    return;
  }

  const entries = getGlossarySubmitEntries(scope);
  const label = scope === "article" ? "篇章术语" : "个人术语";
  const btn = scope === "article" ? $("btn-submit-terms") : $("btn-submit-glossary");

  if (!entries.length) {
    showError(`没有可提交的${label}。请先填写韩文和中文。`);
    return;
  }

  const submitMeta = await openGlossarySubmitModal(entries.length, label);
  if (!submitMeta) return;

  clearError();
  if (btn) btn.disabled = true;

  try {
    const sourceUrl = safeHttpUrl($("url")?.value) || safeHttpUrl(window.location.href);
    const data = await postJSON({
      action: "save_glossary_upload",
      payload: {
        userId: getOrCreateGlossarySubmitterId(),
        sourceUrl,
        sourceTitle: document.title || "韩文同人翻译器",
        locale: "ko-zh-CN",
        submitterNickname: submitMeta.nickname,
        notes: submitMeta.notes || `${label}提交`,
        entries,
      },
    });

    const saved = data?.data?.entryCount || entries.length;
    showNotice(`已提交 ${saved} 条${label}，即将进入审核，谢谢你的帮助！`);
  } catch (err) {
    const apiError = getApiError(err);
    if (apiError?.code === "DATABASE_NOT_CONFIGURED") {
      showError("术语库提交暂时不可用。 ");
    } else {
      showError(err);
    }
  } finally {
    if (btn) btn.disabled = false;
  }
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

  if (currentAbortController) {
    currentAbortController.abort();
  }

  currentAbortController = new AbortController();
  const runId = currentRunId + 1;
  currentRunId = runId;
  const signal = currentAbortController.signal;

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
  resetProgressTrace();
  appendProgressTrace("开始准备正文");
  setBusy(true);
  await beginProcessingWakeLock();
  if (!isCurrentRun(runId)) return;

  try {
    appendProgressTrace("加载本次模型顺序");
    await loadModelOrderForCurrentSession(isFast(), { runId, signal });
    if (!isCurrentRun(runId)) return;

    let prep;

    if (file) {
      setProgress("解析文件", 0, 1);
      appendProgressTrace("读取上传文件");
      const fileData = await readFile(file);
      if (!isCurrentRun(runId)) return;
      appendProgressTrace("提交后端解析文件");
      prep = await postJSON({ action: "prepare", fileData }, { signal });
    } else if (manual) {
      setProgress("处理文本", 0, 1);
      appendProgressTrace("提交后端处理手动文本");
      prep = await postJSON({ action: "prepare", text: manual }, { signal });
    } else {
      setProgress("获取网页", 0, 1);
      appendProgressTrace("提交后端抓取网页");
      prep = await postJSON({ action: "prepare", url }, { signal });
    }
    if (!isCurrentRun(runId)) return;

    const chunks = prep.chunks || [];
    if (!chunks.length) throw new Error("正文为空");
    pendingChunks = chunks;
    appendProgressTrace(`正文准备完成：共 ${chunks.length} 段`);

    if (skipTermExtraction) {
      await translateWithGlossary(getGlossary().map(g => ({ ...g, _src:"global" })), { runId, signal });
      return;
    }

    setProgress("提取术语", 0, 1);
    appendProgressTrace("开始提取篇章术语");
    const ext = await postJSON({
      action: "extract_terms",
      text: chunks.join("\n\n"),
      modelSessionId: currentModelSessionId,
    }, { signal });
    if (!isCurrentRun(runId)) return;

    setModelOrderDisplay(ext);
    const articleTerms = ext.terms || [];
    appendProgressTrace(`术语提取完成：${articleTerms.length} 条`);

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
    if (!isCurrentRun(runId) || isAbortError(err)) return;
    showError(err);
    $("progress-label").textContent = "失败";
    setBusy(false);
    await endProcessingWakeLock();
  }
}

// Phase 2: Translate
async function translateWithGlossary(glossary, options = {}) {
  const runId = options.runId ?? currentRunId;
  const signal = options.signal ?? currentAbortController?.signal;
  if (!isCurrentRun(runId)) return;

  hideTermsReview();
  clearError();
  clearNotice();
  $("output").value = "";
  $("progress").classList.add("active");
  resetProgressTrace();
  appendProgressTrace("开始翻译流程");
  setBusy(true);
  await beginProcessingWakeLock();
  if (!isCurrentRun(runId)) return;

  const chunks = pendingChunks;
  const total = chunks.length;
  const fast = isFast();
  const clean = glossary
    .filter(g => g.ko && g.zh)
    .map(g => ({ ko:g.ko, zh:g.zh, category:g.category }));
  const startedAt = Date.now();
  const usageStats = buildGlossaryUsageStats(glossary, clean, {
    tier: fast ? "light" : "standard",
    chunkCount: total,
  });

  const logTranslationStep = (message) => {
    appendProgressTrace(message);
    console.info("[translation progress]", message);
  };
  const makeStreamDebug = (chunkNumber) => (event) => {
    const message = `第 ${chunkNumber}/${total} 段：${event.message}`;
    appendProgressTrace(message);
    console.info("[translation stream]", { chunk: chunkNumber, total, ...event });
  };

  void trackGlossaryUsageEvent("glossary_translate_started", usageStats);

  try {
    setProgress("准备翻译", 0, total);
    logTranslationStep(`准备翻译：共 ${total} 段，${fast ? "快速模式" : "标准模式"}`);
    await loadModelOrderForCurrentSession(fast, { runId, signal });
    if (!isCurrentRun(runId)) return;

    logTranslationStep("模型顺序加载完成");
    setProgress("翻译中", 0, total);
    const parts = new Array(total).fill("");
    const fallbackList = [];
    const sensitiveFallbackList = [];
    const googleFallbackList = [];
    const switchedModels = new Map();
    const interruptedChunks = new Set();

    if (fast && total > 1) {
      // ── PARALLEL BATCH MODE ──
      let lastPrev = "";

      for (let start = 0; start < total; start += BATCH_SIZE) {
        if (!isCurrentRun(runId)) return;
        const end = Math.min(start + BATCH_SIZE, total);
        setProgress(`翻译第 ${start + 1}–${end}/${total} 段`, start, total);
        logTranslationStep(`快速模式批次开始：第 ${start + 1}–${end}/${total} 段`);

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
            }, { signal })
          );
        }

        const results = await Promise.all(promises);
        if (!isCurrentRun(runId)) return;

        logTranslationStep(`快速模式批次完成：第 ${start + 1}–${end}/${total} 段`);
        for (let j = 0; j < results.length; j++) {
          const idx = start + j;
          parts[idx] = results[j].translated || "";
          if (results[j].fallback) {
            fallbackList.push(idx + 1);
            if (results[j].fallbackType === "sensitive_model") {
              sensitiveFallbackList.push(idx + 1);
            } else if (results[j].fallbackType === "google") {
              googleFallbackList.push(idx + 1);
            }
          }
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
        if (!isCurrentRun(runId)) return;
        const prev = i > 0 ? parts[i - 1] : "";
        setProgress(`翻译第 ${i + 1}/${total} 段`, i, total);
        logTranslationStep(`开始第 ${i + 1}/${total} 段（原文 ${chunks[i].length} 字，previous ${prev.length} 字）`);

        parts[i] = "";
        const data = await postJSONStream(
          {
            action: "translate",
            chunk: chunks[i],
            index: i + 1,
            total,
            previous: prev,
            glossary: clean,
            fast: false,
            modelSessionId: currentModelSessionId,
          },
          (delta) => {
            if (!isCurrentRun(runId)) return;
            parts[i] += delta;
            $("output").value = parts.filter(Boolean).join("\n\n");
          },
          {
            signal,
            // 后端在切换模型重试前会发 restart：清空当前 chunk 累积。
            onRestart: (payload) => {
              if (!isCurrentRun(runId)) return;
              parts[i] = "";
              $("output").value = parts.filter(Boolean).join("\n\n");
              interruptedChunks.add(i + 1);
              // 诊断信息：把后端给的失败模型、错误类型/详情打到 console，
              // 方便定位是 thinking 超时、provider 拒绝、还是其它原因。
              console.warn("[stream restart]", {
                chunk: i + 1,
                attempt: payload?.attempt,
                maxAttempts: payload?.maxAttempts,
                failedModel: payload?.failedModel,
                errorType: payload?.errorType,
                errorDetail: payload?.errorDetail,
                reason: payload?.reason,
              });
              const attempt = payload?.attempt;
              const maxAttempts = payload?.maxAttempts;
              if (attempt && maxAttempts) {
                setProgress(`翻译第 ${i + 1}/${total} 段（重试 ${attempt}/${maxAttempts}）`, i, total);
              }
            },
            onMeta: (payload) => {
              if (!isCurrentRun(runId)) return;
              if (payload?.modelOrder) setModelOrderDisplay(payload);
            },
            onDebug: makeStreamDebug(i + 1),
          }
        );
        if (!isCurrentRun(runId)) return;

        logTranslationStep(`第 ${i + 1}/${total} 段请求返回，开始写入结果`);
        parts[i] = data.translated || parts[i];
        if (data.fallback) {
          fallbackList.push(i + 1);
          if (data.fallbackType === "sensitive_model") {
            sensitiveFallbackList.push(i + 1);
          } else if (data.fallbackType === "google") {
            googleFallbackList.push(i + 1);
          }
        }
        if (data.modelOrder) setModelOrderDisplay(data);
        if (data.switchedModel && data.model) switchedModels.set(i + 1, data.model);
        $("output").value = parts.filter(Boolean).join("\n\n");
        setProgress("翻译中", i + 1, total);
        logTranslationStep(`第 ${i + 1}/${total} 段完成，准备进入下一步`);
      }
    }

    if (!isCurrentRun(runId)) return;
    setProgress("完成", total, total);
    logTranslationStep("全部段落翻译请求完成");

    const notices = [];
    if (switchedModels.size) {
      console.info("Switched models:", Array.from(switchedModels.entries()));
      notices.push("部分段落已自动切换备用模型完成翻译。");
    }
    if (interruptedChunks.size) {
      notices.push(`第 ${Array.from(interruptedChunks).sort((a,b) => a-b).join(", ")} 段流式中断后已切换模型重试`);
    }
    if (sensitiveFallbackList.length) {
      notices.push(`第 ${sensitiveFallbackList.join(", ")} 段已切换敏感内容兼容模型完成翻译`);
    }
    if (googleFallbackList.length) {
      notices.push(`第 ${googleFallbackList.join(", ")} 段使用了备选翻译（机械翻译 + 术语替换）`);
    }
    const untypedFallbackList = fallbackList.filter(
      index => !sensitiveFallbackList.includes(index) && !googleFallbackList.includes(index)
    );
    if (untypedFallbackList.length) {
      notices.push(`第 ${untypedFallbackList.join(", ")} 段使用了备选翻译`);
    }
    if (notices.length) showNotice(notices.join("\n"));

    // Fix Korean residue and review fallback translation issues
    let text = $("output").value;

    const needsReview = containsKorean(text) || fallbackList.length > 0;

    if (needsReview) {
      setProgress("修正译文问题", total, total);
      logTranslationStep("进入译文修正/复核阶段");
      showNotice(
        containsKorean(text)
          ? "检测到韩文残留，正在自动修正并复核术语/人称…"
          : "检测到备选翻译段落，正在复核术语和人称…"
      );

      const skipFixIndices = googleFallbackList.slice();
      logTranslationStep("提交后端修正/复核请求");
      const fix = await postJSON({
        action: "fix",
        translated_text: text,
        translated_chunks: parts,
        source_chunks: chunks,
        fallback_indices: fallbackList,
        google_fallback_indices: googleFallbackList,
        skip_fix_indices: skipFixIndices,
        glossary: clean,
        fast,
        modelSessionId: currentModelSessionId,
      }, { signal });
      if (!isCurrentRun(runId)) return;

      logTranslationStep("修正/复核请求返回");
      if (fix.modelOrder) setModelOrderDisplay(fix);

      if (fix.fixed_text) {
        text = fix.fixed_text;
        $("output").value = text;
      }

      const skippedFixIndices = Array.isArray(fix.skippedFixIndices) ? fix.skippedFixIndices : [];
      const skippedFixNotice = fix.skipped && skippedFixIndices.length
        ? `第 ${skippedFixIndices.join(", ")} 段机械翻译已跳过模型复核，并保留原文供人工检查。`
        : "";
      const reviewNotice = containsKorean(text)
        ? "修正已尝试，仍检测到韩文字符，请手动检查。"
        : "已完成译文问题复核，请检查术语和人称是否符合原文。";
      showNotice([reviewNotice, skippedFixNotice].filter(Boolean).join("\n"));
    }

    void trackGlossaryUsageEvent("glossary_translate_completed", {
      ...usageStats,
      durationMs: Date.now() - startedAt,
      ok: true,
    });
  } catch (err) {
    if (!isCurrentRun(runId) || isAbortError(err)) return;
    void trackGlossaryUsageEvent("glossary_translate_failed", {
      ...usageStats,
      durationMs: Date.now() - startedAt,
      ok: false,
    });
    showError(err);
    $("progress-label").textContent = "失败";
  } finally {
    if (isCurrentRun(runId)) {
      setBusy(false);
      await endProcessingWakeLock();
      if (currentAbortController?.signal === signal) currentAbortController = null;
    }
  }
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

function switchHelpTab(name) {
  document.querySelectorAll(".help-tab").forEach(tab => {
    const active = tab.dataset.helpTab === name;
    tab.classList.toggle("active", active);
    tab.setAttribute("aria-selected", active ? "true" : "false");
  });
  document.querySelectorAll(".help-tab-panel").forEach(panel => {
    panel.classList.toggle("active", panel.id === `help-${name}-panel`);
  });
}

document.querySelectorAll(".help-tab").forEach(tab => {
  tab.addEventListener("click", () => switchHelpTab(tab.dataset.helpTab));
});


// ── Init ─────────────────────────────────────────────────
updateGlossaryModeLabel();
loadGlossaryPresets();
