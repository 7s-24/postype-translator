function countGlossaryCategories(glossary) {
  const counts = {};

  for (const item of glossary) {
    const category = CATEGORIES.includes(item?.category) ? item.category : "其他";
    counts[category] = (counts[category] || 0) + 1;
  }

  return counts;
}

function mapGlossaryByKorean(glossary) {
  const map = new Map();

  for (const item of glossary || []) {
    if (!item || typeof item.ko !== "string" || typeof item.zh !== "string") continue;

    const ko = item.ko.trim();
    const zh = item.zh.trim();
    if (!ko || !zh) continue;

    map.set(ko, {
      zh,
      category: CATEGORIES.includes(item.category) ? item.category : "其他",
    });
  }

  return map;
}

function getPresetModificationStats(currentGlossary, baselineGlossary) {
  const current = mapGlossaryByKorean(currentGlossary);
  const baseline = mapGlossaryByKorean(baselineGlossary);
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

function getGlossaryUsageMode(glossary) {
  const hasArticleTerms = glossary.some(item => item?._src === "article");
  const hasGlobalTerms = glossary.some(item => item?._src === "global" || !item?._src);

  if (hasArticleTerms && hasGlobalTerms) return "mixed";
  if (hasArticleTerms) return "article";
  return getGlossaryMode();
}

function buildGlossaryUsageStats(glossary, clean, extra = {}) {
  const mode = getGlossaryUsageMode(glossary);
  const presetId = currentGlossaryPreset || "";
  const presetStats = presetId
    ? getPresetModificationStats(getGlossary(), defaultGlossary)
    : {
        presetModified: false,
        modifiedTermCount: 0,
        addedTermCount: 0,
        deletedTermCount: 0,
        editedTermCount: 0,
      };

  return {
    userId: getOrCreateGlossarySubmitterId(),
    pageUrl: safeHttpUrl(window.location.origin) || "https://postype-translator.local",
    source: "web",
    glossaryMode: mode,
    presetId,
    usedPresetGlossary: Boolean(presetId),
    termCount: clean.length,
    categoryCounts: countGlossaryCategories(clean),
    ...presetStats,
    ...extra,
  };
}

async function trackGlossaryUsageEvent(eventType, stats) {
  try {
    await postJSON({
      action: "track_event",
      payload: {
        eventType,
        ...stats,
      },
    });
  } catch (err) {
    const code = err?.apiError?.code;
    if (code === "DATABASE_NOT_CONFIGURED") {
      // 开发环境无 MongoDB 是正常情况，不打日志
      return;
    }
    console.info("track_event failed", err);
  }
}
