"""Meta Instagram API: the private numbers only the creator can see, with the creator's permission.

Real API (Instagram API with Instagram Login, Business or Creator accounts, scopes instagram_business_basic and
instagram_business_manage_insights):
  * GET /{ig-media-id}/insights?metric=reach,views,likes,comments,saved,shares,total_interactions   (reels and posts)
  * GET /{ig-media-id}/insights?metric=reach,views,link_clicks,navigation,replies                   (stories, 24 h only)
  * GET /{ig-user-id}/insights?metric=follower_demographics&period=lifetime&metric_type=total_value&breakdown=age|gender|country
Each creator connects once (OAuth login), app review by Meta is needed before real use, and story numbers must be
collected within 24 hours (Meta sends a `story_insights` webhook).

In this demo the API is MOCKED: `MockMetaClient` answers with the same JSON shape as the Graph API, with numbers
derived deterministically from the account's public snapshot. `GraphMetaClient` is the real client with the same
methods; switching is a configuration change (META_MODE=live plus a token per creator), not a rebuild.

When the app collects these numbers:
  1. Monthly (with the Apify refresh): average reach, saves, shares, story views and story link clicks per connected
     account -> insight_snapshot (feeds the stats pack).
  2. During a campaign: the campaign posts at 1, 7 and 30 days after posting, stories within 24 hours
     -> post_insight, which pre-fills the result form in the decision log (the agency adds sales and the client rating).
"""
from __future__ import annotations

import hashlib
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd

from . import config, db

GRAPH_URL = "https://graph.instagram.com"
GRAPH_VERSION = "v23.0"
REEL_METRICS = ["reach", "views", "likes", "comments", "saved", "shares", "total_interactions"]
STORY_METRICS = ["reach", "views", "link_clicks", "navigation", "replies"]
SCOPES = "instagram_business_basic,instagram_business_manage_insights"


def _rng(*parts) -> np.random.Generator:
    h = hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()
    return np.random.default_rng(int(h[:12], 16))


def _metric(name: str, value, media_id: str, period: str = "lifetime") -> dict:
    return {"name": name, "period": period, "values": [{"value": value}], "title": name.replace("_", " ").title(),
            "id": f"{media_id}/insights/{name}/{period}"}


def parse_insights(resp: dict) -> dict:
    """Graph API insights response -> {metric: value}. Unavailable metrics are simply missing (the API returns no row)."""
    out = {}
    for m in (resp or {}).get("data", []):
        vals = m.get("values") or []
        if vals:
            out[m["name"]] = vals[0].get("value")
        elif "total_value" in m:
            out[m["name"]] = m["total_value"].get("value")
    return out


# ----------------------------------------------------------------------------- clients
class MockMetaClient:
    """Answers like the Graph API, from the database. Deterministic: the same account and date give the same numbers."""
    mode = "mock"

    def __init__(self, path=None):
        self.path = path

    def _snapshot(self, account_id: int, on: str) -> pd.Series | None:
        s = db.read("""SELECT * FROM account_snapshot WHERE account_id = ? AND captured_at <= ? ORDER BY captured_at DESC LIMIT 1""",
                    (int(account_id), on), path=self.path)
        if s.empty:
            s = db.read("SELECT * FROM account_snapshot WHERE account_id = ? ORDER BY captured_at LIMIT 1", (int(account_id),), path=self.path)
        return None if s.empty else s.iloc[0]

    def account_period_insights(self, account_id: int, period_end: str) -> dict:
        """Averages over the reels and stories of the 30 days before `period_end` (the app aggregates media insights
        like this with the real API). Returned in Graph API shape, one 'media' list with insights per item."""
        snap = self._snapshot(account_id, period_end)
        if snap is None:
            return {"data": [], "media": []}
        rng = _rng("period", account_id, period_end)
        f, er, views = float(snap.followers), float(snap.engagement_rate), float(snap.avg_views)
        media = []
        for k in range(int(rng.integers(4, 13))):
            mid = f"{account_id}{period_end.replace('-', '')}{k:02d}"
            v = views * float(np.exp(rng.normal(0, 0.35)))
            inter = er / 100 * f * float(np.exp(rng.normal(0, 0.3)))
            saves, shares, comments = inter * rng.uniform(0.08, 0.18), inter * rng.uniform(0.05, 0.12), inter * rng.uniform(0.03, 0.07)
            likes = max(inter - saves - shares - comments, 0)
            media.append({"id": mid, "media_type": "REELS", "insights": {"data": [
                _metric("reach", int(v * rng.uniform(0.6, 0.85)), mid), _metric("views", int(v), mid), _metric("likes", int(likes), mid),
                _metric("comments", int(comments), mid), _metric("saved", int(saves), mid), _metric("shares", int(shares), mid),
                _metric("total_interactions", int(inter), mid)]}})
        for k in range(int(rng.integers(3, 9))):
            mid = f"{account_id}{period_end.replace('-', '')}s{k:02d}"
            sv = f * float(rng.uniform(0.04, 0.10))
            media.append({"id": mid, "media_type": "STORY", "insights": {"data": [
                _metric("reach", int(sv * 0.9), mid), _metric("views", int(sv), mid),
                _metric("link_clicks", int(sv * rng.uniform(0.005, 0.025)), mid), _metric("replies", int(sv * 0.002), mid)]}})
        return {"data": [], "media": media}

    def media_insights(self, participation_id: int, media_type: str, days_after_post: int) -> dict:
        """Insights for a campaign post, `days_after_post` days after it went live (reach grows and flattens)."""
        p = db.read("SELECT * FROM participation WHERE id = ?", (int(participation_id),), path=self.path).iloc[0]
        rng = _rng("media", participation_id, media_type)
        final = float(p.reach_p50) * float(np.exp(rng.normal(-0.12, 0.30)))      # slightly below forecast, like history
        share = {1: 0.55, 7: 0.88, 30: 1.0}.get(int(days_after_post), min(1.0, 0.55 + 0.015 * days_after_post))
        mid = f"p{participation_id}{media_type[0]}"
        if media_type == "STORY":
            sv = final * 0.35
            return {"data": [_metric("reach", int(sv * 0.9), mid), _metric("views", int(sv), mid),
                             _metric("link_clicks", int(sv * rng.uniform(0.008, 0.03)), mid), _metric("replies", int(sv * 0.002), mid)]}
        reach = final * share
        inter = reach * float(rng.uniform(0.03, 0.08))
        return {"data": [_metric("reach", int(reach), mid), _metric("views", int(reach * rng.uniform(1.2, 1.5)), mid),
                         _metric("likes", int(inter * 0.75), mid), _metric("comments", int(inter * 0.05), mid),
                         _metric("saved", int(inter * 0.12), mid), _metric("shares", int(inter * 0.08), mid),
                         _metric("total_interactions", int(inter), mid)]}


class GraphMetaClient:
    """Real Graph API client (same interface as the mock). Needs a long-lived user access token per creator, obtained
    through Instagram Login; store tokens in a secrets vault, not in this database. Not used in the demo."""
    mode = "live"

    def __init__(self, tokens: dict[int, str], ig_user_ids: dict[int, str], version: str = GRAPH_VERSION):
        self.tokens, self.ig_user_ids, self.version = tokens, ig_user_ids, version

    def _get(self, node: str, params: dict, account_id: int) -> dict:
        import requests
        r = requests.get(f"{GRAPH_URL}/{self.version}/{node}", params={**params, "access_token": self.tokens[account_id]}, timeout=30)
        r.raise_for_status()
        return r.json()

    def account_period_insights(self, account_id: int, period_end: str) -> dict:
        end = date.fromisoformat(period_end)
        since = int(datetime.combine(end - timedelta(days=30), datetime.min.time()).timestamp())
        media = self._get(f"{self.ig_user_ids[account_id]}/media", {"fields": "id,media_type,media_product_type,timestamp",
                                                                      "since": since, "limit": 50}, account_id).get("data", [])
        stories = self._get(f"{self.ig_user_ids[account_id]}/stories", {"fields": "id,media_type,timestamp"}, account_id).get("data", [])
        out = []
        for m in media:
            if m.get("media_product_type") == "REELS":
                out.append({"id": m["id"], "media_type": "REELS",
                            "insights": self._get(f"{m['id']}/insights", {"metric": ",".join(REEL_METRICS)}, account_id)})
        for m in stories:
            out.append({"id": m["id"], "media_type": "STORY",
                        "insights": self._get(f"{m['id']}/insights", {"metric": ",".join(STORY_METRICS)}, account_id)})
        return {"data": [], "media": out}

    def media_insights(self, participation_id: int, media_type: str, days_after_post: int) -> dict:
        raise NotImplementedError("Map campaign posts to media ids (the creator shares the post link) before using the live API.")


def client(path=None):
    """The configured client: the mock unless META_MODE=live (which also needs per-creator tokens)."""
    if (config.get_secret("META_MODE") or "mock").lower() == "live":
        raise RuntimeError("META_MODE=live needs per-creator access tokens from Instagram Login; not set up in this demo.")
    return MockMetaClient(path)


# ----------------------------------------------------------------------------- monthly insights
def summarize_period(resp: dict) -> dict:
    reels = [parse_insights(m["insights"]) for m in resp.get("media", []) if m["media_type"] == "REELS"]
    stories = [parse_insights(m["insights"]) for m in resp.get("media", []) if m["media_type"] == "STORY"]
    avg = lambda rows, k: round(float(np.mean([r[k] for r in rows if k in r])), 1) if any(k in r for r in rows) else None
    return {"reels_counted": len(reels), "avg_reach": avg(reels, "reach"), "avg_views": avg(reels, "views"),
            "avg_likes": avg(reels, "likes"), "avg_comments": avg(reels, "comments"), "avg_saves": avg(reels, "saved"),
            "avg_shares": avg(reels, "shares"), "story_avg_views": avg(stories, "views"), "story_link_clicks": avg(stories, "link_clicks")}


def connected_accounts(path=None) -> pd.DataFrame:
    return db.read("""SELECT m.account_id, m.status, m.connected_at, a.handle, i.display_name FROM meta_connection m
                      JOIN account a ON a.id = m.account_id JOIN influencer i ON i.id = a.influencer_id""", path=path)


def monthly_refresh(period_end: date, meta_client=None, path=None) -> dict:
    """Store one insight_snapshot per connected account for the 30 days up to `period_end` (skips existing ones)."""
    meta_client = meta_client or client(path)
    acc = connected_accounts(path)
    acc = acc[acc.status == "connected"]
    pe = period_end.isoformat()
    have = set(db.read("SELECT account_id FROM insight_snapshot WHERE captured_at = ? AND source = 'meta_api'", (pe,), path=path).account_id)
    n = 0
    con = db.connect(path)
    try:
        for aid in acc.account_id:
            if int(aid) in have:
                continue
            s = summarize_period(meta_client.account_period_insights(int(aid), pe))
            if not s["reels_counted"]:
                continue
            con.execute("""INSERT INTO insight_snapshot (account_id, captured_at, source, reels_counted, avg_reach, avg_views, avg_likes,
                               avg_comments, avg_saves, avg_shares, story_avg_views, story_link_clicks, proof_file, note)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (int(aid), pe, "meta_api", s["reels_counted"], s["avg_reach"], s["avg_views"], s["avg_likes"], s["avg_comments"],
                         s["avg_saves"], s["avg_shares"], s["story_avg_views"], s["story_link_clicks"], None,
                         f"Meta Instagram API ({getattr(meta_client, 'mode', 'mock')})"))
            n += 1
        con.execute("""INSERT INTO import_log (started_at, connector, mode, accounts, items, est_cost_usd, status, message)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (datetime.now().isoformat(timespec="seconds"), "meta_api", getattr(meta_client, "mode", "mock"), int(len(acc)), n, 0.0,
                     "ok", f"insights for {n} of {len(acc)} connected accounts, period ending {pe}"))
        con.commit()
    finally:
        con.close()
    return {"connected": int(len(acc)), "written": n, "period_end": pe}


# ----------------------------------------------------------------------------- campaign posts
CHECKPOINTS = (1, 7, 30)


def collect_campaign_insights(campaign_id: int, meta_client=None, path=None, as_of: date | None = None,
                              pretend_finished: bool = False) -> dict:
    """Fetch post insights for every participation of a campaign whose account is connected: the post at 1, 7 and 30 days,
    and the story with the link that goes with it at day 1 (stories expire after 24 h). Only checkpoints that have
    already passed are fetched, unless `pretend_finished` (demo: act as if the campaign had run). Existing checkpoints
    are not fetched twice."""
    as_of = as_of or db.as_of(path)
    meta_client = meta_client or client(path)
    camp = db.read("SELECT * FROM campaign WHERE id = ?", (int(campaign_id),), path=path).iloc[0]
    parts = db.read("""SELECT p.*, a.platform, (m.status = 'connected') AS connected FROM participation p
                       JOIN account a ON a.id = p.account_id LEFT JOIN meta_connection m ON m.account_id = p.account_id
                       WHERE p.campaign_id = ?""", (int(campaign_id),), path=path)
    have = db.read("""SELECT participation_id, media_type, days_after_post FROM post_insight
                      WHERE participation_id IN (SELECT id FROM participation WHERE campaign_id = ?)""", (int(campaign_id),), path=path)
    done = {(int(r.participation_id), r.media_type, int(r.days_after_post)) for r in have.itertuples()}
    posted = date.fromisoformat(camp.start_date)
    out = {"fetched": 0, "not_connected": [], "not_instagram": []}
    con = db.connect(path)
    try:
        for p in parts.itertuples():
            if p.platform != "instagram":
                out["not_instagram"].append(int(p.id))
                continue
            if not p.connected:
                out["not_connected"].append(int(p.id))
                continue
            plan = [("REELS", d) for d in CHECKPOINTS] + [("STORY", 1)]
            for media_type, d in plan:
                if (int(p.id), media_type, d) in done:
                    continue
                if not pretend_finished and posted + timedelta(days=d) > as_of:
                    out.setdefault("not_due", 0)
                    out["not_due"] = out["not_due"] + 1
                    continue
                m = parse_insights(meta_client.media_insights(int(p.id), media_type, d))
                con.execute("""INSERT INTO post_insight (participation_id, media_type, posted_at, captured_at, days_after_post, reach, views,
                                   likes, comments, saves, shares, link_clicks, source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (int(p.id), media_type, posted.isoformat(), (posted + timedelta(days=d)).isoformat(), d, m.get("reach"),
                             m.get("views"), m.get("likes"), m.get("comments"), m.get("saved"), m.get("shares"), m.get("link_clicks"),
                             "meta_api"))
                out["fetched"] += 1
        con.commit()
    finally:
        con.close()
    return out


def prefill_result(participation_id: int, path=None) -> dict | None:
    """Values for the result form from the latest post insights: reach and engagement of the reel at its last checkpoint,
    plus story link clicks. Sales and the client rating still come from people."""
    pi = db.read("SELECT * FROM post_insight WHERE participation_id = ? ORDER BY days_after_post", (int(participation_id),), path=path)
    if pi.empty:
        return None
    reel = pi[pi.media_type == "REELS"].tail(1)
    story = pi[pi.media_type == "STORY"].tail(1)
    r = reel.iloc[0] if len(reel) else None
    reach = int(r.reach) if r is not None and pd.notna(r.reach) else 0
    eng = int(sum(x for x in [r.likes, r.comments, r.saves, r.shares] if pd.notna(x))) if r is not None else 0
    clicks = int(story.iloc[0].link_clicks) if len(story) and pd.notna(story.iloc[0].link_clicks) else 0
    return {"actual_reach": reach, "actual_engagement": eng, "link_clicks": clicks,
            "checkpoint_days": int(r.days_after_post) if r is not None else None}
