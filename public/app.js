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
function showError(msg)  {
  const e=$("error");
  e.textContent="错误："+msg;
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

function setBusy(busy) {
  $("btn-translate").disabled = busy;
  $("btn-download").disabled = busy;
  $("file-html").disabled = busy;
  $("manual-html").disabled = busy;
  $("url").disabled = busy;
  $("fast-mode").disabled = busy;
  $("btn-translate").textContent = busy ? "处理中…" : "翻译";
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

async function postJSON(body) {
  const res = await fetch("/api/translate", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok || data.error) throw new Error(data.error || `请求失败 (HTTP ${res.status})`);
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
        showError(err.message || String(err));
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
}

function hideTermsReview() {
  $("terms-review").classList.remove("active");
}

// ══════════════════════════════════════════════════════════
//  MAIN FLOW
// ══════════════════════════════════════════════════════════

// Phase 1: Prepare + Extract
async function prepareAndExtract() {
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

  clearError();
  clearNotice();
  hideTermsReview();
  $("output").value = "";
  $("progress").classList.add("active");
  setBusy(true);
  await beginProcessingWakeLock();

  try {
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

    setProgress("提取术语", 0, 1);
    const ext = await postJSON({ action: "extract_terms", text: chunks.join("\n\n") });
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
    showError(err.message || String(err));
    $("progress-label").textContent = "失败";
    setBusy(false);
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

  try {
    setProgress("翻译中", 0, total);
    const parts = new Array(total).fill("");
    const fallbackList = [];

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
            })
          );
        }

        const results = await Promise.all(promises);

        for (let j = 0; j < results.length; j++) {
          const idx = start + j;
          parts[idx] = results[j].translated || "";
          if (results[j].fallback) fallbackList.push(idx + 1);
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
        });

        parts[i] = data.translated || "";
        if (data.fallback) fallbackList.push(i + 1);
        $("output").value = parts.filter(Boolean).join("\n\n");
        setProgress("翻译中", i + 1, total);
      }
    }

    setProgress("完成", total, total);

    if (fallbackList.length) {
      showNotice(`第 ${fallbackList.join(", ")} 段使用了备选翻译（机械翻译 + 术语替换）`);
    }

    // Fix Korean residue
    let text = $("output").value;

    if (containsKorean(text)) {
      setProgress("修正韩文残留", total, total);
      showNotice("检测到韩文残留，正在自动修正…");

      const fix = await postJSON({ action: "fix", translated_text: text, fast });

      if (fix.fixed_text) {
        text = fix.fixed_text;
        $("output").value = text;
      }

      showNotice(
        containsKorean(text)
          ? "修正已尝试，仍检测到韩文字符，请手动检查。"
          : "已自动修正韩文残留，请检查译文。"
      );
    }
  } catch (err) {
    showError(err.message || String(err));
    $("progress-label").textContent = "失败";
  } finally {
    setBusy(false);
    await endProcessingWakeLock();
  }
}

// ── Events ───────────────────────────────────────────────
$("btn-translate").addEventListener("click", prepareAndExtract);

$("btn-confirm-translate").addEventListener("click", () => {
  translateWithGlossary(mergedGlossary);
});

$("btn-skip-translate").addEventListener("click", () => {
  translateWithGlossary(getGlossary().map(g => ({ ...g, _src:"global" })));
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
updateGlossaryModeLabel();
loadGlossaryPresets();
