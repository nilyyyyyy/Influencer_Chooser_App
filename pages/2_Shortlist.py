import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
import pandas as pd
import streamlit as st

from core import ai, charts, components as C, config, db, scoring, tables, ui

ui.page_config("Shortlist", ":material/list_alt:")
ui.chrome()
ss = st.session_state

ui.header("Shortlist", "Hard filters remove creators that cannot be used. The rest are scored on seven criteria you can re-weight live.", step=2)
if ui.need_profile():
    st.stop()
profile = ss.profile
ui.sync_filters(profile)


@st.cache_data(show_spinner="Scoring candidates ...", max_entries=8)
def cached_scoring(settings_key: str, profile_key: str, brief_text: str, mtime: float, verdicts_key: str):
    verdicts = {int(k): v for k, v in json.loads(verdicts_key).items()}
    return scoring.run_scoring(db.as_of(), ui.settings_from_key(settings_key), json.loads(profile_key), brief_text,
                               persona_verdicts=verdicts)


# ----------------------------------------------------------------------------- active filters + editor
bar_chips, bar_btn, bar_next = st.columns([4.2, 1, 1.2], vertical_alignment="center")
with bar_btn:
    with st.popover("Edit filters", icon=":material/tune:", width="stretch"):
        st.markdown("##### Hard filters")
        f1, f2 = st.columns(2)
        with f1:
            st.multiselect("Target countries", config.COUNTRIES, key="f_countries",
                           help="A creator passes if based there, or if 35%+ of the audience is in these countries.")
            st.multiselect("Platforms", config.PLATFORMS, key="f_platforms")
            st.selectbox("Instagram format to buy", ["reel", "feed_post", "story_set"], key="f_ig_format")
            st.number_input("Budget (EUR)", min_value=0.0, step=500.0, key="f_budget")
            st.slider("Max fee per creator, share of budget", 0.05, 1.0, step=0.05, key="f_max_share", format="percent")
            st.number_input("Min followers", min_value=0, step=1000, key="f_min_followers")
            st.number_input("Max followers", min_value=0, step=10000, key="f_max_followers")
            st.number_input("Minimum engagement (%)", min_value=0.0, max_value=20.0, step=0.5, key="f_min_er",
                            help="Agency rule: creators below 2% engagement are not considered at all. 0 switches the rule off.")
            st.selectbox("Engagement measured", list(config.ER_BASIS), key="f_er_basis", format_func=config.ER_BASIS.get,
                         help="The agency divides interactions by views of organic reels. Dividing by followers gives much lower "
                              "numbers, so far more creators fall below the same threshold.")
        with f2:
            st.checkbox("Allow creators with limited availability", key="f_allow_limited")
            st.checkbox("Exclude brand conflicts (competitor promo in last 90 days, exclusivity)", key="f_excl_conflicts")
            st.checkbox("Exclude accounts with data-quality warnings", key="f_excl_warn",
                        help="Stale stats, follower spikes, fake followers, expired rate card, low confidence.")
            st.multiselect("Exclude safety flags", config.FLAG_TYPES, key="f_flags", format_func=lambda x: x.replace("_", " "))
            st.checkbox("Exclude creators whose persona clearly does not fit the brief", key="f_persona_filter",
                        help="Only 'does not fit' verdicts backed by an exact quote from the persona description exclude a creator. "
                             "'Unclear' stays in the ranking with a note.")
            st.selectbox("Exclude creators with a flag above severity", [1, 2, 3], key="f_max_sev",
                         help="3 = only the most severe flags exclude a creator. 1 = any flag above 'minor' excludes.")
            st.date_input("Campaign start", key="f_start")
            st.date_input("Campaign end", key="f_end")
            st.selectbox("Reach and conversion forecast", list(config.FORECAST_METHODS), key="f_forecast",
                         format_func=config.FORECAST_METHODS.get,
                         help="How the naive average-views forecast is corrected. The Forecast model page explains and tests each method.")

with bar_next:
    if st.button("Continue to the mix", type="primary", icon=":material/arrow_forward:", key="short_continue_top", width="stretch"):
        st.switch_page("pages/3_Campaign_Mix.py")

settings = ui.filter_settings()
with bar_chips:
    active = [(f"{', '.join(settings.countries) or 'any country'}", "primary"), (f"{len(settings.platforms)} platform{'s' if len(settings.platforms) != 1 else ''}", "outline"),
              (f"Budget EUR {settings.budget_eur:,.0f}", "outline"), (f"Fee cap {settings.max_fee_share:.0%}", "outline"),
              (f"IG {settings.ig_format.replace('_', ' ')}", "outline"), (config.FORECAST_METHODS[ss.get('f_forecast', 'forest')].split(' (')[0], "info")]
    if settings.exclude_conflicts:
        active.append(("Conflicts excluded", "outline"))
    if settings.excluded_flags:
        active.append((f"{len(settings.excluded_flags)} flag type{'s' if len(settings.excluded_flags) != 1 else ''} excluded", "danger"))
    if not settings.allow_limited:
        active.append(("Available only", "outline"))
    if settings.min_engagement_pct:
        active.append((f"Engagement ≥ {settings.min_engagement_pct:g}% {'per view' if settings.er_basis == 'view' else 'per follower'}", "outline"))
    if profile.get("creator_persona") and settings.persona_filter:
        active.append(("Persona checked", "outline"))
    C.chips(active, label="Active filters")

verdicts, persona_info = ui.persona_verdicts(profile)
pack = cached_scoring(ui.settings_key(settings), json.dumps(profile, sort_keys=True, default=str), ss.brief_text,
                      db.db_mtime(), json.dumps({str(k): v for k, v in verdicts.items()}, sort_keys=True))
scored, excluded, universe, meta = pack["scored"], pack["excluded"], pack["universe"], pack["meta"]

# ----------------------------------------------------------------------------- layout
left, right = st.columns([1, 4], gap="medium")
with left:
    with st.container(key="rail_weights", border=True):
        C.panel_title("Ranking weights")
        st.selectbox("Preset", list(config.PRESETS), key="preset", on_change=ui.set_preset)
        for c in config.CRITERIA:
            st.slider(config.CRITERIA_LABELS[c], 0, 10, key=f"w_{c}", help=config.CRITERIA_HELP[c])
        wn = scoring.normalize_weights(ui.weights())
        st.caption("Effective weights: " + ", ".join(f"{config.CRITERIA_LABELS[c].lower()} {wn[c]:.0%}" for c in config.CRITERIA))

with right:
    if scored.empty:
        C.empty_state("No creators pass the hard filters", "Relax the filters (the table below shows which rule removed how many creators).", "users")
        st.warning("No creators pass the hard filters. Relax the filters above (see reasons below).")
        if len(excluded):
            st.dataframe(excluded.exclude_reason.value_counts().rename("creators").to_frame(), width="stretch")
        st.stop()

    ranked = scoring.rank(scored, ui.weights(), profile.get("goal", "awareness"))
    stab = scoring.rank_stability(ranked, ui.weights())
    ss.ranked, ss.score_meta, ss.excluded = ranked, meta, excluded

    k1, k2, k3, k4 = st.columns(4)
    k1.metric("Accounts in database", len(universe))
    k2.metric("Passed hard filters", len(ranked), delta=f"-{len(excluded)} filtered out", delta_color="off")
    k3.metric("Top-5 stability", f"{stab['stability']:.0%}",
              help="How much the top 5 stays the same when the weights are varied randomly around your setting. "
                   "Below about 60% means the ranking depends strongly on your weights.")
    n_check = int(((ranked.persona_verdict == "unclear") | ranked.data_warnings.map(len).gt(0)).sum()) if "persona_verdict" in ranked else 0
    k4.metric("Need a manual check", n_check, help="Creators in the ranking whose persona is unclear for this brief or whose data has a "
                                                   "warning (old numbers, follower jump, fake followers, expired rate card).")
    if stab["stability"] < 0.6:
        st.warning("The top 5 changes a lot when weights move slightly. Treat close ranks as a tie and check the Stability tab.", icon=":material/balance:")

    fc = meta["forecast"]
    if not ui.tech():
        st.caption(f"Reach forecasts are corrected with the agency's past campaign results, so they are realistic rather than "
                   f"optimistic. Turn on 'Show technical details' in the Menu to see the model.")
    elif fc["reach"] == "random forest":
        st.caption(f"Reach forecast by the random forest, trained on {fc['n']} past results "
                   f"({fc['reach_gain']:+.0%} lower cross-validated error than the best simple baseline). "
                   f"Conversions: {fc['conv']}.")
    elif fc["reach"] == "tier correction":
        corr_g = meta["correction"]["_global"]
        st.caption(f"Reach forecasts are corrected per audience size using {corr_g['n']} past results "
                   f"(average bias {(np.exp(corr_g['mu']) - 1) * 100:+.0f}%).")
    else:
        st.caption("Reach forecasts are the naive average-views numbers (no correction).")
    if ui.tech():
        for note in fc["notes"]:
            st.info(note, icon=":material/info:")
        st.caption(f"Topic similarity between brief and creator texts: {meta['embedding_backend']} "
                   "(TF-IDF = word and letter-pattern matching that weighs rare, specific words more than common ones).")
        ui.page_link("pages/7_Forecast_Model.py", label="How is the forecast made?", icon=":material/forest:")

    if profile.get("creator_persona"):
        with st.expander(f"Creator persona: {profile['creator_persona']}", icon=":material/face:", expanded=False):
            uni = pack["universe"]
            counts = uni.drop_duplicates("influencer_id").persona_verdict.value_counts()
            by_ai = persona_info.get("asked_ai", 0) or (any(v.get("source") == "ai" for v in verdicts.values()))
            st.caption(("Checked by the AI model" if by_ai else "Checked with keyword rules (no AI key)") +
                       f": {counts.get('fits', 0)} fit, {counts.get('unclear', 0)} unclear, {counts.get('does_not_fit', 0)} do not fit. "
                       "Only clear 'does not fit' verdicts with an exact quote from the persona description are filtered out; "
                       "'unclear' creators stay in the ranking with a note. Personas are edited and redrafted in the Explorer.")
            if persona_info.get("error"):
                st.warning(f"AI persona check failed, keyword rules were used: {persona_info['error']}")
            out_p = uni[uni.persona_verdict == "does_not_fit"].drop_duplicates("influencer_id")
            if len(out_p):
                st.multiselect("Put back in despite the verdict", out_p.influencer_id.astype(int).tolist(), key="f_persona_keep",
                               format_func=lambda i: out_p.set_index("influencer_id").name.get(i, str(i)),
                               help="A person has the final say: creators chosen here are scored again.")
                tables.show(out_p[["name", "persona_evidence", "persona_reason"]].rename(
                    columns={"persona_evidence": "Evidence (quote)", "persona_reason": "Why"}), height=220)

    t_rank, t_break, t_cmp, t_stab, t_out = st.tabs(["Ranking", "Score breakdown", "Compare", "Stability", "Filtered out"])

    # ---------------------------------------------------------------- ranking
    with t_rank:
        def notes(r) -> str:
            parts = list(r.conflicts) + list(r.data_warnings) + [f"flag: {t.replace('_', ' ')} ({s})" for t, s in r.safety_flags]
            if r.get("persona_verdict") == "unclear":
                parts.append("persona unclear: " + (r.get("persona_reason") or "check by hand"))
            elif r.get("persona_verdict") == "does_not_fit":
                parts.append("persona does not fit (kept in by a person)")
            return "; ".join(parts)
        show = ranked.assign(notes=ranked.apply(notes, axis=1),
                             platform_label=ranked.platform.map(lambda p: tables.BRAND_CASE.get(p, p)),
                             creator=ranked.apply(lambda r: f"{r['name']} ({tables.BRAND_CASE.get(r.platform, r.platform)})", axis=1),
                             reach_range=ranked.apply(lambda r: f"{r.reach_p10:,.0f} to {r.reach_p90:,.0f}", axis=1))
        table = show[["rank", "creator", "followers", "er_view", "er", "price", "reach_p50", "reach_range", "total", "notes"]]
        ev = st.dataframe(
            table, hide_index=True, width="stretch", height=440, on_select="rerun", selection_mode="multi-row", key="rank_table",
            column_config={
                "rank": st.column_config.NumberColumn("#", width=44),
                "creator": st.column_config.TextColumn("Creator (platform)", width=190),
                "followers": st.column_config.NumberColumn("Followers", format="compact", width=82),
                "er_view": st.column_config.NumberColumn("Eng./view", format="%.1f%%", width=76,
                                                         help="Interactions divided by average views (the agency's definition)."),
                "er": st.column_config.NumberColumn("Eng./foll.", format="%.2f%%", width=76,
                                                    help="Interactions divided by followers."),
                "price": st.column_config.NumberColumn("Fee", format="euro", width=68),
                "reach_p50": st.column_config.NumberColumn("Reach p50", format="compact", width=84),
                "reach_range": st.column_config.TextColumn("Range p10 to p90", width=128),
                "total": st.column_config.ProgressColumn("Score", min_value=0.0, max_value=1.0, format="%.2f", width=96),
                "notes": st.column_config.TextColumn("Warnings and flags", width="large"),
            })
        sel_rows = list(ev.selection.rows) if ev and ev.selection else []
        st.caption("Each row is one account: a creator active on two platforms appears twice, and the mix later picks at most one "
                   "account per person. Select rows to compare them in the Compare tab. The reach range runs from a bad case "
                   "(1 in 10 campaigns does worse) to a good case (1 in 10 does better).")

        C.section("Why these creators?", "Short, fact-based explanations for the top of the ranking. Numbers come from the database, never from the AI.")
        e1, e2 = st.columns([1, 3], vertical_alignment="bottom")
        ui.defaults(m_explain_n=8)
        top_n = e1.slider("Explain top N", 3, 12, key="m_explain_n")
        if e2.button("Explain the top creators", icon=":material/smart_toy:"):
            with st.spinner("Writing explanations ..."):
                res = ai.explain_candidates(ui.ai_client(), ranked, profile, top_n)
            ss.explanations, ss.explanations_source, ss.explanations_error = res.data, res.source, res.error
        if ss.explanations:
            ui.ai_badge(ss.explanations_source, ss.get("explanations_error"))
            by_acc = ranked.set_index("account_id")
            items = [(a, e) for a, e in ss.explanations.items() if a in by_acc.index]
            for i in range(0, len(items), 2):
                cols = st.columns(2)
                for col, (acc_id, e) in zip(cols, items[i:i + 2]):
                    r = by_acc.loc[acc_id]
                    with col:
                        with ui.card():
                            C.chips([(f"#{int(r['rank'])}", "primary"), (r["platform"], "outline"), (f"score {r.total:.2f}", "outline")])
                            st.markdown(f"**{r['name']}**")
                            st.caption(e["headline"])
                            st.html(C.bullet_card("Strengths", e["strengths"], "good") + C.bullet_card("Risks", e["risks"], "risk"))
                            if e.get("content_angle"):
                                st.markdown(f"**Content angle:** {e['content_angle']}")
                            if r.get("persona"):
                                st.caption(f"Persona: {r['persona']}")
                            st.caption(f"{int(r.followers):,} followers, {r.er:.2f}% engagement, fee EUR {r.price:,.0f} for a {r.deliverable}, "
                                       f"forecast reach {int(r.reach_p10):,} to {int(r.reach_p90):,}.")

    # ---------------------------------------------------------------- breakdown
    with t_break:
        st.plotly_chart(charts.contributions_bar(ranked), theme=None, width="stretch")
        tables.show(ranked[["rank", "name", "platform"] + config.CRITERIA].head(30), score=config.CRITERIA, labels=config.CRITERIA_LABELS, height=420)
        with st.expander("What do the criteria mean?"):
            for c in config.CRITERIA:
                st.markdown(f"**{config.CRITERIA_LABELS[c]}**: {config.CRITERIA_HELP[c]}")

    # ---------------------------------------------------------------- compare
    with t_cmp:
        rows = ranked.iloc[sel_rows] if sel_rows else ranked.head(3)
        rows = rows.head(6)
        st.caption("Showing the selected creators." if sel_rows else "Showing the top 3. Select rows in the Ranking tab to choose others (max 6).")
        st.plotly_chart(charts.radar(rows), theme=None, width="stretch")
        tables.show(rows[["name", "platform", "followers", "er", "fake", "price", "cpm", "female_pct", "age_25_34", "relationship_score"]],
                    formats={"cpm": "%.0f"})

    # ---------------------------------------------------------------- stability
    with t_stab:
        st.markdown(f"**Top-5 stability: {stab['stability']:.0%}.** "
                    "Weights are an opinion, so the tool tests whether the shortlist survives different opinions. "
                    "It draws 400 random weight sets around yours and counts how often each creator stays in the top 5.")
        st.plotly_chart(charts.stability_bar(ranked, stab["top_freq"]), theme=None, width="stretch")
        st.markdown("**Which weight matters most?** How many of the current top 5 change if one weight is doubled or set to zero:")
        st.plotly_chart(charts.sensitivity_bar(scoring.weight_sensitivity(ranked, ui.weights())), theme=None, width="stretch")

    # ---------------------------------------------------------------- filtered out
    with t_out:
        counts = excluded.exclude_reason.value_counts()
        st.markdown(f"**{len(excluded)} accounts were filtered out.** Each is listed with the first rule it failed.")
        st.bar_chart(counts, horizontal=True, color="#1B7F6B")
        tables.show(excluded.assign(detail=excluded.persona_evidence.where(excluded.exclude_reason.str.startswith("persona"), ""))
                    [["name", "platform", "country", "followers", "price", "exclude_reason", "detail"]].sort_values("exclude_reason"),
                    labels={"detail": "Evidence"}, height=420)

st.write("")
if st.button("Continue to the campaign mix", type="primary", icon=":material/arrow_forward:", key="short_continue"):
    st.switch_page("pages/3_Campaign_Mix.py")
