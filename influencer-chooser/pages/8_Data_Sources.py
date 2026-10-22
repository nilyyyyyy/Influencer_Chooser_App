import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd
import streamlit as st

from core import apify, components as C, config, db, meta_api, refresh, tables, ui

ui.page_config("Data sources", ":material/cloud_sync:")
ui.chrome()
ss = st.session_state

ui.header("Data sources", "Where the numbers come from and when they are refreshed. Public numbers come from Apify, private numbers "
          "(reach, saves, story link clicks) from the Meta Instagram API with the creator's permission, or from screenshots.",
          eyebrow="Insights")

apify_secret = config.get_secret("APIFY_TOKEN")
token = apify_secret or ss.get("ui_apify_token") or None
conn = meta_api.connected_accounts()
ig = apify.instagram_accounts()
self_rep = db.read("SELECT COUNT(DISTINCT account_id) n FROM insight_snapshot WHERE source = 'self_reported'").n[0]

# ----------------------------------------------------------------------------- overview
C.tiles([
    ("refresh", "Apify, 1st of every month", "Public profile numbers for every Instagram creator: followers, the latest 12 reels with "
                                             "views, likes and comments, and which posts are paid partnerships."),
    ("shield", "Meta Instagram API (mock)", "Private numbers with the creator's permission: reach, saves, shares, story link clicks, "
                                            "audience. Monthly, and for campaign posts at 1, 7 and 30 days."),
    ("file", "Screenshots", "Creators who did not connect send screenshots. Their numbers are typed in, labelled self-reported "
                            "and cross-checked against the public numbers."),
])
k1, k2, k3, k4 = st.columns(4)
k1.metric("Instagram accounts", len(ig))
k2.metric("Connected to Meta API", int((conn.status == "connected").sum()), help="Expired or revoked connections need a new login.")
k3.metric("With self-reported numbers", int(self_rep))
k4.metric("Apify token", "set" if token else "not set", help="From Streamlit secrets (APIFY_TOKEN) or the field below.")

t_month, t_apify, t_meta, t_log = st.tabs(["Monthly refresh", "Apify live run", "Meta connections", "Import log"])

# ----------------------------------------------------------------------------- monthly refresh
with t_month:
    st.markdown("The monthly job runs on the 1st of each month. In Apify a schedule runs the scraping tasks; this refresh then "
                "**imports the last finished Apify run (no credits)**, pulls the Meta insights for the past month and redrafts "
                "persona descriptions for creators with new posts (AI only).")
    period_end = refresh.month_end_before(date.today())
    c1, c2 = st.columns([2, 3])
    with c1:
        pe = st.date_input("Period ends", value=period_end, key="ds_period_end")
        redraft = st.checkbox("Redraft personas of creators with new posts (AI)", value=True, key="ds_redraft",
                              disabled=ui.ai_client() is None, help="Needs the AI. At most 20 per run.")
        st.caption(f"{len(refresh.personas_due())} persona description(s) are older than the creator's newest post.")
        if st.button("Run the monthly refresh now", type="primary", icon=":material/sync:"):
            client = apify.ApifyClient(token) if token else None
            with st.spinner("Refreshing ..."):
                res = refresh.monthly_refresh(pe, apify_client=client, ai_client=ui.ai_client(), redraft_personas=redraft)
            st.cache_data.clear()
            ss._last_refresh = res
    with c2:
        res = ss.get("_last_refresh")
        if res:
            st.success(f"Refresh for the period ending {res['period_end']} finished.", icon=":material/check_circle:")
            st.html(C.kv_html([("Apify", str(res["apify"])),
                               ("Meta API", f"{res['meta']['written']} of {res['meta']['connected']} connected accounts" if res["meta"] else "failed"),
                               ("Personas", f"{res['personas']['redrafted']} redrafted, {res['personas']['due']} were due")]))
            for e in res["errors"]:
                st.warning(e)
    if apify_secret:
        st.info(f"Automatic import is on: once a day the app pulls the newest finished Apify run (free). "
                f"Today: {ss.get('_apify_auto', 'not checked yet')}.", icon=":material/autorenew:")
    else:
        st.caption("Add APIFY_TOKEN to the app's secrets to switch on the automatic daily import of the newest Apify run.")
    with st.expander("How to set up the monthly Apify schedule (once)"):
        st.markdown(apify.schedule_instructions(ig.handle.tolist()))

# ----------------------------------------------------------------------------- apify live
with t_apify:
    st.markdown("Run the Apify scrapers **now** for chosen Instagram accounts. This is the step that **spends Apify credits**, "
                "so it needs a confirmation and every run has a hard cost cap.")
    if not apify_secret:
        st.text_input("Apify API token", type="password", key="ui_apify_token",
                      help="Kept in this browser session only. For deployment add APIFY_TOKEN to Streamlit secrets. Never commit it.")
    else:
        st.caption("Apify token loaded from secrets.")
    real = ig[ig.origin == "real_public"]
    opts = ig.handle.tolist()
    ui.defaults(ds_handles=real.handle.tolist())
    handles = st.multiselect("Instagram accounts to scrape", opts, key="ds_handles",
                             help="The two real creators are preselected. The synthetic creators have made-up handles, so scraping them "
                                  "would return nothing useful.")
    reels_n = st.slider("Reels per account", 3, 30, apify.REELS_PER_ACCOUNT, key="ds_reels")
    est = apify.estimate_cost(len(handles), reels_n)
    st.info(f"Estimated cost: {est['results']} results, about **USD {est['usd']:.2f}** "
            f"({est['share_of_free_plan']:.1%} of the free plan's USD {apify.FREE_MONTHLY_USD:.0f} per month). "
            f"Each of the two actor runs is capped at USD {max(round(est['usd'] * 1.5, 2), 0.05):.2f}.", icon=":material/payments:")
    confirm = st.checkbox("I confirm this run may spend Apify credits", key="ds_confirm")
    b1, b2 = st.columns(2)
    if b1.button("Run Apify now", type="primary", icon=":material/cloud_download:", disabled=not (token and handles and confirm)):
        try:
            with st.spinner("Scraping (this can take a few minutes) ..."):
                rep = apify.refresh_now(apify.ApifyClient(token), handles, confirm_spend=True, reels_per_account=reels_n)
            st.cache_data.clear()
            st.success(f"Imported: {rep.message()}. The data's 'today' is now {rep.captured_at}.", icon=":material/check_circle:")
        except Exception as e:
            st.error(str(e))
    if b2.button("Check the token (free)", icon=":material/key:", disabled=not token):
        try:
            me = apify.ApifyClient(token).whoami()
            st.success(f"Token works for Apify user {me.get('username', '?')}.")
        except Exception as e:
            st.error(str(e))
    if not token:
        st.caption("Add a token to enable the buttons.")

# ----------------------------------------------------------------------------- meta
with t_meta:
    st.markdown("Creators connect once with Instagram Login (scopes `instagram_business_basic`, `instagram_business_manage_insights`). "
                "**In this demo the Meta API is mocked**: answers have the same format as the real Graph API, with numbers derived "
                "from the public snapshot. Switching to the real API is a configuration change (`META_MODE=live` plus a token per "
                "creator) after Meta's app review.")
    if conn.empty:
        st.caption("No connections.")
    else:
        tables.show(conn[["display_name", "handle", "status", "connected_at"]], labels={"display_name": "Creator"}, height=320)
    not_conn = ig[~ig.account_id.isin(conn.account_id)]
    st.caption(f"{len(not_conn)} Instagram account(s) without a connection: their private numbers come from screenshots, or are missing "
               "(see the stats pack in the Explorer).")

# ----------------------------------------------------------------------------- log
with t_log:
    log = db.read("SELECT * FROM import_log ORDER BY id DESC LIMIT 200")
    if log.empty:
        st.caption("No imports yet. Run the monthly refresh or an Apify run.")
    else:
        tables.show(log.drop(columns=["id"]), labels={"est_cost_usd": "Est. cost (USD)"}, height=360)
