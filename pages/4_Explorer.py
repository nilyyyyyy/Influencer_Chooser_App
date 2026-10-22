import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from core import charts, components as C, db, persona, report, statspack, tables, ui

ui.page_config("Explorer", ":material/search:")
ui.chrome()

ui.header("Influencer explorer", "Browse the evidence behind the scores: trends over time, audience, rates, flags and campaign history.", eyebrow="Insights")

inf = db.read("SELECT id, display_name, country, city, status FROM influencer ORDER BY display_name")
pick, _ = st.columns([2, 3])
choice = pick.selectbox("Creator", inf.id.tolist(), format_func=lambda i: f"{inf.set_index('id').display_name[i]}  ({inf.set_index('id').country[i]})")
row = db.read("SELECT * FROM influencer WHERE id = ?", (int(choice),)).iloc[0]
accounts = db.read("SELECT * FROM account WHERE influencer_id = ?", (int(choice),))
niches = db.read("""SELECT n.name, inn.is_primary FROM influencer_niche inn JOIN niche n ON n.id = inn.niche_id
                    WHERE inn.influencer_id = ?""", (int(choice),))
as_of = db.as_of().isoformat()

_status_tone = {"active": "success", "paused": "warning", "offboarded": "danger"}
_avail_tone = {"open": "success", "limited": "warning", "unavailable": "danger"}
_flags_n = int(db.read("SELECT COUNT(*) n FROM safety_flag WHERE influencer_id = ?", (int(choice),)).n[0])
C.profile_header(row.display_name, f"{row.city}, {row.country}  |  Languages: {row.languages}  |  Tone: {row.tone}",
                 C.chips_html([(row.status, _status_tone.get(row.status, "neutral")), (f"availability: {row.availability}", _avail_tone.get(row.availability, "neutral")),
                               *[(p, "outline") for p in accounts.platform], *([(f"{_flags_n} safety flag{'s' if _flags_n != 1 else ''}", "danger")] if _flags_n else [])]))
_n_camp = int(db.read("SELECT COUNT(*) n FROM participation WHERE influencer_id = ?", (int(choice),)).n[0])
h1, h2, h3, h4 = st.columns(4)
h1.metric("Relationship score", f"{row.relationship_score} / 5")
h2.metric("Past campaigns", _n_camp)
h3.metric("Active safety flags", _flags_n)
h4.metric("GDPR consent until", row.consent_until, delta="expired" if row.consent_until < as_of else None, delta_color="inverse")

t_prof, t_stats, t_trend, t_aud, t_rate, t_hist, t_content = st.tabs(
    ["Profile", "Stats pack", "Trends", "Audience", "Rates and flags", "Campaign history", "Content"])

with t_prof:
    st.write(row.bio_text)
    prim = ", ".join(n.replace("_", " ") for n, p in zip(niches.name, niches.is_primary) if p)
    sec = ", ".join(n.replace("_", " ") for n, p in zip(niches.name, niches.is_primary) if not p)
    st.markdown(f"Primary niche: **{prim}**" + (f". Also: {sec}." if sec else "."))
    if row.agency_notes:
        st.info(f"Agency notes: {row.agency_notes}", icon=":material/sticky_note_2:")
    tables.show(accounts[["platform", "handle", "url", "verified"]])

    C.section("Persona", "Who the creator is as a person: life stage, family, situation. Free text, so any brief requirement can be "
              "checked against it. Redrafted from new posts, and the agency can correct it at any time.")
    cur = persona.latest().get(int(choice))
    src_label = {"seed": "seeded with the dataset", "ai": "drafted by the AI", "rule": "rule-based", "agency": "written by the agency"}
    if cur:
        st.markdown(cur["description"])
        st.caption(f"Version from {cur['created_at'][:10]}, {src_label.get(cur['source'], cur['source'])}"
                   + (f", based on {cur['based_on']}" if cur.get("based_on") else "") + ".")
    else:
        st.caption("No persona description yet.")
    pe1, pe2 = st.columns([3, 1], vertical_alignment="bottom")
    with pe1:
        edit_key = f"persona_edit_{int(choice)}"
        ui.defaults(**{edit_key: cur["description"] if cur else ""})
        st.text_area("Correct or update the description", key=edit_key, height=90,
                     help="Saved as a new version written by the agency. Older versions are kept.")
    with pe2:
        if st.button("Save correction", icon=":material/save:", key=f"persona_save_{int(choice)}",
                     disabled=bool(not st.session_state.get(edit_key, "").strip()
                                   or (cur and st.session_state[edit_key].strip() == cur["description"]))):
            persona.save(int(choice), st.session_state[edit_key], "agency", "corrected by the agency")
            st.cache_data.clear()
            st.toast("Persona saved")
            st.rerun()
        if st.button("Redraft from latest posts (AI)", icon=":material/auto_awesome:", key=f"persona_ai_{int(choice)}",
                     disabled=ui.ai_client() is None, help="Needs an AI key. Keeps what the current version says unless new posts change it."):
            with st.spinner("Reading bio and posts ..."):
                text, src, err = persona.redraft(int(choice), ui.ai_client())
            if src == "ai":
                st.session_state.pop(edit_key, None)
                st.cache_data.clear()
                st.rerun()
            else:
                st.error(f"Could not redraft: {err}")
    hist_p = persona.history(int(choice))
    if len(hist_p) > 1:
        with st.expander(f"Earlier versions ({len(hist_p) - 1})"):
            tables.show(hist_p.iloc[1:][["created_at", "source", "based_on", "description"]], widths={"description": "large"})

with t_stats:
    st.caption("What a brand agency asks for before booking, with the source of every number. Send it as Markdown, HTML or PDF.")
    acc_pick = st.selectbox("Account", accounts.id.tolist(), key=f"sp_acc_{int(choice)}",
                            format_func=lambda a: f"{accounts.set_index('id').platform[a]}  @{accounts.set_index('id').handle[a]}")
    pack = statspack.build(int(acc_pick))
    cov = pack.coverage()
    C.chips([(f"{cov['verified']} verified", "success"), (f"{cov['public']} public", "primary"),
             (f"{cov['self_reported']} self-reported", "warning"), (f"{cov['estimated']} estimates", "neutral"),
             (f"{cov['missing']} missing", "danger")])
    tables.show(pack.frame(), height=min(36 * (len(pack.metrics) + 1) + 4, 620), widths={"Note": "large"})
    for w in pack.warnings:
        st.warning(w, icon=":material/rule:")
    if pack.requests:
        st.markdown("**Still to request from the creator**")
        for r_ in pack.requests:
            st.markdown(f"- {r_}")
    md = statspack.to_markdown(pack)
    title = f"Stats pack {pack.creator} @{pack.handle}"
    fname = f"stats_pack_{pack.handle}"
    x1, x2, x3 = st.columns(3)
    x1.download_button("Download PDF", report.memo_pdf(md, title), file_name=f"{fname}.pdf", mime="application/pdf",
                       icon=":material/picture_as_pdf:", width="stretch", key=f"sp_pdf_{acc_pick}")
    x2.download_button("Download HTML", report.memo_html(md, title), file_name=f"{fname}.html", mime="text/html",
                       icon=":material/html:", width="stretch", key=f"sp_html_{acc_pick}")
    x3.download_button("Download Markdown", md, file_name=f"{fname}.md", mime="text/markdown", icon=":material/markdown:",
                       width="stretch", key=f"sp_md_{acc_pick}")

    if pack.platform == "instagram":
        with st.expander("Add numbers from a creator's screenshot", icon=":material/add_photo_alternate:"):
            st.caption("For creators who did not connect the Meta API. The numbers are stored as self-reported, with the screenshot, "
                       "and compared with the public numbers.")
            with st.form(f"self_report_{acc_pick}"):
                f1, f2, f3 = st.columns(3)
                period = f1.date_input("Period ends (30 days)", value=db.as_of())
                n_reels = f2.number_input("Reels in the period", 0, step=1)
                reach = f3.number_input("Average reach per reel", 0, step=100)
                views = f1.number_input("Average views per reel", 0, step=100)
                likes = f2.number_input("Average likes", 0, step=10)
                comments = f3.number_input("Average comments", 0, step=1)
                saves = f1.number_input("Average saves", 0, step=1)
                shares = f2.number_input("Average shares", 0, step=1)
                story = f3.number_input("Average story views", 0, step=50)
                clicks = f1.number_input("Average link clicks per story", 0.0, step=1.0)
                shot = st.file_uploader("Screenshot (PNG, JPG or PDF)", type=["png", "jpg", "jpeg", "pdf"])
                if st.form_submit_button("Save self-reported numbers", icon=":material/save:"):
                    nz = lambda v: float(v) if v else None
                    statspack.save_self_report(int(acc_pick), {
                        "reels_counted": int(n_reels) or None, "avg_reach": nz(reach), "avg_views": nz(views), "avg_likes": nz(likes),
                        "avg_comments": nz(comments), "avg_saves": nz(saves), "avg_shares": nz(shares), "story_avg_views": nz(story),
                        "story_link_clicks": nz(clicks)}, period, shot.name if shot else None, shot.getvalue() if shot else None)
                    st.cache_data.clear()
                    st.toast("Saved")
                    st.rerun()

with t_trend:
    for _, a in accounts.iterrows():
        snap = db.read("SELECT * FROM account_snapshot WHERE account_id = ? ORDER BY captured_at", (int(a.id),))
        st.plotly_chart(charts.trend_lines(snap, f"{a.platform} @{a.handle}"), theme=None, width="stretch")
        last = snap.iloc[-1]
        age = (pd.Timestamp(as_of) - pd.Timestamp(last.captured_at)).days
        growth = snap.followers.pct_change().tail(6).max()
        notes = []
        if age > 90:
            notes.append(f"stale: last snapshot {age} days old")
        if growth >= 0.5:
            notes.append(f"follower jump of {growth:.0%} in one month")
        if last.fake_follower_pct >= 30:
            notes.append(f"{last.fake_follower_pct:.0f}% estimated fake followers")
        if notes:
            st.warning("Data warnings: " + "; ".join(notes))
        st.caption(f"Source of the latest snapshot: {last.source} (confidence {last.confidence:.2f}).")

with t_aud:
    cols = st.columns(max(len(accounts), 1))
    for col, (_, a) in zip(cols, accounts.iterrows()):
        aud = db.read("SELECT * FROM audience_snapshot WHERE account_id = ? ORDER BY captured_at DESC LIMIT 1", (int(a.id),))
        with col:
            st.markdown(f"**{a.platform}**")
            if aud.empty:
                st.caption("No audience data")
                continue
            r = aud.iloc[0]
            st.metric("Women", f"{r.female_pct:.0f}%")
            fig = go.Figure(go.Bar(x=["18-24", "25-34", "35-44", "45+"], y=[r.age_18_24, r.age_25_34, r.age_35_44, r.age_45_plus],
                                   marker_color=charts.COLORS[0]))
            fig.update_layout(height=240, yaxis_title="% of audience", margin=dict(l=10, r=10, t=10, b=10))
            st.plotly_chart(fig, theme=None, width="stretch", key=f"age_{a.id}")
            tc = pd.DataFrame(json.loads(r.top_countries))
            tables.show(tc, labels={"country": "Country", "share": "Audience share"}, ratio=["share"])
            st.caption(f"Snapshot {r.captured_at}")

with t_rate:
    rates = db.read("SELECT platform, deliverable, price_eur, usage_rights_months, exclusivity_premium_pct, valid_from, valid_to FROM rate_card WHERE influencer_id = ?", (int(choice),))
    rates["expired"] = rates.valid_to < as_of
    tables.show(rates)
    if rates.expired.any():
        st.warning("Some rate cards have expired. Renegotiate before booking.")
    flags = db.read("SELECT flag_type, severity, source, evidence_url, flagged_at, expires_at FROM safety_flag WHERE influencer_id = ?", (int(choice),))
    excl = db.read("""SELECT b.name AS brand, e.valid_from, e.valid_to FROM exclusivity e JOIN brand b ON b.id = e.brand_id
                      WHERE e.influencer_id = ?""", (int(choice),))
    st.markdown("**Safety flags**")
    if len(flags):
        tables.show(flags)
    else:
        st.caption("None")
    st.markdown("**Exclusivity contracts**")
    if len(excl):
        tables.show(excl)
    else:
        st.caption("None")

with t_hist:
    hist = db.read("""SELECT c.name AS campaign, b.name AS brand, b.category, p.deliverable, p.fee_eur, p.reach_p50,
                             r.actual_reach, r.conversions, r.delivered_on_time, r.client_rating
                      FROM participation p JOIN campaign c ON c.id = p.campaign_id JOIN brand b ON b.id = c.brand_id
                      LEFT JOIN result r ON r.participation_id = p.id WHERE p.influencer_id = ? ORDER BY c.start_date DESC""", (int(choice),))
    if hist.empty:
        st.caption("No campaigns yet.")
    else:
        hist["actual_vs_predicted"] = hist.actual_reach / hist.reach_p50
        tables.show(hist, labels={"reach_p50": "Forecast reach"}, hide=["category"],
                    widths={"campaign": 250, "brand": 130, "deliverable": 80, "fee_eur": 80, "reach_p50": 105, "actual_reach": 100, "conversions": 100,
                            "delivered_on_time": 75, "client_rating": 100, "actual_vs_predicted": 120})

with t_content:
    content = db.read("""SELECT c.posted_at, a.platform, c.format, c.caption, c.views, c.likes, c.comments, c.is_sponsored, b.name AS brand, c.is_disclosed
                         FROM content_item c JOIN account a ON a.id = c.account_id LEFT JOIN brand b ON b.id = c.brand_id
                         WHERE a.influencer_id = ? ORDER BY c.posted_at DESC""", (int(choice),))
    tables.show(content, height=400, labels={"format": "Format"})
    undisclosed = content[(content.is_sponsored == 1) & (content.is_disclosed == 0)]
    if len(undisclosed):
        st.warning(f"{len(undisclosed)} sponsored post(s) without an advertising label. Check compliance before working with this creator.")
