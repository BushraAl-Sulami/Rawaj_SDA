"""Streamlit workspace: confirm facts, generate/edit a kit, preview assets, and mark posting complete."""

from __future__ import annotations

import base64
import zlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html import escape

import streamlit as st
from ui import api
from ui import post_kit as kitlib
# The workspace is drawn with st.html, which removes <svg> tags: use the mask version of the icons.
from ui.icons import html_icon as icon
from ui.plan import progress

TONE_LABELS = {code: label for label, code in kitlib.TONES.items()}


@dataclass(frozen=True)
class WorkspaceContext:
    restaurant: dict
    plan: dict
    task: dict
    idea: dict
    wid: str
    restaurant_name: str
    when: str


# Session helpers


def html(markup: str) -> None:
    """Display trusted templates; escape dynamic text at the call site."""
    st.html(markup)


def keep(key: str, value) -> None:
    """Initialize a widget once; Streamlit removes widget values when pages change."""
    if key not in st.session_state:
        st.session_state[key] = value


def chip(state: str, text: str) -> str:
    symbol = {"ok": "check", "na": "info"}.get(state, "alert")
    return f'<span class="chk {state}">{icon(symbol, 13, 2.2)}{escape(text)}</span>'


def _workspace_id(restaurant: dict, task: dict, idea: dict) -> str:
    idea_key = zlib.crc32(str(idea.get("name", "idea")).encode("utf-8"))
    return f"{restaurant['id']}_{task['id']}_{idea_key}"


def _new_workspace(restaurant: dict) -> dict:
    return {
        "facts": kitlib.default_facts(restaurant),
        "facts_plan": None,
        "seed_rows": [],
        "row_count": 0,
        "resp": None,
        "generated_for": None,
        "caption": "",
        "tone": "warm",
        "overlay": "",
        "checked": {},
        "assets": {},
        "upload_generation": 0,
        "scheduled": False,
        "before": None,
        "error": "",
    }


def _get_workspace(ctx: WorkspaceContext) -> dict:
    workspaces = st.session_state.setdefault("post_workspaces", {})
    if ctx.wid not in workspaces:
        workspaces[ctx.wid] = _new_workspace(ctx.restaurant)
    return workspaces[ctx.wid]


def _fact_key(ctx: WorkspaceContext, field: str) -> str:
    return f"fact_{ctx.wid}_{field}"


def _gap(ctx: WorkspaceContext) -> dict | None:
    return kitlib.pick_gap(ctx.plan, ctx.task, ctx.idea)


def _current_signature(ws: dict) -> str:
    return kitlib.facts_signature(ws["facts"])


def _kit_is_stale(ws: dict) -> bool:
    return bool(ws.get("resp") and ws.get("generated_for") != _current_signature(ws))


# API actions


def _ensure_facts_plan(ctx: WorkspaceContext, ws: dict) -> dict:
    if ws["facts_plan"] is not None:
        return ws["facts_plan"]

    try:
        with st.spinner("Reading the idea to see what needs to be confirmed..."):
            ws["facts_plan"] = api.facts_plan(
                ctx.restaurant["id"], ctx.task["day"], ctx.idea, kitlib.gap_text(_gap(ctx))
            )
    except api.ApiError:
        ws["facts_plan"] = kitlib.generic_plan()

    ws["seed_rows"] = kitlib.starting_rows(ws["facts_plan"], ctx.restaurant)
    ws["row_count"] = len(ws["seed_rows"])
    return ws["facts_plan"]


def _generate_kit(ctx: WorkspaceContext, ws: dict) -> bool:
    try:
        with st.spinner("Building the posting guide and checking every claim..."):
            response = api.create_post_kit(
                ctx.restaurant["id"],
                ctx.task["day"],
                ctx.idea,
                ws["facts"],
                ws["tone"],
                kitlib.gap_text(_gap(ctx)),
            )
    except api.ApiError as exc:
        ws["error"] = exc.detail or str(exc)
        return False

    ws["resp"] = response
    ws["generated_for"] = _current_signature(ws)
    ws["kit_has_photo"] = bool(ws["facts"].get("has_photo"))  # the guide's labels follow the answer it was made for
    ws["error"] = ""
    ws["checked"] = {}

    fresh = response["kit"]
    ws["caption"] = fresh["caption"]["text"]
    ws["overlay"] = fresh["visual"].get("text_overlay") or ""
    st.session_state[f"cap_{ctx.wid}"] = ws["caption"]

    for key in list(st.session_state):
        if str(key).startswith(f"chk_{ctx.wid}_"):
            del st.session_state[key]
    return True


def _rewrite_caption(ctx: WorkspaceContext, ws: dict, change: str, claims: list[str] | None = None) -> bool:
    current = st.session_state.get(f"cap_{ctx.wid}", ws["caption"])
    try:
        with st.spinner("Rewriting your caption..."):
            rewritten = api.rewrite_caption(
                ctx.restaurant["id"],
                ctx.task["day"],
                current,
                change,
                ws["facts"],
                ctx.idea["content_format"],
                claims,
            )
    except api.ApiError as exc:
        st.error(exc.detail or str(exc))
        return False

    ws["caption"] = rewritten["text"]
    st.session_state[f"cap_{ctx.wid}"] = rewritten["text"]
    return True


# Header cards


def _show_post_progress(ctx: WorkspaceContext, ws: dict, posted: bool) -> None:
    done = [
        True,
        ws.get("resp") is not None and not _kit_is_stale(ws),
        ws.get("scheduled", False) or posted,
    ]
    with st.container(key="card_steps"):
        html(f"""
            <div class="card-head"><h3 class="card-title">Your post, step by step</h3>
            <span class="muted">{escape(ctx.idea["name"])}</span></div>
            {kitlib.steps_html(done, confirmed=posted)}
            <p class="steps-note">Rawaj prepares the content and guide; you still publish it in Instagram.
            When the post is live, “Mark as posted” completes the calendar day.</p>
            """)


def _show_post_reason(ctx: WorkspaceContext, ws: dict) -> None:
    gap = _gap(ctx)
    if gap:
        detail = (
            f" · {escape(gap['highlight_label'] or 'Evidence')}: {escape(gap['highlight'])}"
            if gap.get("highlight")
            else ""
        )
        reason = (
            f"This post answers a gap Rawaj found: <b>{escape(gap['gap'])}</b> "
            f"({escape(gap.get('severity') or 'unrated')}{detail}). "
            f"{escape(gap.get('key_point') or '')}"
        )
    else:
        targets = ctx.plan.get("targets") or []
        goal = targets[0] if targets else ctx.plan.get("focus", "your content plan")
        reason = f"This post supports your plan: <b>{escape(str(goal))}</b>."

    answers = ""
    resp = ws.get("resp")
    if resp and not _kit_is_stale(ws):
        caption = st.session_state.get(f"cap_{ctx.wid}", ws["caption"])
        fixes = kitlib.gap_fixes(gap, resp, caption, ws["facts"], resp.get("more_cutoff", kitlib.MORE_CUTOFF))
        if fixes:
            answers = (
                '<div class="eyebrow" style="margin-top:.7rem;">How this post answers it</div>'
                '<div class="fixes">' + "".join(chip(state, text) for state, text in fixes) + "</div>"
            )
    elif gap:
        answers = '<p class="hint" style="margin:.4rem 0 0;">Create the posting guide to see how this post answers it.</p>'

    with st.container(key="card_why"):
        html(f"""
            <div class="why"><div class="badge">{icon("target", 20)}</div>
              <div><div class="eyebrow">Why this post</div><p>{reason}</p>{answers}</div></div>
            """)


# Facts form


def _show_item_rows(ctx: WorkspaceContext, ws: dict, plan_facts: dict, language: str) -> list[dict]:
    if not plan_facts.get("items_max"):
        return []

    html(f'<div class="dl"><small>{escape(plan_facts["items_label"])}</small></div>')

    items: list[dict] = []
    groups = list(plan_facts.get("suggested_groups") or [])

    for i in range(ws["row_count"]):
        saved_items = ws["facts"].get("items") or ws["seed_rows"]
        seed = saved_items[i] if i < len(saved_items) else {}
        for field in ("name_en", "name_ar", "price", "group"):
            keep(_fact_key(ctx, f"{field}{i}"), seed.get(field, ""))

        group = str(st.session_state[_fact_key(ctx, f"group{i}")] or "").strip()
        if plan_facts.get("wants_groups"):
            if group:
                html(
                    f'<div class="hook-card" style="margin:.65rem 0 .35rem;"><small>Moment {i + 1}</small><b>{escape(group)}</b></div>'
                )
            elif groups:
                group = st.selectbox(
                    "Moment",
                    groups,
                    key=_fact_key(ctx, f"group_select{i}"),
                )
                st.session_state[_fact_key(ctx, f"group{i}")] = group

        columns = []
        if language in {"English", "Bilingual"}:
            columns.append("en")
        if language in {"Arabic", "Bilingual"}:
            columns.append("ar")
        if plan_facts.get("wants_prices"):
            columns.append("price")

        widths = [1.5 if field != "price" else 0.8 for field in columns]
        cells = iter(st.columns(widths or [1]))

        name_en = str(st.session_state[_fact_key(ctx, f"name_en{i}")] or "")
        name_ar = str(st.session_state[_fact_key(ctx, f"name_ar{i}")] or "")
        price = str(st.session_state[_fact_key(ctx, f"price{i}")] or "")

        if "en" in columns:
            name_en = next(cells).text_input(
                "Item name (English)",
                key=_fact_key(ctx, f"name_en{i}"),
                max_chars=120,
                placeholder="e.g. Spanish Latte",
            )
        if "ar" in columns:
            name_ar = next(cells).text_input(
                "اسم الصنف (العربية)",
                key=_fact_key(ctx, f"name_ar{i}"),
                max_chars=120,
                placeholder="مثال: سبانش لاتيه",
            )
        if "price" in columns:
            price = next(cells).text_input(
                "Price",
                key=_fact_key(ctx, f"price{i}"),
                max_chars=40,
                placeholder="e.g. 24 SAR",
            )

        items.append(
            {
                "name_en": name_en.strip(),
                "name_ar": name_ar.strip(),
                "price": price.strip(),
                "group": group,
            }
        )

    add_col, drop_col, _ = st.columns([1, 1, 2.4])
    if add_col.button(
        "Add another",
        icon=":material/add:",
        type="tertiary",
        key=f"add_{ctx.wid}",
        disabled=ws["row_count"] >= int(plan_facts["items_max"]),
    ):
        ws["row_count"] += 1
        st.rerun()

    minimum_rows = max(int(plan_facts.get("items_min") or 0), len(groups) if plan_facts.get("wants_groups") else 0)
    if drop_col.button(
        "Remove last",
        icon=":material/remove:",
        type="tertiary",
        key=f"drop_{ctx.wid}",
        disabled=ws["row_count"] <= minimum_rows,
    ):
        ws["row_count"] -= 1
        st.rerun()

    return items


def _show_facts(ctx: WorkspaceContext, ws: dict) -> None:
    plan_facts = _ensure_facts_plan(ctx, ws)
    facts = ws["facts"]

    with st.container(key="card_facts"):
        html(f"""
            <div class="eyebrow" style="color:var(--blue);">Confirm only what Rawaj cannot safely know</div>
            <h2 style="font:600 22px var(--head); margin:.4rem 0 .3rem;">Make this idea real.</h2>
            <p class="muted" style="margin:0 0 .8rem;">{escape(plan_facts["summary"])}
            Rawaj will create the hook, slide order, visual direction, caption and CTA.</p>
            """)

        keep(_fact_key(ctx, "language"), facts["language"])
        language = st.radio(
            "Caption language",
            kitlib.LANGUAGES,
            horizontal=True,
            key=_fact_key(ctx, "language"),
        )

        items = _show_item_rows(ctx, ws, plan_facts, language)

        channels: list[str] = []
        if plan_facts.get("wants_channels"):
            initial_channels = facts["channels"] or plan_facts.get("suggested_channels") or []
            keep(_fact_key(ctx, "channels"), initial_channels)
            options = kitlib.DELIVERY_CHOICES + [
                c for c in plan_facts.get("suggested_channels") or [] if c not in kitlib.DELIVERY_CHOICES
            ]
            channels = st.multiselect(
                "How can guests get it?",
                options,
                key=_fact_key(ctx, "channels"),
                accept_new_options=True,
                placeholder="Choose only the ways that are true",
            )

        offer = ""
        if plan_facts.get("wants_offer"):
            keep(_fact_key(ctx, "offer"), facts["offer"])
            offer = st.text_input(
                "Confirmed offer",
                key=_fact_key(ctx, "offer"),
                max_chars=200,
                placeholder="Only if this idea depends on a real current offer",
            )

        notes = ""
        if plan_facts.get("notes_prompt"):
            keep(_fact_key(ctx, "notes"), facts["notes"])
            notes = st.text_area(
                plan_facts["notes_prompt"],
                key=_fact_key(ctx, "notes"),
                max_chars=400,
                height=80,
                placeholder="e.g. slow-cooked lamb on saffron rice, serves four. Only what is true; leave empty if unsure.",
            )

        keep(_fact_key(ctx, "photo"), "Yes" if facts["has_photo"] else "No")
        has_photo = st.radio(
            "Do you already have photos/video you may use?",
            ["Yes", "No"],
            horizontal=True,
            key=_fact_key(ctx, "photo"),
        )

        ws["facts"] = {
            "items": items,
            "channels": channels,
            "offer": offer.strip(),
            "notes": notes.strip(),
            "language": language,
            "has_photo": has_photo == "Yes",
            "brand_colors": facts.get("brand_colors") or [],
        }

        missing = kitlib.missing_item_count(plan_facts, items)
        if language == "Bilingual":
            incomplete = [
                i
                for i in items
                if (i.get("name_en") or i.get("name_ar")) and not (i.get("name_en") and i.get("name_ar"))
            ]
            if incomplete:
                html(
                    '<p class="hint">For a bilingual caption, add both Arabic and English names when you need the item named in both languages.</p>'
                )
        if missing:
            html(
                f'<p class="hint">Add {missing} more confirmed item{"s" if missing != 1 else ""} to complete this idea.</p>'
            )

        if plan_facts.get("fallback"):
            html('<p class="hint">Rawaj could not make an idea-specific form, so this is the safe fallback.</p>')
            if st.button("Read the idea again", type="tertiary", key=f"replan_{ctx.wid}", icon=":material/refresh:"):
                ws["facts_plan"] = None
                ws["seed_rows"] = []
                ws["row_count"] = 0
                st.rerun()

        stale = _kit_is_stale(ws)
        if stale:
            st.warning(
                "You changed the confirmed facts. Update the posting guide before using the old caption or instructions."
            )

        action, note = st.columns([1.25, 2.1], vertical_alignment="center")
        label = "Update posting guide" if ws.get("resp") else "Create posting guide"
        if action.button(
            label,
            icon=":material/auto_awesome:",
            type="primary",
            key=f"generate_{ctx.wid}",
            disabled=missing > 0,
        ):
            if _generate_kit(ctx, ws):
                st.rerun()

        note.markdown(
            '<span class="muted" style="font-size:10.5px;">Rawaj chooses the execution format and tells you exactly what to create.</span>',
            unsafe_allow_html=True,
        )
        if ws["error"]:
            st.error(ws["error"])


# Generated kit


def _show_kit_notes(ctx: WorkspaceContext, ws: dict, resp: dict) -> None:
    """Rawaj's own fact-check, shown as what it is: every rule and claim checked, and what still needs the owner."""
    caption = st.session_state.get(f"cap_{ctx.wid}", ws["caption"])
    checks = resp.get("checks") or []
    passed = sum(1 for check in checks if check.get("ok"))
    flagged = resp.get("unsupported_claims") or []
    still = kitlib.open_claims(resp, caption)
    failing = [check["message"] for check in checks if not check.get("ok")]
    look = len(still) + len(failing)

    if resp.get("claims_checked", False):
        summary = f"Rawaj checked {passed} of {len(checks)} rules and every public claim against your confirmed facts"
    else:
        summary = f"Rawaj checked {passed} of {len(checks)} rules. The claim checker was unavailable: read the copy against your facts"
    summary += f" · {look} need{'s' if look == 1 else ''} your look." if look else " · all clear."
    html(
        f'<div class="flag {"good" if not look else "note"}">{icon("shield" if not look else "info", 15)}'
        f"<span><b>{escape(summary)}</b></span></div>"
    )

    for claim in still:
        html(
            f'<div class="flag">{icon("alert", 15)}<span><b>Needs your confirmation:</b> “{escape(claim["text"])}”. '
            f"{escape(claim['reason'])}</span></div>"
        )
    in_caption = [claim["text"] for claim in still if claim["text"].strip().casefold() in caption.casefold()]
    if in_caption and st.button(
        "Rewrite the caption without " + ("it" if len(in_caption) == 1 else "them"),
        icon=":material/auto_fix_high:",
        type="secondary",
        key=f"fixclaims_{ctx.wid}",
    ):
        if _rewrite_caption(ctx, ws, "claims", in_caption):
            st.rerun()
    for claim in flagged:
        if claim not in still:
            html(
                f'<div class="flag good">{icon("check", 15, 2.4)}<span>Removed from the caption: “{escape(claim["text"])}”.</span></div>'
            )
    for text in failing:
        html(f'<div class="flag">{icon("alert", 15)}<span>{escape(text)}</span></div>')


def _show_execution_summary(resp: dict) -> None:
    kit = resp["kit"]
    execution = kit.get("execution") or {}
    shoot = kit["shoot"]
    html(f"""
        <div class="readout">
          <small>Recommended execution · {escape(kitlib.format_name(shoot["format"]))}</small>
          <b>{escape(execution.get("what_to_make") or "")}</b>
          <span>{escape(shoot.get("format_reason") or "")}</span>
        </div>
        <div class="readout" style="margin-top:.55rem;">
          <small>Start here</small>
          {escape(execution.get("owner_action") or "")}
        </div>
        """)


def _change_tone(ctx: WorkspaceContext, ws: dict) -> None:
    key = f"tone_{ctx.wid}"
    selected = st.session_state.get(key)
    if selected and kitlib.TONES[selected] != ws["tone"]:
        if _rewrite_caption(ctx, ws, kitlib.TONES[selected]):
            ws["tone"] = kitlib.TONES[selected]
        else:
            st.session_state[key] = TONE_LABELS[ws["tone"]]


def _change_caption(ctx: WorkspaceContext, ws: dict) -> None:
    key = f"rw_{ctx.wid}"
    selected = st.session_state.get(key)
    st.session_state[key] = None
    if selected:
        _rewrite_caption(ctx, ws, kitlib.CHIPS[selected])


def _show_caption_tab(ctx: WorkspaceContext, ws: dict, resp: dict) -> None:
    kit = resp["kit"]
    cutoff = resp.get("more_cutoff", kitlib.MORE_CUTOFF)

    keep(f"tone_{ctx.wid}", TONE_LABELS[ws["tone"]])
    html('<div class="dl"><small>Choose a tone</small></div>')
    st.segmented_control(
        "Tone",
        list(kitlib.TONES),
        key=f"tone_{ctx.wid}",
        label_visibility="collapsed",
        on_change=_change_tone,
        args=(ctx, ws),
    )
    html(f'<p class="hint" style="margin-top:.2rem;">{escape(kitlib.TONE_NOTES[ws["tone"]])}</p>')

    keep(f"cap_{ctx.wid}", ws["caption"])
    length = len(st.session_state[f"cap_{ctx.wid}"])
    html(f'<div class="cap-head"><b>Edit caption</b><span>{length} characters</span></div>')
    text = st.text_area(
        "Edit caption",
        key=f"cap_{ctx.wid}",
        height=200,
        label_visibility="collapsed",
    )
    ws["caption"] = text
    state, message = kitlib.subject_check(
        text,
        ws["facts"],
        cutoff,
        ctx.idea["content_format"] == "Story",
    )
    html(chip(state, message))
    detail = kitlib.details_check(text, resp.get("detail_words") or [])
    if detail:
        html(chip(*detail))

    st.pills(
        "Change one thing",
        list(kitlib.CHIPS),
        selection_mode="single",
        key=f"rw_{ctx.wid}",
        on_change=_change_caption,
        args=(ctx, ws),
    )
    html('<div class="dl" style="margin-top:.8rem;"><small>Hashtags</small></div>')
    html(
        '<div class="tags">'
        + "".join(f'<span class="tag" dir="auto">{escape(tag)}</span>' for tag in kit["hashtags"])
        + "</div>"
    )
    st.code(kitlib.hashtag_text(kit["hashtags"]), language=None)


def _show_execution_tab(ctx: WorkspaceContext, ws: dict, resp: dict) -> None:
    guide = resp["kit"]["shoot"]
    existing = ws.get("kit_has_photo", False)
    duration = f" · {guide['duration_seconds']} seconds" if guide.get("duration_seconds") else ""
    source = "Using your photos" if existing else "New shoot"
    html(
        f'<span class="chip">{icon("video" if guide["format"] in ("reel", "story") else "camera", 13)}'
        f"{escape(kitlib.format_name(guide['format']))}{duration} · {source}</span>"
    )
    html(
        f'<p class="hint" style="margin-top:.5rem;"><b>Why this format:</b> {escape(guide.get("format_reason") or "")}</p>'
    )
    if guide.get("hook"):
        html(
            f'<div class="hook-card" style="margin-top:.8rem;"><small>Hook · first two seconds</small><b>{escape(guide["hook"])}</b></div>'
        )

    cards = "".join(
        kitlib.shot_card(number, shot, existing) for number, shot in enumerate(guide.get("shots") or [], 1)
    )
    html(f'<div style="margin-top:.8rem;">{cards}</div>')

    html(f'<div class="dl"><small>{"Before you post" if existing else "Before you shoot"}</small></div>')
    for i, item in enumerate(guide.get("checklist") or []):
        keep(f"chk_{ctx.wid}_{i}", ws["checked"].get(i, False))
        ws["checked"][i] = st.checkbox(item, key=f"chk_{ctx.wid}_{i}")


def _show_publish_tab(ctx: WorkspaceContext, resp: dict) -> None:
    kit = resp["kit"]
    headline, detail, text = kitlib.best_time_summary(
        resp["best_time"],
        ctx.when,
        passed=ctx.task["date"] < datetime.now(timezone(timedelta(hours=3))).date(),
    )
    html(f"""
        <div class="time">{icon("clock", 26)}<div>
          <div class="big">{escape(headline)}</div><p><b>{escape(detail)}</b></p><p>{escape(text)}</p></div></div>
        """)
    html('<div class="dl" style="margin-top:1rem;"><small>Location tag</small></div>')
    st.code(kit["location_tag"], language=None)

    if kit.get("mentions"):
        rows = "".join(
            f"<li><b>{escape(m['handle'] and '@' + m['handle'].lstrip('@') or m['who'])}</b> · {escape(m['why'])}"
            f"{'' if m['handle'] else ' (add their handle)'}</li>"
            for m in kit["mentions"]
        )
        html(
            f'<div class="dl" style="margin-top:.6rem;"><small>Suggested mentions</small><ul class="ev">{rows}</ul></div>'
        )

    html('<div class="dl" style="margin-top:.6rem;"><small>Follow-up ideas</small></div>')
    for item in kit.get("follow_ups") or []:
        options = (
            "".join(f'<div class="opt" dir="auto">{escape(option)}</div>' for option in item.get("options") or [])
            or '<div class="opt">Type something…</div>'
        )
        sticker = (
            f'<div class="poll"><div class="q" dir="auto">{escape(item.get("sticker_text") or "")}</div>{options}</div>'
            if item.get("sticker") in {"poll", "quiz", "question"} and item.get("sticker_text")
            else ""
        )
        html(
            f'<div class="fu"><div><b>{escape(item["format"])} · {escape(item["title"])}</b>'
            f'<p>{escape(item["description"])}</p><p><b style="font-size:11px;">{escape(item["timing"])}</b></p>'
            f"</div><div>{sticker}</div></div>"
        )


def _show_kit(ctx: WorkspaceContext, ws: dict) -> None:
    resp = ws["resp"]
    if not resp:
        return

    with st.container(key="card_kit"):
        html("""
            <div class="eyebrow" style="color:var(--blue);">Your posting guide</div>
            <h2 style="font:600 22px var(--head); margin:.4rem 0 .6rem;">What to post, and exactly how to make it.</h2>
            """)
        _show_kit_notes(ctx, ws, resp)
        _show_execution_summary(resp)

        tab_caption, tab_execute, tab_publish = st.tabs(["Caption", "Create it", "Publish"])
        with tab_caption:
            _show_caption_tab(ctx, ws, resp)
        with tab_execute:
            _show_execution_tab(ctx, ws, resp)
        with tab_publish:
            _show_publish_tab(ctx, resp)


# Preview and media uploads


def _asset_slots(resp: dict | None) -> list[tuple[str, str, str]]:
    """Return (storage_key, label, accepted_kind) for content the owner may upload."""
    if not resp:
        return [("final", "Post asset", "image_or_video")]

    shoot = resp["kit"]["shoot"]
    fmt = shoot["format"]
    shots = shoot.get("shots") or []
    if fmt == "carousel":
        return [(str(i), shot["title"], "image") for i, shot in enumerate(shots)]
    if fmt == "story":
        return [(str(i), shot["title"], "image_or_video") for i, shot in enumerate(shots)]
    if fmt == "reel":
        return [("final", "Final edited Reel", "video")]
    return [("final", "Final post image", "image")]


def _read_upload(upload, content_format: str) -> dict:
    data = upload.getvalue()
    is_video = (upload.type or "").startswith("video/") or upload.name.lower().endswith((".mp4", ".mov"))
    if is_video:
        return {"name": upload.name, "kind": "video", "bytes": data}

    check = kitlib.photo_check(data, content_format)
    return {
        "name": upload.name,
        "kind": "image",
        "bytes": data,
        "check": check,
        "preview": check["preview"],
    }


def _show_preview(ctx: WorkspaceContext, ws: dict) -> None:
    resp = ws.get("resp")
    kit = resp["kit"] if resp else None
    actual_format = kitlib.execution_format(resp, ctx.idea["content_format"])
    cutoff = resp.get("more_cutoff", kitlib.MORE_CUTOFF) if resp else kitlib.MORE_CUTOFF
    slots = _asset_slots(resp)

    if actual_format == "carousel":
        # Every slide in one Instagram-style frame: arrows and dots, each slide with its photo and on-screen text.
        shots = kit["shoot"].get("shots") or []
        slides = []
        for i, (storage_key, _, _) in enumerate(slots):
            media = ws["assets"].get(storage_key)
            overlay = (shots[i].get("overlay_text") or "") if i < len(shots) else ""
            if i == 0 and ws["overlay"]:
                overlay = ws["overlay"]
            slides.append({"image": media.get("preview") if media else None, "overlay": overlay})
        st.html(
            kitlib.carousel_html(
                uid=ctx.wid,
                handle=ctx.restaurant.get("instagram_username") or ctx.restaurant_name,
                place=ctx.restaurant.get("location") or "",
                name=ctx.restaurant_name,
                caption=ws["caption"],
                hashtags=kit.get("hashtags", []),
                slides=slides,
                colors=ws["facts"].get("brand_colors") or [],
                cutoff=cutoff,
            )
        )
        html('<p class="hint" style="text-align:center;">Use the arrows or dots to swipe through the slides.</p>')
        return

    preview_index = 0
    if len(slots) > 1:
        preview_index = st.selectbox(
            "Preview",
            list(range(len(slots))),
            format_func=lambda i: f"{i + 1}. {slots[i][1]}",
            key=f"preview_asset_{ctx.wid}",
            label_visibility="collapsed",
        )

    storage_key = slots[preview_index][0]
    media = ws["assets"].get(storage_key)
    video = None
    if media and media["kind"] == "video":
        if "data_uri" not in media:
            # .mov files are usually H.264 too; labelling them mp4 lets Chrome play them.
            media["data_uri"] = "data:video/mp4;base64," + base64.b64encode(media["bytes"]).decode("ascii")
        video = media["data_uri"]
    overlay = ws["overlay"]
    shots = kit["shoot"].get("shots") or [] if kit else []
    if preview_index < len(shots) and (preview_index > 0 or not overlay):
        overlay = shots[preview_index].get("overlay_text") or overlay

    preview = kitlib.preview_html(
        handle=ctx.restaurant.get("instagram_username") or ctx.restaurant_name,
        place=ctx.restaurant.get("location") or "",
        name=ctx.restaurant_name,
        caption=ws["caption"],
        hashtags=kit.get("hashtags", []) if kit else [],
        overlay=overlay,
        image=media.get("preview") if media and media["kind"] == "image" else None,
        video=video,
        content_format=actual_format,
        colors=ws["facts"].get("brand_colors") or [],
        cutoff=cutoff,
        position=preview_index + 1 if len(slots) > 1 else None,
        total=len(slots) if len(slots) > 1 else None,
    )
    st.html(preview)


def _read_cached(ws: dict, upload, content_format: str) -> dict:
    """Read each uploaded file once; Streamlit hands the same files back on every rerun."""
    cache = ws.setdefault("upload_cache", {})
    key = f"{upload.file_id}:{content_format}"
    if key not in cache:
        cache[key] = _read_upload(upload, content_format)
    return cache[key]


def _show_uploads(ctx: WorkspaceContext, ws: dict) -> None:
    """One upload box for every asset the post needs; files go to slides in the order they were added."""
    resp = ws.get("resp")
    actual_format = kitlib.execution_format(resp, ctx.idea["content_format"])
    slots = _asset_slots(resp)

    if not resp:
        html(
            '<p class="hint">Create the posting guide first. Rawaj will then tell you how many images or videos the idea needs.</p>'
        )
        return

    many = len(slots) > 1
    html(
        f'<p class="hint">{len(slots)} asset{"s" if many else ""} for this {escape(kitlib.format_name(actual_format).lower())}. '
        f'{"Add them all here, in slide order. " if many else ""}Uploading is optional; it fills the preview and checks each photo.</p>'
    )
    extensions = {
        "image": ["png", "jpg", "jpeg", "webp"],
        "video": ["mp4", "mov"],
        "image_or_video": ["png", "jpg", "jpeg", "webp", "mp4", "mov"],
    }
    types = sorted({ext for _, _, kind in slots for ext in extensions[kind]})
    uploaded = st.file_uploader(
        "Your photos" if many else slots[0][1],
        type=types,
        accept_multiple_files=many,
        key=f"asset_up_{ctx.wid}_{ws['upload_generation']}",
        label_visibility="collapsed",
    )
    files = [f for f in (uploaded if many else [uploaded]) if f is not None]
    names = [f.name for f in files]

    assets: dict[str, dict] = {}
    for i, (storage_key, label, _) in enumerate(slots):
        choice = i
        if many and len(files) > 1 and i < len(files):
            # A slide's picker swaps files when they were added in another order.
            choice = st.selectbox(
                f"Slide {i + 1} · {label}",
                list(range(len(files))),
                index=i,
                format_func=lambda j: names[j],
                key=f"asset_pick_{ctx.wid}_{ws['upload_generation']}_{i}_{len(files)}",
            )
        else:
            html(f'<div class="dl"><small>{f"Slide {i + 1} · " if many else ""}{escape(label)}</small></div>')

        if choice >= len(files):
            html('<p class="hint" style="margin:.1rem 0 .35rem;">Not added yet.</p>')
            continue
        try:
            media = _read_cached(ws, files[choice], actual_format)
        except ValueError as exc:
            st.error(f"{files[choice].name}: {exc}")
            continue
        assets[storage_key] = media
        if media["kind"] == "video":
            st.video(media["bytes"])
            html('<p class="hint">Video is previewed but not visually evaluated.</p>')
        else:
            levels = {"ok": ("good", "check"), "warn": ("limit", "alert"), "bad": ("bad", "alert")}
            rows = "".join(
                f'<div class="note {levels[level][0]}"><span class="dot">{icon(levels[level][1], 13, 2.4)}</span><span>{escape(text)}</span></div>'
                for level, text in media["check"]["items"]
            )
            html(f'<div class="notes one">{rows}</div>')

    if len(files) > len(slots):
        html(f'<p class="hint">Only the first {len(slots)} files are used: this post has {len(slots)} slides.</p>')
    ws["assets"] = assets
    if files and st.button("Remove all", key=f"asset_clear_{ctx.wid}", type="tertiary", icon=":material/close:"):
        ws["assets"] = {}
        ws["upload_cache"] = {}
        ws["upload_generation"] += 1
        st.rerun()


def _show_side(ctx: WorkspaceContext, ws: dict) -> None:
    with st.container(key="side_sticky"):
        preview = None
        if str(ctx.idea.get("content_format") or "").strip().casefold() not in {"story", "stories"}:
            preview = st.container(key="card_preview")

        # Read the uploads before drawing the preview above them, so a new photo shows at once.
        with st.container(key="card_upload"):
            html(
                '<div class="card-head"><h3 class="card-title">Your content assets</h3><span class="muted">Stays in this session</span></div>'
            )
            _show_uploads(ctx, ws)

        if preview is not None:
            with preview:
                html(
                    '<div class="card-head"><h3 class="card-title">Live preview</h3><span class="muted">Updates as you edit</span></div>'
                )
                _show_preview(ctx, ws)


# Final hand-off and completion


def _set_posted(ctx: WorkspaceContext, ws: dict, completed: bool, before=None) -> None:
    try:
        api.set_day_status(ctx.restaurant["id"], ctx.task["day"], "Completed" if completed else "Planned")
    except api.ApiError as exc:
        st.session_state.plan_error = exc.detail or str(exc)
    else:
        ws["before"] = before if completed else None


def _show_posting_section(ctx: WorkspaceContext, ws: dict, stats: dict) -> None:
    resp = ws["resp"]
    if _kit_is_stale(ws):
        st.warning("Update the posting guide first. The current guide was generated from older facts.")
        return

    kit = resp["kit"]
    html("""
        <div class="eyebrow" style="color:var(--blue);">Take it to Instagram</div>
        <h2 style="font:600 22px var(--head); margin:.4rem 0 .3rem;">Everything you need is ready.</h2>
        <p class="muted" style="margin:0 0 .8rem;">Copy the caption or the full execution guide, create the post in Instagram, then mark it as posted.</p>
        """)

    keep(f"copy_{ctx.wid}", "Caption")
    what = (
        st.segmented_control(
            "Copy",
            ["Caption", "Hashtags"],
            key=f"copy_{ctx.wid}",
            label_visibility="collapsed",
        )
        or "Caption"
    )
    copy_text = {
        "Caption": lambda: ws["caption"],
        "Hashtags": lambda: kitlib.hashtag_text(kit["hashtags"]),
        
    }[what]()
    st.code(copy_text, language=None, wrap_lines=True)

    keep(f"sch_{ctx.wid}", ws["scheduled"])
    ws["scheduled"] = st.checkbox(
        "I've scheduled it in Instagram",
        key=f"sch_{ctx.wid}",
        on_change=lambda: ws.update(scheduled=st.session_state[f"sch_{ctx.wid}"]),
    )

    html(
        f'<div class="flag good">{icon("info", 15)}<span><b>You publish it yourself.</b> Rawaj prepares and checks the guide; it does not publish to Instagram yet.</span></div>'
    )
    one, _ = st.columns([1, 1.4])

    if one.button(
        "Mark as posted",
        icon=":material/check_circle:",
        type="primary",
        key=f"post_{ctx.wid}",
    ):
        _set_posted(ctx, ws, True, stats["done"])
        st.rerun()


def _show_post_completion(ctx: WorkspaceContext, ws: dict, stats: dict) -> None:
    html(f"""
        <div class="done-head"><span class="badge">{icon("check", 20, 2.6)}</span>
          <div><b>Posted.</b><span>“{escape(ctx.idea["name"])}” is marked complete on your calendar.</span></div></div>
        """)

    moved = ws["before"] is not None and ws["before"] != stats["done"]
    counted = (
        f"{ws['before']} → {stats['done']} of {stats['total']} actions"
        if moved
        else f"{stats['done']} of {stats['total']} actions completed"
    )
    html(f"""
        <div style="margin:1rem 0 .3rem;"><div class="card-head" style="margin-bottom:.4rem;"><h3 class="card-title">Task progress</h3>
        <span class="muted"><b>{counted}</b></span></div>
        <div class="bar-row"><div class="bar"><i style="width:{stats["percent"]}%"></i></div><b>{stats["percent"]}%</b></div></div>
        """)

    following = kitlib.next_task(ctx.plan, ctx.task)
    if following:
        label = " · ".join(
            part
            for part in (
                f"{following['date']:%a %d %b}",
                following["format"] if following.get("typed") else "",
                following["title"],
            )
            if part
        )
        html(
            f'<div class="readout" style="margin-top:.8rem;"><small>Next on your calendar</small><b>{escape(label)}</b></div>'
        )
    if st.button("Plan the next post", icon=":material/arrow_forward:", type="primary", key=f"next_{ctx.wid}"):
        if following:
            st.session_state.selected_task = following["id"]
        st.switch_page("views/strategy.py")

    if st.button("Undo", icon=":material/undo:", type="tertiary", key=f"undo_{ctx.wid}"):
        _set_posted(ctx, ws, False)
        st.rerun()


# Public entry point


def show_workspace(restaurant: dict, plan: dict, task: dict, idea: dict) -> None:
    """Display the complete idea: executable post workflow."""
    ctx = WorkspaceContext(
        restaurant=restaurant,
        plan=plan,
        task=task,
        idea=idea,
        wid=_workspace_id(restaurant, task, idea),
        restaurant_name=plan.get("restaurant_name") or restaurant["name"],
        when=f"{task['date']:%A %d %b}",
    )
    ws = _get_workspace(ctx)
    stats = progress(plan["tasks"])
    posted = task.get("status") == "Completed"

    _show_post_progress(ctx, ws, posted)
    _show_post_reason(ctx, ws)

    left, right = st.columns([1.55, 1], gap="medium")
    with left:
        _show_facts(ctx, ws)
        if ws.get("resp"):
            _show_kit(ctx, ws)

    with right:
        _show_side(ctx, ws)

    if ws.get("resp") or posted:
        with left:
            with st.container(key="card_done" if posted else "card_end"):
                if st.session_state.get("plan_error"):
                    st.error(st.session_state.pop("plan_error"))
                if posted:
                    _show_post_completion(ctx, ws, stats)
                else:
                    _show_posting_section(ctx, ws, stats)
