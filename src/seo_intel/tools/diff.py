"""兩個頁面版本的結構化差異（變動類型：內容擴充、新段落、結構化資料、FAQ 等）。"""

from __future__ import annotations

from seo_intel.tools.extract import PageFeatures

SIGNIFICANT_WORDS_CHANGE = 0.20


def diff_features(before: PageFeatures, after: PageFeatures) -> dict:
    words_pct = (after.word_count - before.word_count) / max(before.word_count, 1)
    schema_added = sorted(set(after.schema_types) - set(before.schema_types))
    schema_removed = sorted(set(before.schema_types) - set(after.schema_types))
    faq_added = after.faq_count - before.faq_count
    sections_added = [h for h in after.sections if h not in before.sections]
    table_added = after.has_table and not before.has_table

    change_types: list[str] = []
    if words_pct >= SIGNIFICANT_WORDS_CHANGE:
        change_types.append("content_expansion")
    if sections_added:
        change_types.append("new_sections")
    change_types += [f"schema:{s}" for s in schema_added]
    if faq_added > 0:
        change_types.append("faq_content")
    if table_added:
        change_types.append("comparison_table")

    return {
        "words_before": before.word_count,
        "words_after": after.word_count,
        "words_change_pct": round(words_pct, 3),
        "schema_added": schema_added,
        "schema_removed": schema_removed,
        "faq_added": faq_added,
        "sections_added": sections_added,
        "table_added": table_added,
        "change_types": change_types,
        "significant": bool(change_types),
    }
