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
