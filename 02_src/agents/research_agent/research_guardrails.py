"""
Deterministic guardrails around the Apify scrape

They keep every run bounded and the scraped data trustworthy:

- input:  a valid Instagram username and bounded content/lookback limits
- cost:   per-actor timeout, memory, item cap and USD charge cap
- output: failed runs rejected, Apify error rows dropped, content from other
          accounts dropped, oversized text truncated, media URLs restricted to
          Instagram's CDN over HTTPS (they are sent to the LLM)
"""

import logging
import re
from datetime import timedelta
from decimal import Decimal
from typing import Any
from urllib.parse import urlparse

# ---------- Input ----------
USERNAME_RE = re.compile(r"^(?!.*\.\.)(?!\.)(?!.*\.$)[A-Za-z0-9._]{1,30}$")
# Scrape at most the 20 most recent posts from the last 90 days.
MAX_CONTENT_LIMIT = 20
MAX_LOOKBACK_DAYS = 90

# ---------- Apify run limits (per actor call) ----------
ACTOR_RUN_TIMEOUT = timedelta(minutes=5)
ACTOR_MEMORY_MBYTES = 1024
ACTOR_MAX_CHARGE_USD = Decimal("1.00")

# ---------- Output ----------
MAX_CAPTION_CHARS = 4000
MAX_TRANSCRIPT_CHARS = 8000
MAX_BIO_CHARS = 1000
MAX_IMAGES_PER_ITEM = 10
ALLOWED_MEDIA_HOST_SUFFIXES = (".cdninstagram.com", ".fbcdn.net")


logger = logging.getLogger(__name__)


def report(message: str, *args: Any) -> None:
    """Log one guardrail check so a live run shows every check as it happens."""

    logger.info("[guardrail] " + message, *args)


class ScrapeGuardrailError(RuntimeError):
    """The scrape cannot be trusted or must not run."""


# =========================================================
# INPUT
# =========================================================


def normalize_instagram_username(value: str | None) -> str:
    """Accept 'name', '@name' or an instagram.com profile URL; return 'name'."""

    raw = (value or "").strip()
    match = re.match(r"^(?:https?://)?(?:www\.)?instagram\.com/([^/?#]+)", raw, re.I)
    if match:
        raw = match.group(1)
    username = raw.lstrip("@").strip()

    if not USERNAME_RE.fullmatch(username):
        raise ValueError(f"Invalid Instagram username: {value!r}")
    report("username OK: @%s", username)
    return username


def clamp_scrape_limits(content_limit: int, lookback_days: int) -> tuple[int, int]:
    """Reject non-positive limits; reduce larger ones to the 20-post / 90-day cap."""

    if content_limit < 1:
        raise ValueError("content_limit must be greater than 0")
    if lookback_days < 1:
        raise ValueError("lookback_days must be greater than 0")

    limits = min(content_limit, MAX_CONTENT_LIMIT), min(lookback_days, MAX_LOOKBACK_DAYS)
    if limits != (content_limit, lookback_days):
        report(
            "limits reduced: requested %s posts / %s days -> using %s / %s",
            content_limit, lookback_days, *limits,
        )
    return limits


# =========================================================
# APIFY RUN
# =========================================================


def actor_call_options(max_items: int) -> dict[str, Any]:
    """Keyword arguments for ApifyClient.actor(...).call that bound one run."""

    return {
        "max_items": max_items,
        "max_total_charge_usd": ACTOR_MAX_CHARGE_USD,
        "memory_mbytes": ACTOR_MEMORY_MBYTES,
        "run_timeout": ACTOR_RUN_TIMEOUT,
        "wait_duration": ACTOR_RUN_TIMEOUT + timedelta(minutes=1),
    }


def _field(run: Any, attr: str, key: str) -> Any:
    value = getattr(run, attr, None)
    if value is None and isinstance(run, dict):
        value = run.get(key)
    return value


def check_actor_run(actor_id: str, run: Any) -> str:
    """Raise unless the run finished successfully; return its dataset id."""

    if run is None:
        raise ScrapeGuardrailError(f"Apify actor returned no run: {actor_id}")

    status = _field(run, "status", "status")
    status = str(getattr(status, "value", status) or "").upper()
    if status != "SUCCEEDED":
        raise ScrapeGuardrailError(f"Apify actor {actor_id} finished with status {status or 'UNKNOWN'}")

    dataset_id = _field(run, "default_dataset_id", "defaultDatasetId")
    if not dataset_id:
        raise ScrapeGuardrailError(f"No dataset returned by actor: {actor_id}")
    report("%s: run SUCCEEDED", actor_id)
    return dataset_id


# =========================================================
# OUTPUT
# =========================================================


def split_error_rows(rows: list[Any]) -> tuple[list[dict], list[str]]:
    """Apify actors report failures as dataset rows with an 'error' key."""

    valid: list[dict] = []
    errors: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if row.get("error"):
            errors.append(str(row.get("errorDescription") or row["error"]))
        else:
            valid.append(row)
    return valid, errors


def check_profile_owner(requested: str, profile: dict) -> None:
    returned = str(profile.get("username") or "")
    if returned.casefold() != requested.casefold():
        raise ScrapeGuardrailError(
            f"Apify returned profile @{returned or '?'} for requested @{requested}"
        )
    report("profile owner OK: Apify returned @%s", returned)


def belongs_to(username: str, row: dict) -> bool:
    """False only when the row names a different owner."""

    owner = row.get("ownerUsername") or (row.get("owner") or {}).get("username")
    return not owner or str(owner).casefold() == username.casefold()


def truncate(text: str | None, limit: int) -> str | None:
    if text is None or len(text) <= limit:
        return text
    report("text truncated: %s -> %s characters", len(text), limit)
    return text[:limit].rstrip() + " …[truncated]"


# =========================================================
# NEUTRALITY (LLM output must describe, never judge)
# =========================================================

# Judgement belongs to the Qualification Agent, not Research.
JUDGEMENT_TERMS = [
    r"good", r"bad", r"great", r"poor", r"weak", r"strong", r"excellent", r"impressive",
    r"lacks?", r"lacking", r"missing", r"gaps?", r"opportunit(?:y|ies)",
    r"strengths?", r"weakness(?:es)?", r"should", r"recommend(?:s|ed|ation)?",
    r"needs? to", r"improve(?:s|d|ment)?", r"(?:in)?effective", r"underperform\w*",
    r"(?:high|low) engagement", r"qualif(?:y|ies|ied)", r"unprofessional",
    r"ضعيف", r"قوي", r"ممتاز", r"ينقص", r"يجب", r"فرصة", r"فجوة",
]
JUDGEMENT_RE = re.compile(r"(?<!\w)(" + "|".join(JUDGEMENT_TERMS) + r")(?!\w)", re.I)
# Quoted text reproduces what was written or said (e.g. a transcript translation).
QUOTED_RE = re.compile(r"“[^”]*”|\"[^\"]*\"|«[^»]*»")


def find_judgement_terms(text: str, source_text: str = "") -> list[str]:
    """
    Judgement words in LLM-written text. Skipped: quoted text, and words that
    also appear in the restaurant's own text (source_text, e.g. "the best
    burger"). Quoting or describing what the restaurant says is not judging it.
    """

    source = source_text.casefold()
    found: list[str] = []
    for match in JUDGEMENT_RE.finditer(QUOTED_RE.sub(" ", text or "")):
        term = match.group(0).casefold()
        if term not in source and term not in found:
            found.append(term)
    return found


def safe_media_urls(urls: list[str]) -> list[str]:
    """Keep HTTPS Instagram CDN URLs only, capped per item."""

    kept: list[str] = []
    unsafe = 0
    for url in urls:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if parsed.scheme == "https" and host.endswith(ALLOWED_MEDIA_HOST_SUFFIXES):
            kept.append(url)
        else:
            unsafe += 1
        if len(kept) >= MAX_IMAGES_PER_ITEM:
            break
    if unsafe:
        report("unsafe image URL(s) removed: %s", unsafe)
    return kept
