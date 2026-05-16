const CATEGORIES = ["人名","地名","技能","称号","物品","组织","称呼","艺名","本名","其他"];

function normalizeGlossaryTerm(term, options = {}) {
  const { trim = false, includeSource = false } = options;

  if (!term || typeof term.ko !== "string" || typeof term.zh !== "string") {
    return null;
  }

  const ko = trim ? term.ko.trim() : term.ko;
  const zh = trim ? term.zh.trim() : term.zh;
  if (trim && (!ko || !zh)) return null;

  const normalized = {
    ko,
    zh,
    category: CATEGORIES.includes(term.category) ? term.category : "其他",
  };

  if (includeSource && term._src) {
    normalized._src = term._src;
  }

  return normalized;
}

function normalizeGlossary(arr, options = {}) {
  if (!Array.isArray(arr)) return [];
  return arr
    .map(term => normalizeGlossaryTerm(term, options))
    .filter(Boolean);
}

function glossaryTermsByKorean(arr, options = {}) {
  const map = new Map();

  for (const term of normalizeGlossary(arr, { trim: true, includeSource: options.includeSource })) {
    map.set(term.ko, term);
  }

  return map;
}

function dedupeGlossaryPreferLast(arr) {
  return Array.from(glossaryTermsByKorean(arr).values());
}

function mergeGlossaryPreferImported(current, imported) {
  return Array.from(glossaryTermsByKorean([...current, ...imported]).values());
}

function cleanGlossaryForExport(arr) {
  return normalizeGlossary(arr, { trim: true });
}

function countGlossaryCategories(glossary) {
  const counts = {};

  for (const item of normalizeGlossary(glossary, { includeSource: true })) {
    counts[item.category] = (counts[item.category] || 0) + 1;
  }

  return counts;
}

function getPresetModificationStats(currentGlossary, baselineGlossary) {
  const current = glossaryTermsByKorean(currentGlossary);
  const baseline = glossaryTermsByKorean(baselineGlossary);
  let addedTermCount = 0;
  let deletedTermCount = 0;
  let editedTermCount = 0;

  for (const [ko, currentTerm] of current.entries()) {
    const baselineTerm = baseline.get(ko);
    if (!baselineTerm) {
      addedTermCount += 1;
    } else if (baselineTerm.zh !== currentTerm.zh || baselineTerm.category !== currentTerm.category) {
      editedTermCount += 1;
    }
  }

  for (const ko of baseline.keys()) {
    if (!current.has(ko)) {
      deletedTermCount += 1;
    }
  }

  const modifiedTermCount = addedTermCount + deletedTermCount + editedTermCount;

  return {
    presetModified: modifiedTermCount > 0,
    modifiedTermCount,
    addedTermCount,
    deletedTermCount,
    editedTermCount,
  };
}
