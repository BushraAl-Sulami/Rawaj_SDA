"""Research Agent Apify guardrails. Apify is faked here: no paid calls are made."""
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from agents.research_agent import research_scraper as scraper
from agents.research_agent.research_analyzer import check_neutrality, usable_transcript
from agents.research_agent.research_guardrails import (
    MAX_IMAGES_PER_ITEM,
    ScrapeGuardrailError,
    actor_call_options,
    check_actor_run,
    clamp_scrape_limits,
    find_judgement_terms,
    normalize_instagram_username,
    safe_media_urls,
    split_error_rows,
    truncate,
)
from agents.research_agent.research_schemas import (
    AnalyzedContent,
    ContentAnalysis,
    ContentRawData,
    ReelDetails,
    ScrapedContent,
)

USERNAME = "dearduck.sa"
NOW = datetime.now(timezone.utc)
CDN_IMAGE = "https://scontent.cdninstagram.com/photo.jpg"


def post(i: int, days_ago: int, owner: str = USERNAME, **extra) -> dict:
    return {
        "id": str(i), "shortCode": f"code{i}", "ownerUsername": owner, "type": "Image",
        "timestamp": (NOW - timedelta(days=days_ago)).isoformat(),
        "caption": f"post {i}", "displayUrl": CDN_IMAGE, **extra,
    }


def fake_apify(profile: dict, posts: list[dict], calls: list | None = None):
    """Stand-in for _run_apify_actor that also applies the error-row guardrail."""

    def run(actor_id, run_input, max_items, warnings=None):
        if calls is not None:
            calls.append((actor_id, max_items))
        rows = [profile] if actor_id == scraper.PROFILE_ACTOR else posts
        valid, errors = split_error_rows(rows)
        if errors and warnings is not None:
            warnings.append(f"{actor_id} reported {len(errors)} error row(s): {errors[0]}")
        return valid[:max_items]

    return run


# ---------- Layer 1: before Apify ----------


@pytest.mark.parametrize("value", ["dearduck.sa", "@dearduck.sa", "https://www.instagram.com/dearduck.sa/"])
def test_username_accepts_handle_at_sign_and_profile_url(value):
    assert normalize_instagram_username(value) == USERNAME


@pytest.mark.parametrize("value", ["", "   ", "dear duck", "x" * 31, "../etc", ".dearduck", "dear..duck"])
def test_invalid_username_is_rejected_before_any_apify_call(value):
    with patch.object(scraper, "_run_apify_actor") as apify, pytest.raises(ValueError):
        scraper.scrape_instagram_data(value, content_limit=30, lookback_days=90)
    apify.assert_not_called()


def test_limits_are_capped_at_30_posts_and_90_days():
    assert clamp_scrape_limits(100, 365) == (30, 90)
    assert clamp_scrape_limits(20, 30) == (20, 30)


@pytest.mark.parametrize("limits", [(0, 90), (30, 0), (-5, 90)])
def test_non_positive_limits_are_rejected(limits):
    with pytest.raises(ValueError):
        clamp_scrape_limits(*limits)


def test_scrape_asks_apify_for_at_most_30_posts_from_last_90_days():
    posts = [post(i, days_ago=i * 3) for i in range(60)]  # up to 177 days old
    calls: list = []
    with patch.object(scraper, "_run_apify_actor", side_effect=fake_apify({"username": USERNAME}, posts, calls)):
        data = scraper.scrape_instagram_data(USERNAME, content_limit=100, lookback_days=365)

    assert (data.content_limit, data.lookback_days) == (30, 90)
    assert (scraper.POST_ACTOR, 30) in calls
    assert 0 < len(data.content) <= 30
    oldest = min(datetime.fromisoformat(c.raw_data.timestamp) for c in data.content)
    assert NOW - oldest <= timedelta(days=90)


def test_private_account_skips_the_paid_content_scrape():
    calls: list = []
    profile = {"username": USERNAME, "private": True}
    with patch.object(scraper, "_run_apify_actor", side_effect=fake_apify(profile, [], calls)):
        data = scraper.scrape_instagram_data(USERNAME, content_limit=30, lookback_days=90)

    assert [actor for actor, _ in calls] == [scraper.PROFILE_ACTOR]
    assert data.content == []
    assert any("private" in w for w in data.scrape_warnings)


# ---------- Layer 2: during Apify ----------


def test_every_actor_run_has_cost_and_time_limits():
    options = actor_call_options(max_items=30)
    assert options["max_items"] == 30
    assert options["max_total_charge_usd"] > 0
    assert options["memory_mbytes"] > 0
    assert options["run_timeout"].total_seconds() > 0


@pytest.mark.parametrize("status", ["FAILED", "TIMED-OUT", "ABORTED", None])
def test_unsuccessful_actor_run_is_rejected(status):
    with pytest.raises(ScrapeGuardrailError):
        check_actor_run("some/actor", {"status": status, "defaultDatasetId": "ds1"})


def test_successful_actor_run_returns_its_dataset():
    assert check_actor_run("some/actor", {"status": "SUCCEEDED", "defaultDatasetId": "ds1"}) == "ds1"


# ---------- Layer 3: after Apify ----------


def test_apify_error_rows_are_dropped_and_reported():
    posts = [post(1, 1), {"error": "not_found", "errorDescription": "Post not found"}]
    with patch.object(scraper, "_run_apify_actor", side_effect=fake_apify({"username": USERNAME}, posts)):
        data = scraper.scrape_instagram_data(USERNAME, content_limit=30, lookback_days=90)

    assert [c.content_id for c in data.content] == ["1"]
    assert any("Post not found" in w for w in data.scrape_warnings)


def test_wrong_profile_returned_by_apify_stops_the_scrape():
    with patch.object(scraper, "_run_apify_actor", side_effect=fake_apify({"username": "someone_else"}, [])):
        with pytest.raises(ScrapeGuardrailError):
            scraper.scrape_instagram_data(USERNAME, content_limit=30, lookback_days=90)


def test_posts_from_other_accounts_are_dropped():
    posts = [post(1, 1), post(2, 2, owner="someone_else")]
    with patch.object(scraper, "_run_apify_actor", side_effect=fake_apify({"username": USERNAME}, posts)):
        data = scraper.scrape_instagram_data(USERNAME, content_limit=30, lookback_days=90)

    assert [c.content_id for c in data.content] == ["1"]
    assert any("another account" in w for w in data.scrape_warnings)


def test_long_captions_are_truncated():
    posts = [post(1, 1, caption="x" * 9000)]
    with patch.object(scraper, "_run_apify_actor", side_effect=fake_apify({"username": USERNAME}, posts)):
        data = scraper.scrape_instagram_data(USERNAME, content_limit=30, lookback_days=90)

    caption = data.content[0].raw_data.caption
    assert len(caption) < 9000 and caption.endswith("[truncated]")
    assert truncate("short", 100) == "short"


def test_only_https_instagram_cdn_images_reach_the_llm():
    urls = [
        CDN_IMAGE,
        "https://scontent.xx.fbcdn.net/photo.jpg",
        "http://scontent.cdninstagram.com/insecure.jpg",
        "https://evil.example.com/photo.jpg",
        "https://cdninstagram.com.evil.com/photo.jpg",
    ]
    assert safe_media_urls(urls) == urls[:2]
    assert len(safe_media_urls([CDN_IMAGE] * 50)) == MAX_IMAGES_PER_ITEM


# ---------- Neutrality: the LLM analysis must describe, not judge ----------


def analyzed(caption: str, **analysis) -> AnalyzedContent:
    fields = {
        "content_type": "single photo", "content_pillar": "food_product",
        "visual_summary": "A burger on a wooden table.",
        "caption_summary": "The caption names the burger.", **analysis,
    }
    return AnalyzedContent(
        content_id="1", raw_data=ContentRawData(caption=caption),
        analysis=ContentAnalysis(**fields),
    )


def test_neutral_description_passes():
    assert check_neutrality([analyzed("New smash burger")]) == []


@pytest.mark.parametrize("summary, word", [
    ("The account has weak branding.", "weak"),
    ("The post is missing a call to action.", "missing"),
    ("The restaurant should post more Reels.", "should"),
    ("This shows low engagement.", "low engagement"),
    ("المحتوى ضعيف", "ضعيف"),
])
def test_judgement_words_in_the_analysis_are_flagged(summary, word):
    warnings = check_neutrality([analyzed("New smash burger", visual_summary=summary)])
    assert len(warnings) == 1 and word in warnings[0]


def test_words_the_restaurant_wrote_itself_are_allowed():
    item = analyzed(
        "Our strong coffee, you should try it!",
        caption_summary="The caption calls the coffee strong and says you should try it.",
    )
    assert check_neutrality([item]) == []


def test_quoted_text_such_as_a_transcript_translation_is_allowed():
    item = analyzed(
        "New smash burger",
        transcript_summary="The transcript is in Portuguese, meaning “The aquarium was still missing.”",
    )
    assert check_neutrality([item]) == []
    assert find_judgement_terms('It says "weak" but the dish is missing') == ["missing"]


def test_judgement_words_inside_a_word_are_not_flagged():
    assert find_judgement_terms("A goodbye note next to a bad-free... gapfill") == ["bad"]
    assert find_judgement_terms("Goodies and strongbox") == []


# ---------- Reel transcripts: skip song lyrics and noise ----------


def reel(transcript: str | None, uses_original_audio: bool | None = True) -> ScrapedContent:
    music = None if uses_original_audio is None else {"uses_original_audio": uses_original_audio}
    return ScrapedContent(
        content_id="1",
        raw_data=ContentRawData(music_info=music),
        reel_details=ReelDetails(transcript=transcript),
    )


def test_real_voice_over_is_kept():
    text = "Try our new chicken waffle this weekend"
    assert usable_transcript(reel(text)) == text
    assert usable_transcript(reel(text, uses_original_audio=None)) == text


def test_transcript_of_a_licensed_song_is_skipped():
    lyrics = "Bonjour soleil, bonjour la vie, le café coule, tout est joli"
    assert usable_transcript(reel(lyrics, uses_original_audio=False)) is None


@pytest.mark.parametrize("text", ["Tulu.", "The shore.", "Who took the bomb?", "\n", ""])
def test_too_short_transcript_is_skipped(text):
    assert usable_transcript(reel(text)) is None


def test_post_without_a_reel_has_no_transcript():
    assert usable_transcript(ScrapedContent(content_id="1", raw_data=ContentRawData())) is None


def test_failed_reel_enrichment_keeps_the_content_and_warns():
    reel = post(1, 1, type="Video", productType="clips", url="https://www.instagram.com/reel/code1/")

    def run(actor_id, run_input, max_items, warnings=None):
        if actor_id == scraper.REEL_ACTOR:
            raise ScrapeGuardrailError("Apify actor finished with status FAILED")
        return fake_apify({"username": USERNAME}, [reel])(actor_id, run_input, max_items, warnings)

    with patch.object(scraper, "_run_apify_actor", side_effect=run):
        data = scraper.scrape_instagram_data(USERNAME, content_limit=30, lookback_days=90)

    assert [c.content_id for c in data.content] == ["1"]
    assert data.content[0].reel_details is None
    assert any("Reel enrichment failed" in w for w in data.scrape_warnings)
