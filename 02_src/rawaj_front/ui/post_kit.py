"""Pure helpers for facts, captions, media checks, preview HTML, and progress. No API or UI state."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import re
from html import escape

from ui.icons import icon

MORE_CUTOFF = 125
LANGUAGES = ["Arabic", "English", "Bilingual"]
FORMAT_NAMES = {
    "single_image": "Single image",
    "carousel": "Carousel",
    "reel": "Reel",
    "story": "Story",
    "Post": "Post",
    "Reel": "Reel",
    "Story": "Story",
}
TONES = {"Warm": "warm", "Playful": "playful", "Premium": "premium", "Direct": "direct"}
TONE_NOTES = {
    "warm": "Friendly and personal, like the owner talking to a regular guest.",
    "playful": "Light, with a smile in it.",
    "premium": "Calm and refined: fewer exclamation marks, no slang.",
    "direct": "Short: what it is and what to do, first.",
}
CHIPS = {"Shorter": "shorter", "Stronger hook": "hook"}
STEPS = ["Idea selected", "Kit ready", "Scheduled"]
DELIVERY_CHOICES = ["Visit us", "Pickup", "Jahez", "HungerStation", "Keeta"]

_HEX = re.compile(r"^#[0-9A-Fa-f]{6}$")
_ARABIC = re.compile(r"[\u0621-\u063A\u0641-\u064A]")
_STOP = {
    "this",
    "that",
    "with",
    "from",
    "have",
    "your",
    "their",
    "them",
    "than",
    "into",
    "after",
    "before",
    "which",
    "about",
    "more",
    "less",
}

# Workspace defaults and facts-plan helpers

def default_facts(restaurant: dict) -> dict:
    """Return the smallest safe facts object accepted by the API."""
    context = restaurant.get("context") or {}
    said = str(context.get("language") or "").casefold()
    if any(word in said for word in ("bilingual", "both", "english and arabic", "arabic and english")):
        language = "Bilingual"
    elif "arab" in said:
        language = "Arabic"
    else:
        language = "English"
    return {
        "items": [],
        "channels": [],
        "offer": "",
        "notes": "",
        "language": language,
        "has_photo": False,
    }


def generic_plan() -> dict:
    """Fallback form when the idea-specific facts planner cannot be reached."""
    return {
        "kind": "other",
        "summary": "Confirm only the concrete details this post will mention.",
        "items_label": "Items shown in this post",
        "items_min": 0,
        "items_max": 6,
        "wants_prices": False,
        "wants_groups": False,
        "wants_channels": False,
        "wants_offer": False,
        "notes_prompt": "Any other confirmed detail this post may mention?",
        "suggested_items": [],
        "suggested_channels": [],
        "suggested_groups": [],
        "fallback": True,
    }


def _blank_row(name: str = "", group: str = "") -> dict:
    arabic = bool(_ARABIC.search(name))
    return {
        "name_en": "" if arabic else name,
        "name_ar": name if arabic else "",
        "price": "",
        "group": group,
    }


def starting_rows(plan: dict, restaurant: dict) -> list[dict]:
    """Seed rows without asking the owner to retype moments already in the idea.

    For ``menu_groups``, each suggested moment/category receives its own row first.
    This is the key behavior for ideas such as "catch-up / on-the-go / slow cup".
    """
    top = max(0, int(plan.get("items_max") or 0))
    if top == 0:
        return []

    names = [str(v).strip() for v in plan.get("suggested_items") or [] if str(v).strip()]
    groups = [str(v).strip() for v in plan.get("suggested_groups") or [] if str(v).strip()]

    saved = (restaurant.get("context") or {}).get("signature_items")
    if not names and plan.get("kind") == "single_item" and isinstance(saved, (list, tuple)) and saved:
        first = str(saved[0]).strip()
        if first:
            names = [first]

    rows: list[dict] = []
    if plan.get("kind") == "menu_groups" and groups:
        for i, group in enumerate(groups[:top]):
            rows.append(_blank_row(names[i] if i < len(names) else "", group))
        for name in names[len(rows) : top]:
            rows.append(_blank_row(name, ""))
    else:
        rows = [_blank_row(name) for name in names[:top]]

    minimum = min(max(int(plan.get("items_min") or 0), 0), top)
    target = min(max(minimum, len(groups) if plan.get("kind") == "menu_groups" else 0, len(rows)), top)
    while len(rows) < target:
        group = groups[len(rows)] if len(rows) < len(groups) else ""
        rows.append(_blank_row(group=group))
    return rows


def missing_item_count(plan: dict, items: list[dict]) -> int:
    named = [item for item in items if (item.get("name_en") or "").strip() or (item.get("name_ar") or "").strip()]
    return max(0, int(plan.get("items_min") or 0) - len(named))


def facts_signature(facts: dict) -> str:
    """Fingerprint only facts that can change generated public copy/instructions.

    Media availability affects the execution instructions; colours are only a preview preference.
    """
    relevant = {
        "items": facts.get("items") or [],
        "channels": facts.get("channels") or [],
        "offer": facts.get("offer") or "",
        "notes": facts.get("notes") or "",
        "language": facts.get("language") or "English",
        "has_photo": bool(facts.get("has_photo")),
    }
    canonical = json.dumps(relevant, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def execution_format(resp: dict | None, idea_format: str) -> str:
    """Use the generated format when available; otherwise map the idea's broad format."""
    if resp:
        value = ((resp.get("kit") or {}).get("shoot") or {}).get("format")
        if value:
            return value
    return {"post": "single_image", "reel": "reel", "story": "story"}.get(
        str(idea_format).strip().casefold(), "single_image"
    )


def format_name(value: str) -> str:
    return FORMAT_NAMES.get(value, value.replace("_", " ").title())


def pick_gap(plan: dict, task: dict, idea: dict) -> dict | None:
    """Pick the gap that shares the most meaningful words with this task/idea."""
    gaps = plan.get("gaps") or []
    if not gaps:
        return None

    def words(text: str) -> set[str]:
        return {word for word in re.findall(r"[a-z]{4,}", text.casefold()) if word not in _STOP}

    own = words(
        f"{task.get('title', '')} {task.get('text', '')} {idea.get('description', '')} "
        f"{idea.get('why_it_fits', '')} {idea.get('angle', '')}"
    )
    return max(
        gaps,
        key=lambda gap: len(
            own & words(f"{gap.get('gap', '')} {gap.get('key_point', '')} {gap.get('highlight_label') or ''}")
        ),
    )


def next_task(plan: dict, task: dict) -> dict | None:
    later = [
        t
        for t in plan.get("tasks", [])
        if t["date"] > task["date"] and t.get("status") != "Completed" and t.get("ideas", True)
    ]
    return min(later, key=lambda t: (t["date"], t["day"]), default=None)


# Caption and copy helpers


def direction(text: str) -> str:
    first = next((char for char in text if char.isalpha()), "")
    return "rtl" if first and _ARABIC.match(first) else "ltr"


def cut_at_more(text: str, cutoff: int = MORE_CUTOFF) -> tuple[str, bool]:
    if len(text) <= cutoff:
        return text, False
    head = text[:cutoff]
    space = head.rfind(" ")
    return (head[:space] if space > cutoff // 2 else head).rstrip(), True


def subjects(facts: dict) -> list[str]:
    names = [name for item in facts.get("items") or [] for name in (item.get("name_en"), item.get("name_ar")) if name]
    return names + [channel for channel in facts.get("channels") or [] if channel]


def subject_check(text: str, facts: dict, cutoff: int = MORE_CUTOFF, story: bool = False) -> tuple[str, str]:
    if story:
        return "na", "A Story has no ‘more’ cut-off."
    names = subjects(facts)
    if not names:
        return "na", "No menu item or ordering channel is required for this idea."
    lowered = text.casefold()
    found = [(lowered.find(name.casefold()), name) for name in names if name.casefold() in lowered]
    if not found:
        return "missing", "The caption does not name any confirmed item or ordering channel."
    start, name = min(found)
    if start + len(name) > cutoff:
        return "late", f"“{name}” appears after Instagram's ‘more’. Move it earlier."
    return "ok", f"“{name}” appears before ‘more’."


def _stem(word: str) -> str:
    return word.casefold()[: max(4, len(word) - 2)]


def details_check(text: str, words: list[str]) -> tuple[str, str] | None:
    """Whether the caption uses the owner's confirmed details (the API's ``detail_words``); None when there are none."""
    if not words:
        return None
    lowered = text.casefold()
    used = [word for word in words if _stem(word) in lowered]
    if not used:
        return "missing", f"The caption uses none of your details. Add one, e.g. “{words[0]}”."
    return "ok", "Uses your details: " + ", ".join(used[:3]) + "."


def gap_text(gap: dict | None) -> str:
    """The gap as one line for the API, so the form and the kit are built to fix what the owner is shown."""
    if not gap:
        return ""
    parts = [gap.get("gap") or "", gap.get("highlight_label") or "", gap.get("key_point") or ""]
    return " · ".join(part.strip() for part in parts if part and part.strip())[:600]


def open_claims(resp: dict, caption: str) -> list[dict]:
    """Flagged claims still in the public copy: one the owner removed from the caption no longer needs a look."""
    kit = resp["kit"]
    other = " ".join(
        [
            kit["visual"].get("text_overlay") or "",
            *(shot.get("overlay_text") or "" for shot in kit["shoot"].get("shots") or []),
            *(kit.get("hashtags") or []),
        ]
    ).casefold()
    lowered = caption.casefold()
    return [
        claim
        for claim in resp.get("unsupported_claims") or []
        if claim["text"].strip().casefold() in lowered or claim["text"].strip().casefold() in other
    ]


_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")


def gap_fixes(gap: dict | None, resp: dict, caption: str, facts: dict, cutoff: int = MORE_CUTOFF) -> list[tuple[str, str]]:
    """(state, text) for each way this post answers the gap, recomputed as the owner edits the caption."""
    kit = resp["kit"]
    about = f"{(gap or {}).get('gap', '')} {(gap or {}).get('highlight_label') or ''}".casefold()
    fixes: list[tuple[str, str]] = []
    story = kit["shoot"]["format"] == "story"

    if subjects(facts) and not story:
        state, _ = subject_check(caption, facts, cutoff)
        fixes.append(("ok", "Item named before ‘more’") if state == "ok" else ("missing", "Name the item before ‘more’"))
    if "pric" in about or "commerce" in about or "سعر" in about:
        prices = [item["price"] for item in facts.get("items") or [] if item.get("price")]
        public = " ".join([caption, *(shot.get("overlay_text") or "" for shot in kit["shoot"].get("shots") or [])])
        wanted = {n for price in prices for n in _NUMBER.findall(price)}
        if not prices:
            fixes.append(("missing", "Add the price in the form to fix this gap"))
        elif wanted & set(_NUMBER.findall(public)):
            fixes.append(("ok", "Price visible"))
        else:
            fixes.append(("missing", "Show the price in the caption"))
    if any(word in about for word in ("engag", "interact", "comment", "conversation", "audience")):
        last = caption.strip().splitlines()[-1] if caption.strip() else ""
        fixes.append(
            ("ok", "Ends with a question followers can answer")
            if "?" in last or "؟" in last
            else ("missing", "End with a question")
        )
        if any(f.get("format") == "Story" and f.get("sticker") in {"poll", "quiz", "question"} for f in kit.get("follow_ups") or []):
            fixes.append(("ok", "Story poll keeps the conversation going"))
    detail = details_check(caption, resp.get("detail_words") or [])
    if detail:
        fixes.append(("ok", "Uses your confirmed details") if detail[0] == "ok" else ("missing", "Use a detail you confirmed"))
    if resp.get("claims_checked"):
        count = len(open_claims(resp, caption))
        fixes.append(
            ("ok", "Every claim checked against your facts")
            if not count
            else ("missing", f"{count} claim{'s' if count != 1 else ''} need{'' if count != 1 else 's'} your confirmation")
        )
    return fixes


def hashtag_text(hashtags: list[str]) -> str:
    return "\n".join(hashtags)


def shot_card(number: int, shot: dict, existing: bool = False) -> str:
    """One step of the guide; with ``existing`` media the tip is an editing tip, not a camera tip."""
    seconds = f"<i>{shot['seconds']} s</i>" if shot.get("seconds") is not None else ""
    overlay = (
        f'<p class="overlay"><b>On-screen text:</b> {escape(shot["overlay_text"])}</p>' if shot.get("overlay_text") else ""
    )
    return (
        f'<div class="shot"><span class="n">{number}</span><div class="body">'
        f"<b>{escape(shot['title'])}{seconds}</b>"
        f"<p>{escape(shot['instruction'])}</p>"
        f'<p class="tip{" edit" if existing else ""}">{escape(shot["phone_tip"])}</p>{overlay}</div></div>'
    )


def shot_list_text(shoot: dict) -> str:
    head = format_name(shoot["format"])
    if shoot.get("duration_seconds"):
        head += f" · {shoot['duration_seconds']} seconds"
    lines = [f"EXECUTION GUIDE · {head}", f"Why this format: {shoot.get('format_reason', '')}".strip()]
    if shoot.get("hook"):
        lines.append(f"Hook (first 2 seconds): {shoot['hook']}")
    for number, shot in enumerate(shoot.get("shots") or [], 1):
        seconds = f" ({shot['seconds']} s)" if shot.get("seconds") is not None else ""
        lines.append(f"{number}. {shot['title']}{seconds}: {shot['instruction']}")
        if shot.get("overlay_text"):
            lines.append(f"   On-screen text: {shot['overlay_text']}")
        lines.append(f"   Phone tip: {shot['phone_tip']}")
    checklist = shoot.get("checklist") or []
    if checklist:
        lines.extend(["", "Before you shoot:", *[f"- {item}" for item in checklist]])
    return "\n".join(lines)


def best_time_summary(best_time: dict, when: str, passed: bool = False) -> tuple[str, str, str]:
    """(headline, detail, text) for the "Best time to post" card.

    ``best_time`` is the API's ``best_posting_time`` result: ``status`` is "ok" (a real window found from the
    account's own posts), "no_clear_difference" or "not_enough_data" (both carry a ``general`` fallback suggestion
    instead). ``when`` is the task's own day (e.g. "Thursday 24 Sep"); ``passed`` is True once that day is already
    behind us, so the card reads as a look-back instead of advice.
    """
    if best_time.get("status") == "ok":
        headline = best_time.get("label", "")
        tense = "was" if passed else "is"
        confidence = best_time.get("confidence", "low")
        detail = f"Your best time to post {tense} {headline}, from your own posts ({confidence} confidence)."
    else:
        general = best_time.get("general") or {}
        headline = general.get("label", "Evening")
        detail = f"For {when}: {general.get('note', '')}"
    return headline, detail.strip(), best_time.get("basis", "")


def best_time_text(best_time: dict, when: str) -> str:
    """One paragraph for the plain-text copy of the whole kit (the "BEST TIME" section's body)."""
    _, detail, text = best_time_summary(best_time, when)
    return f"{detail} {text}".strip()


def whole_kit_text(resp: dict, caption: str, overlay: str, when: str, title: str) -> str:
    kit = resp["kit"]
    execution = kit.get("execution") or {}
    parts = [
        title,
        "",
        "WHAT TO MAKE",
        execution.get("what_to_make", ""),
        f"Goal: {execution.get('goal', '')}",
        f"Start with: {execution.get('owner_action', '')}",
        "",
        "CAPTION",
        caption,
        "",
        "HASHTAGS",
        hashtag_text(kit.get("hashtags") or []),
        "",
        "LOCATION TAG",
        kit.get("location_tag", ""),
    ]
    if kit.get("mentions"):
        parts += [
            "",
            "MENTIONS",
            *[f"- {m.get('handle') or m.get('who')}: {m.get('why', '')}" for m in kit["mentions"]],
        ]
    parts += ["", "COVER", kit["visual"]["cover_frame"]]
    if overlay:
        parts.append(f"Cover text: {overlay}")
    parts += ["", shot_list_text(kit["shoot"]), "", "BEST TIME", best_time_text(resp["best_time"], when)]
    parts += [
        "",
        "FOLLOW-UPS",
        *[f"- {f['format']} · {f['title']} ({f['timing']}): {f['description']}" for f in kit.get("follow_ups") or []],
    ]
    return "\n".join(part for part in parts if part is not None)


# Media checks 
def _vertical_format(content_format: str) -> bool:
    return content_format in {"Reel", "Story", "reel", "story"}


def photo_check(data: bytes, content_format: str) -> dict:
    """Basic local image check: resolution, aspect ratio, and brightness only."""
    from PIL import Image, ImageOps, ImageStat, UnidentifiedImageError

    try:
        image = ImageOps.exif_transpose(Image.open(io.BytesIO(data)))
        image.load()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise ValueError("This file could not be read as an image.") from exc

    width, height = image.size
    vertical = _vertical_format(content_format)
    target = "9:16" if vertical else "4:5"
    items: list[tuple[str, str]] = []

    if width >= 1080:
        items.append(("ok", f"Sharp enough: {width} × {height} px."))
    elif width >= 720:
        items.append(("warn", f"{width} × {height} px works, but 1080 px wide will look crisper."))
    else:
        items.append(("bad", f"Only {width} × {height} px. Retake it at a higher resolution."))

    ratio = width / height
    if vertical:
        if 0.5 <= ratio <= 0.62:
            items.append(("ok", "Vertical 9:16: it fills the screen."))
        else:
            items.append(("warn", "This is not close to 9:16. Retake it vertically for a Reel/Story."))
    elif 0.8 <= ratio <= 1.91:
        items.append(("ok", "The aspect ratio works in the feed."))
    else:
        items.append(("warn", "Instagram will crop this image. Keep the important subject near the centre."))

    grey = image.convert("L")
    grey.thumbnail((256, 256))
    light = ImageStat.Stat(grey).mean[0]
    if light < 70:
        items.append(("warn", "It looks dark. Move closer to soft daylight."))
    elif light > 205:
        items.append(("warn", "It looks very bright. Move away from harsh direct light."))
    else:
        items.append(("ok", "The overall brightness looks balanced."))

    preview = image.convert("RGB")
    preview.thumbnail((720, 720))
    buffer = io.BytesIO()
    preview.save(buffer, "JPEG", quality=82)
    return {
        "width": width,
        "height": height,
        "target": target,
        "items": items,
        "preview": base64.b64encode(buffer.getvalue()).decode("ascii"),
    }


def _is_dark(colour: str) -> bool:
    red, green, blue = (int(colour[i : i + 2], 16) for i in (1, 3, 5))
    return 0.299 * red + 0.587 * green + 0.114 * blue < 150


def caption_html(handle: str, text: str, cutoff: int = MORE_CUTOFF) -> str:
    """One caption with native more/less expansion and per-paragraph direction."""

    def paragraphs(value: str) -> str:
        return "".join(
            f'<div dir="{direction(part)}" style="text-align:start;unicode-bidi:plaintext;">'
            f"{escape(part).replace(chr(10), '<br>')}</div>"
            for part in re.split(r"\n\s*\n", value.strip())
            if part.strip()
        )

    if not text.strip():
        return '<span class="ig-hint">Your caption appears here.</span>'
    name = f'<div class="ig-name" dir="ltr">{escape(handle)}</div>'
    shown, cut = cut_at_more(text, cutoff)
    if not cut:
        return name + paragraphs(text)
    return (
        "<style>.rawaj-caption summary{cursor:pointer;list-style:none;}"
        ".rawaj-caption summary::-webkit-details-marker{display:none;}"
        ".rawaj-caption .caption-less{display:none;}"
        ".rawaj-caption[open] .caption-short{display:none;}"
        ".rawaj-caption[open] .caption-less{display:inline;}"
        ".rawaj-caption{overflow-wrap:anywhere;}</style>"
        '<details class="rawaj-caption"><summary>'
        f'<span class="caption-short">{name}{paragraphs(shown)}'
        '<span class="ig-more">… more</span></span>'
        '<span class="caption-less ig-more">less</span></summary>'
        f'<div class="caption-full">{name}{paragraphs(text)}</div></details>'
    )


def preview_html(
    *,
    handle: str,
    place: str,
    name: str,
    caption: str,
    hashtags: list[str],
    overlay: str,
    image: str | None,
    video: str | None,
    content_format: str,
    colors: list[str],
    cutoff: int = MORE_CUTOFF,
    position: int | None = None,
    total: int | None = None,
) -> str:
    """Display a lightweight Instagram-style preview for the selected asset/slide.

    ``video`` is a data URI for an uploaded video; it plays inside the media frame.
    """
    vertical = _vertical_format(content_format)
    text = caption.strip()
    if hashtags:
        text += ("\n\n" if text else "") + hashtag_text(hashtags)

    first, second = ([c for c in colors if _HEX.match(c)] + ["#EAF3FC", "#D3E4F6"])[:2]
    background = (
        f"background-image:url(data:image/jpeg;base64,{image})"
        if image
        else f"background:linear-gradient(160deg,{first},{second})"
    )

    inside = ""
    if video:
        inside += f'<video class="ig-video" src="{escape(video)}" autoplay muted loop playsinline controls></video>'
    if position and total and total > 1:
        inside += f'<div class="ig-count">{position}/{total}</div>'
    if overlay.strip():
        dark = image is not None or video is not None or _is_dark(first)
        inside += f'<div class="ig-ov{"" if dark else " on-light"}" dir="auto">{escape(overlay.strip())}</div>'
    if not image and not video:
        noun = "video" if vertical else "photo"
        inside += f'<div class="ig-empty">{icon("camera", 22)}<span>Your {noun} goes here</span></div>'

    body = caption_html(handle, text, cutoff)

    return f"""
    <div class="ig{" tall" if vertical else ""}">
      <div class="ig-head"><span class="ig-av">{escape(name[:1])}</span>
        <div><b>{escape(handle)}</b><small>{escape(place)}</small></div><span class="ig-dots">···</span></div>
      <div class="ig-media" style="{background}">{inside}</div>
      <div class="ig-actions">{icon("heart", 22)}{icon("message", 22)}{icon("send", 22)}<span class="sp"></span>{icon("bookmark", 22)}</div>
      <div class="ig-cap">{body}</div>
    </div>
    """

def carousel_html(
    *,
    uid: str,
    handle: str,
    place: str,
    name: str,
    caption: str,
    hashtags: list[str],
    slides: list[dict],
    colors: list[str],
    cutoff: int = MORE_CUTOFF,
) -> str:
    """An Instagram-style carousel: arrows and dots move between slides, as on Instagram.

    ``slides`` holds ``{"image": base64 JPEG or None, "overlay": str}`` per slide. It is pure CSS (hidden radio
    buttons and labels), because st.html runs no scripts. A rerun of the page starts again at the first slide.
    """
    uid = re.sub(r"\W", "", uid) or "c"
    count = len(slides)
    text = caption.strip()
    if hashtags:
        text += ("\n\n" if text else "") + hashtag_text(hashtags)
    first, second = ([c for c in colors if _HEX.match(c)] + ["#EAF3FC", "#D3E4F6"])[:2]

    radios, cells, dots, rules = [], [], [], []
    for i, slide in enumerate(slides):
        rid = f"car{uid}_{i}"
        radios.append(
            f'<input class="ig-r" type="radio" name="car{uid}" id="{rid}"{" checked" if i == 0 else ""}>'
        )
        rules.append(
            f"#{rid}:checked ~ .ig-media .ig-track {{ transform: translateX(-{i * 100}%); }}"
            f"#{rid}:checked ~ .ig-actions label[for={rid}] {{ background: #3897f0; }}"
        )
        image = slide.get("image")
        style = (
            f"background-image:url(data:image/jpeg;base64,{image})"
            if image
            else f"background:linear-gradient(160deg,{first},{second})"
        )
        inside = f'<div class="ig-count">{i + 1}/{count}</div>'
        overlay = (slide.get("overlay") or "").strip()
        if overlay:
            dark = image is not None or _is_dark(first)
            inside += f'<div class="ig-ov{"" if dark else " on-light"}" dir="auto">{escape(overlay)}</div>'
        if not image:
            inside += f'<div class="ig-empty">{icon("camera", 22)}<span>Photo {i + 1} goes here</span></div>'
        if i > 0:
            inside += f'<label class="ig-arrow prev" for="car{uid}_{i - 1}" title="Previous">‹</label>'
        if i < count - 1:
            inside += f'<label class="ig-arrow next" for="car{uid}_{i + 1}" title="Next">›</label>'
        cells.append(f'<div class="ig-slide" style="{style}">{inside}</div>')
        dots.append(f'<label class="ig-dot" for="{rid}"></label>')

    return f"""
    <style>{"".join(rules)}</style>
    <div class="ig carousel">
      {"".join(radios)}
      <div class="ig-head"><span class="ig-av">{escape(name[:1])}</span>
        <div><b>{escape(handle)}</b><small>{escape(place)}</small></div><span class="ig-dots">···</span></div>
      <div class="ig-media"><div class="ig-track">{"".join(cells)}</div></div>
      <div class="ig-actions">{icon("heart", 22)}{icon("message", 22)}{icon("send", 22)}
        <span class="sp ig-dotrow">{"".join(dots)}</span>{icon("bookmark", 22)}</div>
      <div class="ig-cap">{caption_html(handle, text, cutoff)}</div>
    </div>
    """


# Progress bar
def steps_html(done: list[bool], confirmed: bool = False) -> str:
    """Display the three visible steps; a posted item has completed all three."""
    states = [confirmed or bool(done[i]) if i < len(done) else confirmed for i in range(len(STEPS))]
    current = next((i for i, finished in enumerate(states) if not finished), None)
    cells = []
    for i, (label, finished) in enumerate(zip(STEPS, states)):
        state = "done" if finished else "now" if i == current else ""
        mark = icon("check", 14, 2.8) if finished else str(i + 1)
        cells.append(f'<div class="step {state}"><span class="dotc">{mark}</span><b>{label}</b></div>')
    return f'<div class="steps">{"".join(cells)}</div>'
