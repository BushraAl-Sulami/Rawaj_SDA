"""Present saved research facts without changing the qualification or its sources."""

import re


_PATH = r"(?:profile_analysis|metrics|analysis_coverage|data_quality)(?:\.[a-zA-Z_][a-zA-Z_0-9]*)+"
_ASSIGNMENT = re.compile(
    rf"`?(?P<path>{_PATH})`?\s*[:=]\s*`?(?P<value>true|false|null|none|-?\d+(?:\.\d+)?%?)`?(?!\w|\.\d)",
    re.IGNORECASE,
)
_SOURCE_NOTE = re.compile(r"\s*\(\s*Metric\s+Paths?\s*:[^)]*\)", re.IGNORECASE)
_SOURCE_SUFFIX = re.compile(
    rf"\s*Metric\s+Paths?\s*:\s*`?{_PATH}`?\.?\s*$", re.IGNORECASE
)
_REFERENCE = re.compile(
    r"^(?:research signals?|metrics?|metric paths?|evidence sources?|evidence content ids?)\s*:",
    re.IGNORECASE,
)
_EMPTY = {"", "0", "0.0", "false", "true", "none", "null", "n/a", "not available"}
_IDENTIFIER = re.compile(r"^[a-z][a-z0-9]*(?:[_.][a-z0-9]+)+[.]?$", re.IGNORECASE)

# False means not observed in this analysis, not that the feature cannot exist.
_BOOL_FACTS = {
    "profile_analysis.bio.cta_present": (
        "A call to action was observed in the bio.",
        "No call to action was observed in the bio.",
    ),
    "profile_analysis.bio.location_present": (
        "A location was observed in the bio.", "No location was observed in the bio.",
    ),
    "profile_analysis.bio.cuisine_identified": (
        "The cuisine was identified in the bio.", "The cuisine was not identified in the bio.",
    ),
    "profile_analysis.bio.value_proposition_present": (
        "The bio described what makes the restaurant distinctive.",
        "The bio did not describe what makes the restaurant distinctive.",
    ),
    "data_quality.profile_scrape_success": (
        "The Instagram profile was retrieved successfully.",
        "The Instagram profile could not be retrieved successfully.",
    ),
}
for _field, _label in {
    "website": "website", "external_link": "external link",
    "phone": "phone contact option", "email": "email contact option",
}.items():
    _BOOL_FACTS[f"profile_analysis.contact_accessibility.{_field}_available"] = (
        f"The profile analysis recorded an available {_label}.",
        f"No {_label} was recorded as available in the profile analysis.",
    )

_NUMERIC_FACTS = {
    "metrics.activity.content_last_30_days": "Content items observed in the last 30 days: {value}.",
    "metrics.activity.content_per_week": "Observed posting frequency: {value} content items per week.",
    "metrics.activity.days_since_last_content": "Days since the latest observed content: {value}.",
    "metrics.commerce_visibility.menu_visible_pct": "Menu information was visible in {value}% of analyzed content.",
    "metrics.commerce_visibility.price_visible_pct": "Price information was visible in {value}% of analyzed content.",
    "metrics.commerce_visibility.offer_visible_pct": "Offer information was visible in {value}% of analyzed content.",
    "metrics.engagement.engagement_rate_pct": "Observed engagement rate: {value}%.",
    "metrics.promotion.promotional_content_pct": "Promotional content represented {value}% of analyzed content.",
    "metrics.cta.any_cta_pct": "Calls to action were observed in {value}% of analyzed content.",
    "analysis_coverage.content_analyzed": "Content items analyzed: {value}.",
    "analysis_coverage.analysis_success_pct": "Content analysis success rate: {value}%.",
}


def _label(path: str) -> str:
    parts = path.split(".")
    if parts[0] == "metrics":
        parts = parts[1:]
    return " / ".join(part.replace("_", " ").replace("pct", "percentage") for part in parts)


def _fact(match: re.Match) -> str:
    path, value = match["path"].lower(), match["value"].lower()
    # Some older reports flattened the bio field.
    path = path.replace("profile_analysis.bio_cta_present", "profile_analysis.bio.cta_present")
    if value in {"null", "none"}:
        return f"{_label(path).capitalize()}: not available."
    if value in {"true", "false"}:
        if path in _BOOL_FACTS:
            return _BOOL_FACTS[path][value == "false"]
        return f"{_label(path).capitalize()}: {'yes' if value == 'true' else 'no'} (as recorded)."
    if path in _NUMERIC_FACTS:
        return _NUMERIC_FACTS[path].format(value=value.rstrip("%"))
    if len(path.split(".")) == 4 and path.startswith(("metrics.content_mix.", "metrics.format_mix.")) and path.endswith((".count", ".pct")):
        category, unit = path.split(".")[2:]
        label = category.replace("_", " ").capitalize()
        if unit == "pct":
            return f"{label} represented {value.rstrip('%')}% of analyzed content."
        return f"{label}: {value} analyzed content items."
    unit = "%" if path.endswith("_pct") or value.endswith("%") else ""
    return f"{_label(path).capitalize()}: {value.rstrip('%')}{unit}."


def evidence_text(line: str) -> str | None:
    """Keep prose and numeric facts, translate scalar fields, and omit source-only lines."""
    text = _SOURCE_NOTE.sub("", line).strip()
    text = _SOURCE_SUFFIX.sub("", text).strip()
    # Older reports cite a parent path once, followed by several short fields.
    context = re.search(rf"Metric path:\s*`?({_PATH})`?\s*[—–]\s*(.+)", text, re.IGNORECASE)
    if context:
        facts = []
        for part in context[2].split(";"):
            scalar = re.fullmatch(
                r"`?([a-z_][a-z_0-9]*)`?\s*[:=]\s*`?(true|false|null|none|-?\d+(?:\.\d+)?%?)`?(?:\s+items?)?\.?",
                part.strip(), re.IGNORECASE,
            )
            if not scalar:
                break
            facts.append(evidence_text(f"{context[1]}.{scalar[1]} = {scalar[2]}"))
        else:
            return " ".join(facts)
    mix = re.fullmatch(
        r"(?:Related content mix:\s*)?`?metrics\.(?:content_mix|format_mix)\.([a-z_0-9]+)`?"
        r"\s*:\s*(\d+)\s+items?\s*/\s*(\d+(?:\.\d+)?)%\.?", text, re.IGNORECASE,
    )
    if mix:
        return f"{mix[1].replace('_', ' ').capitalize()}: {mix[2]} analyzed content items ({mix[3]}%)."
    matches = list(_ASSIGNMENT.finditer(text))
    if len(matches) == 1:
        match = matches[0]
        prefix, suffix = text[:match.start()].strip(), text[match.end():].strip()
        if (not prefix or prefix.endswith(":")) and suffix in {"", "."}:
            return _fact(match)
    if matches:
        text = _ASSIGNMENT.sub(_fact, text)
    elif _REFERENCE.match(text) and not re.search(r"\d+(?:\.\d+)?\s*%", text):
        return None
    stripped = text.strip("` ")
    if stripped.lower() in _EMPTY or _IDENTIFIER.fullmatch(stripped):
        return None
    # Preserve the old handling of short, standalone metric keys and signal IDs.
    key, colon, value = text.partition(":")
    if colon and not matches:
        if _IDENTIFIER.fullmatch(key.strip()) or _IDENTIFIER.fullmatch(value.strip("` .")):
            return None
    return text or None


def display_evidence(lines: list[str]) -> list[str]:
    return list(dict.fromkeys(text for line in lines if (text := evidence_text(line))))
