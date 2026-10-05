"""
PART 2 OF THE RESEARCH AGENT — ANALYZE

Takes the raw data produced by Part 1 (research_scraper) and turns it into
research evidence:

1. profile analysis   (LLM)
2. content analysis   (LLM, one item at a time, multimodal)
3. research metrics   (deterministic)
4. research signals   (deterministic)
5. coverage + data quality
"""

import json
import logging
import os
import threading
from collections import Counter
from datetime import datetime, timezone
from statistics import median

from dotenv import load_dotenv
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from langsmith.utils import ContextThreadPoolExecutor

from .research_schemas import (
    AnalysisCoverage,
    AnalysisDateRange,
    AnalysisMetadata,
    AnalyzedContent,
    ContentAnalysis,
    CountPct,
    DataQuality,
    FailedAnalysis,
    ProfileAnalysis,
    ResearchMetrics,
    ResearchProfile,
    ResearchSignal,
    RestaurantInfo,
    ScrapedContent,
    ScrapedInstagramData,
)
from .research_prompt import (
    CONTENT_ANALYSIS_SYSTEM_PROMPT,
    PROFILE_ANALYSIS_SYSTEM_PROMPT,
)
from .research_guardrails import find_judgement_terms, report
from .research_scraper import parse_timestamp

load_dotenv()

logger = logging.getLogger(__name__)


# =========================================================
# INTERNAL HELPERS
# =========================================================


def _get_llm() -> ChatOpenAI:
    if not os.getenv("OPENAI_API_KEY"):
        raise ValueError("OPENAI_API_KEY is missing from .env")
    return ChatOpenAI(
        model="gpt-5.6-luna",
        timeout=120,
        max_retries=3,
        use_responses_api=True,
    )


def _pct(count: int, total: int) -> float:
    return round((count / total) * 100, 2) if total else 0.0


def _normalize_label(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = " ".join(str(value).strip().lower().split())
    return cleaned or None


def _format_key(item: dict) -> str:
    if item.get("reel_details"):
        return "reel"

    raw = item.get("raw_data", {})
    product_type = str(raw.get("product_type") or "").lower()
    content_type = str(raw.get("content_type") or "").lower()

    if "carousel" in product_type or content_type in {"sidecar", "carousel"}:
        return "carousel"
    if content_type == "video":
        return "video"
    if content_type == "image":
        return "image"
    return content_type or product_type or "unknown"


# =========================================================
# STEP 1 — ANALYZE THE PROFILE (LLM)
# =========================================================


def analyze_instagram_profile(
    profile: dict,
    known_email: str | None = None,
    known_location: str | None = None,
) -> dict:
    """
    Describe what the scraped profile/bio states.

    Records whether the bio identifies cuisine/business, location, a CTA or
    value proposition, and whether public contact/access methods are present.
    It does not qualify the prospect or judge the profile.
    """

    structured_llm = _get_llm().with_structured_output(ProfileAnalysis, method="json_schema")

    payload = {
        "profile": profile,
        "known_database_context": {
            "email": known_email,
            "location": known_location,
        },
    }

    result = structured_llm.invoke(
        [
            SystemMessage(content=PROFILE_ANALYSIS_SYSTEM_PROMPT),
            HumanMessage(content=json.dumps(payload, ensure_ascii=False, indent=2)),
        ]
    )

    # Make deterministic contact fields authoritative where possible.
    result.contact_accessibility.website_available = bool(profile.get("website"))
    result.contact_accessibility.external_link_available = bool(profile.get("website"))
    result.contact_accessibility.email_available = bool(known_email)
    result.profile_completeness.has_external_link = bool(profile.get("website"))
    result.profile_completeness.has_location = (
        result.profile_completeness.has_location
        or result.bio.location_present
        or bool(known_location)
    )
    result.profile_completeness.has_contact_method = (
        result.contact_accessibility.website_available
        or result.contact_accessibility.email_available
        or result.contact_accessibility.phone_available
    )

    return result.model_dump()


# =========================================================
# STEP 2 — ANALYZE THE CONTENT (LLM, one item at a time)
# =========================================================


MIN_TRANSCRIPT_WORDS = 5
# How many posts are sent to OpenAI at the same time.
CONTENT_ANALYSIS_WORKERS = 5


def usable_transcript(content: ScrapedContent) -> str | None:
    """
    The Reel transcript, or None when it is song lyrics or too short to be
    real speech. The transcript stays in the saved data either way; this only
    decides whether the LLM sees it.
    """

    if not content.reel_details or not content.reel_details.transcript:
        return None

    # A track from Instagram's music library: the transcript is its lyrics.
    music = content.raw_data.music_info or {}
    if music.get("uses_original_audio") is False:
        return None

    # A few words are almost always a misheard sound, not speech.
    if len(content.reel_details.transcript.split()) < MIN_TRANSCRIPT_WORDS:
        return None

    return content.reel_details.transcript


def _analyze_single_content(item: dict) -> dict:
    content = ScrapedContent.model_validate(item)
    structured_llm = _get_llm().with_structured_output(ContentAnalysis, method="json_schema")

    transcript = usable_transcript(content)

    text_payload = {
        "content_id": content.content_id,
        "content_url": content.raw_data.content_url,
        "timestamp": content.raw_data.timestamp,
        "raw_content_type": content.raw_data.content_type,
        "product_type": content.raw_data.product_type,
        "caption": content.raw_data.caption,
        "likes": content.raw_data.likes,
        "comments": content.raw_data.comments,
        "views": content.raw_data.views,
        "hashtags": content.raw_data.hashtags,
        "alt_text": content.raw_data.alt_text,
        "reel_transcript": transcript,
        "reel_duration_seconds": (
            content.reel_details.duration_seconds if content.reel_details else None
        ),
    }

    message_content: list[dict] = [
        {
            "type": "text",
            "text": json.dumps(text_payload, ensure_ascii=False, indent=2),
        }
    ]

    for image_url in content.raw_data.image_urls[:10]:
        message_content.append(
            {
                "type": "image_url",
                "image_url": {"url": image_url, "detail": "high"},
            }
        )

    analysis = structured_llm.invoke(
        [
            SystemMessage(content=CONTENT_ANALYSIS_SYSTEM_PROMPT),
            HumanMessage(content=message_content),
        ]
    )

    return AnalyzedContent(
        **content.model_dump(),
        analysis=analysis,
        analysis_error=None,
    ).model_dump()


def check_neutrality(analyzed_content: list[AnalyzedContent]) -> list[str]:
    """
    Warn when the LLM's description of a post judges it instead of describing it.

    Evidence quotes are not checked, and words the restaurant itself wrote
    (caption, transcript, alt text, text in the image) are allowed.
    """

    warnings: list[str] = []
    checked = 0

    for item in analyzed_content:
        analysis = item.analysis
        if analysis is None:
            continue
        checked += 1

        source_text = " ".join(
            filter(None, [
                item.raw_data.caption,
                item.raw_data.alt_text,
                " ".join(item.raw_data.hashtags),
                item.reel_details.transcript if item.reel_details else None,
                analysis.text_in_visual,
            ])
        )
        written_by_llm = " ".join(
            filter(None, [
                analysis.content_type,
                analysis.visual_summary,
                analysis.caption_summary,
                analysis.transcript_summary,
                analysis.cta.intent,
                analysis.cta.description,
                analysis.promotion.intent,
                analysis.promotion.description,
                *analysis.content_categories,
                *analysis.content_themes,
                *analysis.spoken_topics,
            ])
        )

        terms = find_judgement_terms(written_by_llm, source_text)
        if terms:
            report("neutrality: content %s analysis uses judgement words %s", item.content_id, terms)
            warnings.append(
                f"Content {item.content_id}: analysis uses judgement words "
                f"({', '.join(terms)}); Research should only describe."
            )

    if not warnings:
        report("neutrality: %s analysis/analyses checked, no judgement words", checked)
    return warnings


def analyze_instagram_content(recent_content: list[dict]) -> list[dict]:
    """
    Analyze EVERY item in the supplied content window individually.

    For images/carousels: caption + actual images + engagement metadata.
    For normal videos: caption + cover/image + engagement metadata.
    For matched Reels: caption + engagement + cover/image + transcript when available.

    CTA and promotion meanings are inferred from context with no fixed taxonomy.
    One failed item does not stop analysis of the remaining items.
    """

    total = len(recent_content)
    done = 0
    lock = threading.Lock()

    def analyze(item: dict) -> dict:
        nonlocal done
        try:
            result = _analyze_single_content(item)
        except Exception as exc:
            content = ScrapedContent.model_validate(item)
            result = AnalyzedContent(
                **content.model_dump(),
                analysis=None,
                analysis_error=str(exc),
            ).model_dump()
        with lock:
            done += 1
            logger.info("analyzed post %s/%s", done, total)
        return result

    # Posts are independent, so several go to OpenAI at once. The LangSmith
    # pool keeps each call inside the current trace. map() keeps the order.
    with ContextThreadPoolExecutor(max_workers=CONTENT_ANALYSIS_WORKERS) as pool:
        return list(pool.map(analyze, recent_content))


# =========================================================
# STEP 3 — CALCULATE AGGREGATED METRICS (deterministic)
# =========================================================


def calculate_research_metrics(
    profile: dict,
    analyzed_content: list[dict],
) -> dict:
    """
    Calculate deterministic account-level research metrics.

    Calculates activity, engagement, media-format mix, content-pillar mix,
    CTA/promotion prevalence, branding, commerce visibility, and Reel metrics.
    It does not judge whether values are good or bad.
    """

    items = [AnalyzedContent.model_validate(item) for item in analyzed_content]
    total = len(items)
    now = datetime.now(timezone.utc)

    # ---------- Activity ----------
    dates = [
        parsed
        for parsed in (parse_timestamp(item.raw_data.timestamp) for item in items)
        if parsed is not None
    ]

    content_last_30_days = sum(1 for date in dates if 0 <= (now - date).days <= 30)
    content_per_week = round((content_last_30_days / 30) * 7, 2)
    days_since_last_content = None
    if dates:
        days_since_last_content = max((now - max(dates)).days, 0)

    # ---------- Engagement ----------
    engagement_items = [
        item
        for item in items
        if item.raw_data.likes is not None or item.raw_data.comments is not None
    ]

    avg_likes = med_likes = avg_comments = engagement_rate = None
    if engagement_items:
        likes = [item.raw_data.likes or 0 for item in engagement_items]
        comments = [item.raw_data.comments or 0 for item in engagement_items]

        avg_likes = round(sum(likes) / len(likes), 2)
        med_likes = round(float(median(likes)), 2)
        avg_comments = round(sum(comments) / len(comments), 2)

        followers = profile.get("followers")
        if followers and followers > 0:
            engagement_rate = round(((avg_likes + avg_comments) / followers) * 100, 3)

    # ---------- Format mix ----------
    format_counter = Counter(_format_key(item.model_dump()) for item in items)
    format_mix = {
        key: CountPct(count=count, pct=_pct(count, total))
        for key, count in format_counter.items()
    }

    # ---------- Valid analyses ----------
    valid = [item for item in items if item.analysis is not None]
    valid_total = len(valid)

    # ---------- Content mix (by content pillar) ----------
    pillar_counter: Counter[str] = Counter()
    for item in valid:
        pillar = item.analysis.content_pillar
        if pillar:
            pillar_counter[pillar] += 1

    content_mix = {
        pillar: CountPct(count=count, pct=_pct(count, valid_total))
        for pillar, count in pillar_counter.items()
    }

    # ---------- CTA ----------
    cta_items = [item for item in valid if item.analysis.cta.present]
    cta_intents: Counter[str] = Counter()
    for item in cta_items:
        label = _normalize_label(item.analysis.cta.intent)
        if label:
            cta_intents[label] += 1

    # ---------- Promotion ----------
    promo_items = [item for item in valid if item.analysis.promotion.present]
    promotion_intents: Counter[str] = Counter()
    for item in promo_items:
        label = _normalize_label(item.analysis.promotion.intent)
        if label:
            promotion_intents[label] += 1

    # ---------- Visual / commerce ----------
    logo_count = sum(1 for item in valid if item.analysis.visual_signals.logo_visible)
    branding_count = sum(1 for item in valid if item.analysis.visual_signals.branding_visible)
    menu_count = sum(1 for item in valid if item.analysis.visual_signals.menu_visible)
    price_count = sum(1 for item in valid if item.analysis.visual_signals.price_visible)
    offer_count = sum(1 for item in valid if item.analysis.visual_signals.offer_visible)

    # ---------- Reel metrics ----------
    reels = [item for item in items if item.reel_details is not None]
    enriched_reels = [
        item
        for item in reels
        if item.reel_details and item.reel_details.enrichment_success
    ]

    reel_views = [
        item.reel_details.views or item.reel_details.plays or item.raw_data.views
        for item in enriched_reels
        if (
            item.reel_details.views is not None
            or item.reel_details.plays is not None
            or item.raw_data.views is not None
        )
    ]
    reel_durations = [
        item.reel_details.duration_seconds
        for item in enriched_reels
        if item.reel_details.duration_seconds is not None
    ]
    transcript_count = sum(
        1
        for item in enriched_reels
        if item.reel_details and item.reel_details.transcript
    )

    metrics = ResearchMetrics.model_validate(
        {
            "activity": {
                "content_last_30_days": content_last_30_days,
                "content_per_week": content_per_week,
                "days_since_last_content": days_since_last_content,
            },
            "engagement": {
                "average_likes": avg_likes,
                "median_likes": med_likes,
                "average_comments": avg_comments,
                "engagement_rate_pct": engagement_rate,
                "sample_size": len(engagement_items),
            },
            "format_mix": {
                key: value.model_dump() for key, value in format_mix.items()
            },
            "content_mix": {
                key: value.model_dump() for key, value in content_mix.items()
            },
            "cta": {
                "content_with_cta": len(cta_items),
                "any_cta_pct": _pct(len(cta_items), valid_total),
                "cta_intents": dict(cta_intents),
            },
            "promotion": {
                "promotional_content": len(promo_items),
                "promotional_content_pct": _pct(len(promo_items), valid_total),
                "promotion_intents": dict(promotion_intents),
            },
            "branding": {
                "logo_presence_pct": _pct(logo_count, valid_total),
                "branding_presence_pct": _pct(branding_count, valid_total),
            },
            "commerce_visibility": {
                "menu_visible_pct": _pct(menu_count, valid_total),
                "price_visible_pct": _pct(price_count, valid_total),
                "offer_visible_pct": _pct(offer_count, valid_total),
            },
            "reels": {
                "reels_detected": len(reels),
                "reels_enriched": len(enriched_reels),
                "reel_content_pct": _pct(len(reels), total),
                "average_views": (
                    round(sum(reel_views) / len(reel_views), 2) if reel_views else None
                ),
                "median_views": round(float(median(reel_views)), 2) if reel_views else None,
                "average_duration_seconds": (
                    round(sum(reel_durations) / len(reel_durations), 2)
                    if reel_durations
                    else None
                ),
                "transcript_available_pct": _pct(transcript_count, len(enriched_reels)),
            },
        }
    )

    return metrics.model_dump()


# =========================================================
# STEP 4 — BUILD EVIDENCE-BACKED RESEARCH SIGNALS (deterministic)
# =========================================================


def build_research_signals(
    metrics: dict,
    analyzed_content: list[dict],
) -> list[dict]:
    """
    Build compact, evidence-backed observations for the Qualification Agent.

    Signals are descriptive only. They must not label the account good/bad,
    strong/weak, qualified/unqualified, or recommend any action.
    """

    m = ResearchMetrics.model_validate(metrics)
    items = [AnalyzedContent.model_validate(item) for item in analyzed_content]

    def ids_where(predicate) -> list[str]:
        return [
            item.content_id
            for item in items
            if item.content_id and item.analysis is not None and predicate(item)
        ]

    signals: list[ResearchSignal] = []

    signals.append(
        ResearchSignal(
            signal_id="activity_01",
            dimension="posting_activity",
            observation=(
                f"Observed {m.activity.content_last_30_days} content items in the last 30 days "
                f"({m.activity.content_per_week} per week in the observed window); "
                f"days since latest content: {m.activity.days_since_last_content}."
            ),
            metric_path="metrics.activity",
            value=m.activity.model_dump(),
            
        )
    )

    signals.append(
        ResearchSignal(
            signal_id="engagement_01",
            dimension="engagement",
            observation=(
                f"Across {m.engagement.sample_size} items with engagement data, average likes were "
                f"{m.engagement.average_likes}, median likes {m.engagement.median_likes}, "
                f"average comments {m.engagement.average_comments}, and follower-normalized "
                f"engagement rate was {m.engagement.engagement_rate_pct}%."
            ),
            metric_path="metrics.engagement",
            value=m.engagement.model_dump(),
            
        )
    )

    cta_ids = ids_where(lambda item: item.analysis.cta.present)
    signals.append(
        ResearchSignal(
            signal_id="cta_01",
            dimension="cta_usage",
            observation=(
                f"CTA detected in {m.cta.content_with_cta} analyzed content items "
                f"({m.cta.any_cta_pct}%). Observed intent labels: {dict(m.cta.cta_intents)}."
            ),
            metric_path="metrics.cta",
            value=m.cta.model_dump(),
            evidence_content_ids=cta_ids,
            
        )
    )

    promo_ids = ids_where(lambda item: item.analysis.promotion.present)
    signals.append(
        ResearchSignal(
            signal_id="promotion_01",
            dimension="promotional_activity",
            observation=(
                f"Promotional intent detected in {m.promotion.promotional_content} analyzed content "
                f"items ({m.promotion.promotional_content_pct}%). Observed promotion intent labels: "
                f"{dict(m.promotion.promotion_intents)}."
            ),
            metric_path="metrics.promotion",
            value=m.promotion.model_dump(),
            evidence_content_ids=promo_ids,
            
        )
    )

    commerce_ids = ids_where(
        lambda item: (
            item.analysis.visual_signals.menu_visible
            or item.analysis.visual_signals.price_visible
            or item.analysis.visual_signals.offer_visible
        )
    )
    signals.append(
        ResearchSignal(
            signal_id="commerce_01",
            dimension="commerce_visibility",
            observation=(
                f"Within analyzed visuals: menu visible in {m.commerce_visibility.menu_visible_pct}%, "
                f"price visible in {m.commerce_visibility.price_visible_pct}%, and offer messaging "
                f"visible in {m.commerce_visibility.offer_visible_pct}% of items."
            ),
            metric_path="metrics.commerce_visibility",
            value=m.commerce_visibility.model_dump(),
            evidence_content_ids=commerce_ids,
            
        )
    )

    branding_ids = ids_where(
        lambda item: (
            item.analysis.visual_signals.logo_visible
            or item.analysis.visual_signals.branding_visible
        )
    )
    signals.append(
        ResearchSignal(
            signal_id="branding_01",
            dimension="branding_presence",
            observation=(
                f"Logo was visually detected in {m.branding.logo_presence_pct}% of analyzed items; "
                f"recognizable branding was detected in {m.branding.branding_presence_pct}%."
            ),
            metric_path="metrics.branding",
            value=m.branding.model_dump(),
            evidence_content_ids=branding_ids,
            
        )
    )

    if m.reels.reels_detected > 0:
        reel_ids = [
            item.content_id
            for item in items
            if item.content_id and item.reel_details is not None
        ]
        signals.append(
            ResearchSignal(
                signal_id="reels_01",
                dimension="reel_activity",
                observation=(
                    f"{m.reels.reels_detected} of the observed content items were matched as Reels "
                    f"({m.reels.reel_content_pct}% of the content window). {m.reels.reels_enriched} "
                    f"were enriched; average Reel views: {m.reels.average_views}; transcript "
                    f"availability: {m.reels.transcript_available_pct}%."
                ),
                metric_path="metrics.reels",
                value=m.reels.model_dump(),
                evidence_content_ids=reel_ids,
            )
        )

    return [signal.model_dump() for signal in signals]


# =========================================================
# STEP 5 — COVERAGE + DATA QUALITY
# =========================================================


def _build_coverage(
    content_limit: int,
    scraped_content: list[ScrapedContent],
    analyzed_content: list[AnalyzedContent],
) -> AnalysisCoverage:
    successful = [
        item
        for item in analyzed_content
        if item.analysis is not None and not item.analysis_error
    ]
    failed = [
        item
        for item in analyzed_content
        if item.analysis is None or item.analysis_error
    ]

    dates = [
        parsed
        for parsed in (parse_timestamp(item.raw_data.timestamp) for item in analyzed_content)
        if parsed is not None
    ]

    reels = [item for item in analyzed_content if item.reel_details is not None]
    enriched_reels = [
        item
        for item in reels
        if item.reel_details and item.reel_details.enrichment_success
    ]
    transcript_count = sum(
        1
        for item in enriched_reels
        if item.reel_details and item.reel_details.transcript
    )

    return AnalysisCoverage(
        content_requested=content_limit,
        content_scraped=len(scraped_content),
        content_analyzed=len(successful),
        content_failed=len(failed),
        analysis_success_pct=(
            round((len(successful) / len(scraped_content)) * 100, 2)
            if scraped_content
            else 0.0
        ),
        date_range=AnalysisDateRange(
            oldest_content=min(dates).isoformat() if dates else None,
            newest_content=max(dates).isoformat() if dates else None,
        ),
        multimodal_analysis=bool(successful),
        reels_detected=len(reels),
        reels_enriched=len(enriched_reels),
        reel_transcripts_available=transcript_count,
    )


def _build_data_quality(
    profile: dict | None,
    scraped_content: list[ScrapedContent],
    analyzed_content: list[AnalyzedContent],
    reel_enrichment_called: bool,
) -> DataQuality:
    failed_records: list[FailedAnalysis] = []
    warnings: list[str] = []

    for item in analyzed_content:
        if item.analysis_error:
            failed_records.append(
                FailedAnalysis(
                    content_id=item.content_id,
                    stage="multimodal_analysis",
                    reason=item.analysis_error,
                )
            )

    reel_items = [item for item in analyzed_content if item.reel_details is not None]

    if reel_items:
        warnings.append(
            "Reel/video visual analysis uses supplied cover/images plus transcript when available; full raw video frames are not analyzed."
        )
        no_transcript = [
            item.content_id
            for item in reel_items
            if item.reel_details and not item.reel_details.transcript
        ]
        if no_transcript:
            warnings.append(
                f"No Reel transcript was available for {len(no_transcript)} matched Reel item(s)."
            )

    # Normal video items that were not matched as Reels are still analyzed.
    unmatched_videos = [
        item.content_id
        for item in analyzed_content
        if (
            str(item.raw_data.content_type or "").lower() == "video"
            and item.reel_details is None
        )
    ]
    if unmatched_videos:
        warnings.append(
            f"{len(unmatched_videos)} video item(s) were not matched by the Reel scraper; they were analyzed using caption, engagement metadata, and available cover/images."
        )

    profile_ok = bool(profile)
    scrape_ok = bool(scraped_content)
    successful_count = sum(
        1
        for item in analyzed_content
        if item.analysis is not None and not item.analysis_error
    )
    multimodal_complete = bool(analyzed_content) and successful_count == len(analyzed_content)

    reel_enrichment_complete = True
    if reel_items:
        reel_enrichment_complete = all(
            item.reel_details is not None and item.reel_details.enrichment_success
            for item in reel_items
        )

    if profile_ok and scrape_ok and multimodal_complete:
        status = "complete"
    elif profile_ok or scrape_ok or successful_count > 0:
        status = "partial"
    else:
        status = "failed"

    return DataQuality(
        status=status,
        profile_scrape_success=profile_ok,
        content_scrape_success=scrape_ok,
        multimodal_analysis_completed=multimodal_complete,
        reel_enrichment_attempted=reel_enrichment_called,
        reel_enrichment_completed=reel_enrichment_complete,
        warnings=warnings,
        failed_analyses=failed_records,
    )


# =========================================================
# PART 2 ENTRY POINT
# =========================================================


def analyze_instagram_data(
    restaurant: RestaurantInfo,
    scraped: ScrapedInstagramData,
) -> ResearchProfile:
    """
    Analyze the raw data from Part 1 and return the full research result.

    Works only from `scraped` (plus the email/location already stored for the
    restaurant). It performs no scraping and makes no gap or qualification
    judgement.
    """

    analyzed_at = datetime.now(timezone.utc).isoformat()
    profile = scraped.profile

    # 1) Describe the profile.
    profile_analysis = ProfileAnalysis.model_validate(
        analyze_instagram_profile(
            profile=profile.model_dump(),
            known_email=restaurant.email,
            known_location=restaurant.location,
        )
    )

    # 2) Describe each content item.
    analyzed_content_raw = analyze_instagram_content(
        [item.model_dump() for item in scraped.content]
    )
    analyzed_content = [
        AnalyzedContent.model_validate(item) for item in analyzed_content_raw
    ]

    # 3) Calculate deterministic metrics from the analyzed evidence.
    metrics = ResearchMetrics.model_validate(
        calculate_research_metrics(
            profile=profile.model_dump(),
            analyzed_content=analyzed_content_raw,
        )
    )

    # 4) Build descriptive, evidence-backed signals for the qualification handoff.
    research_signals = [
        ResearchSignal.model_validate(item)
        for item in build_research_signals(
            metrics=metrics.model_dump(),
            analyzed_content=analyzed_content_raw,
        )
    ]

    # 5) Coverage and data quality.
    coverage = _build_coverage(
        content_limit=scraped.content_limit,
        scraped_content=scraped.content,
        analyzed_content=analyzed_content,
    )
    data_quality = _build_data_quality(
        profile=profile.model_dump(),
        scraped_content=scraped.content,
        analyzed_content=analyzed_content,
        reel_enrichment_called=True,
    )
    skipped_transcripts = sum(
        1
        for item in scraped.content
        if item.reel_details and item.reel_details.transcript and usable_transcript(item) is None
    )
    transcript_warnings = (
        [f"{skipped_transcripts} Reel transcript(s) not sent to the analysis (song lyrics or too short)."]
        if skipped_transcripts
        else []
    )

    data_quality.warnings = [
        *scraped.scrape_warnings,
        *transcript_warnings,
        *check_neutrality(analyzed_content),
        *data_quality.warnings,
    ]

    return ResearchProfile(
        restaurant=restaurant,
        analysis_metadata=AnalysisMetadata(
            status=data_quality.status,
            analyzed_at=analyzed_at,
            scraped_at=scraped.scraped_at,
        ),
        profile=profile,
        profile_analysis=profile_analysis,
        metrics=metrics,
        research_signals=research_signals,
        content=analyzed_content,
        analysis_coverage=coverage,
        data_quality=data_quality,
    )
