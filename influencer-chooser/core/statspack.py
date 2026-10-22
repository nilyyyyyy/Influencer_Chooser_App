"""Creator stats pack: the numbers a brand agency asks for before booking, each labelled with where it comes from.

Sources, from most to least trustworthy:
  verified      Meta Instagram API with the creator's permission (private insights)
  public        Apify scrape of the public profile, or an API/CSV import
  self_reported typed in from a screenshot the creator sent (the screenshot is attached)
  estimated     an estimate (for example views from a market formula) or an AI-estimated snapshot
  missing       not available yet -> goes on the list of things to request from the creator

Self-reported numbers are cross-checked against the public numbers; large gaps are flagged.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

import pandas as pd

from . import config, db

STATUS_LABEL = {"verified": "Verified (Meta API)", "public": "Public data", "self_reported": "Self-reported (screenshot)",
                "estimated": "Estimate", "missing": "Missing"}
SNAPSHOT_STATUS = {"apify": "public", "api": "public", "csv_import": "public", "manual": "estimated", "ai_estimated": "estimated",
                   "meta_api": "verified", "self_reported": "self_reported"}
SNAPSHOT_SOURCE = {"apify": "Apify (public profile)", "api": "Platform API", "csv_import": "CSV import", "manual": "Entered by hand",
                   "ai_estimated": "AI estimate", "meta_api": "Meta API", "self_reported": "Creator screenshot"}
CROSS_CHECK_RATIO = 1.3          # self-reported views more than 30% above the public average are flagged
UPLOAD_DIR = config.ROOT / "data" / "uploads"


@dataclass
class Metric:
    group: str
    label: str
    value: float | None
    unit: str
    status: str
    source: str
    as_of: str | None
    note: str = ""

    def text(self) -> str:
        if self.value is None:
            return "n/a"
        v = self.value
        if self.unit == "%":
            return f"{v:.2f}%"
        if self.unit == "EUR":
            return f"EUR {v:,.0f}"
        return f"{v:,.1f}" if abs(v) < 100 and v != int(v) else f"{v:,.0f}"


@dataclass
class StatsPack:
    account_id: int
    creator: str
    handle: str
    platform: str
    url: str
    as_of: str
    metrics: list[Metric] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    requests: list[str] = field(default_factory=list)
    proof_files: list[str] = field(default_factory=list)

    def frame(self) -> pd.DataFrame:
        return pd.DataFrame([{"Group": m.group, "Metric": m.label, "Value": m.text(), "Status": STATUS_LABEL[m.status],
                              "Source": m.source, "As of": m.as_of or "", "Note": m.note} for m in self.metrics])

    def coverage(self) -> dict:
        c = pd.Series([m.status for m in self.metrics]).value_counts().to_dict()
        return {k: int(c.get(k, 0)) for k in STATUS_LABEL}


def _f(x):
    return None if x is None or (isinstance(x, float) and pd.isna(x)) or pd.isna(x) else float(x)


def build(account_id: int, path=None) -> StatsPack:
    acc = db.read("""SELECT a.*, i.display_name, i.origin FROM account a JOIN influencer i ON i.id = a.influencer_id
                     WHERE a.id = ?""", (int(account_id),), path=path).iloc[0]
    as_of = db.as_of(path).isoformat()
    pack = StatsPack(int(account_id), acc.display_name, acc.handle, acc.platform, acc.url, as_of)
    M = pack.metrics.append

    # ------------------------------------------------------------------ public profile
    snaps = db.read("SELECT * FROM account_snapshot WHERE account_id = ? ORDER BY captured_at", (int(account_id),), path=path)
    last = snaps.iloc[-1] if len(snaps) else None
    if last is not None:
        st, src = SNAPSHOT_STATUS.get(last.source, "estimated"), SNAPSHOT_SOURCE.get(last.source, last.source)
        listing = acc.origin == "real_public" and last.source == "manual"      # real creator, numbers from the public listing
        if listing:
            st, src = "public", "Modash public list"
        est = ("estimated", "Market formula") if listing else (st, src)
        est_note = "Estimated from followers until the first Apify refresh" if listing else ""
        basis = "likes + comments" if last.source == "apify" else "likes, comments, saves, shares"
        er_view = _f(last.engagement_rate) * _f(last.followers) / max(_f(last.avg_views) or 1, 1)
        M(Metric("Public profile", "Followers", _f(last.followers), "", st, src, last.captured_at))
        M(Metric("Public profile", "Average views per post (organic)", _f(last.avg_views), "", *est, last.captured_at, est_note))
        M(Metric("Engagement", "Engagement per view", er_view, "%", *est, last.captured_at,
                 f"({basis}) / views; agency threshold {config.MIN_ENGAGEMENT_PCT:g}%" + ("; uses estimated views" if listing else "")))
        M(Metric("Engagement", "Engagement per follower", _f(last.engagement_rate), "%", st, src, last.captured_at,
                 "as published by the listing" if listing else f"({basis}) / followers"))
        M(Metric("Public profile", "Posts per week", _f(last.posts_per_week), "", *est, last.captured_at))
        M(Metric("Quality", "Estimated fake followers", _f(last.fake_follower_pct), "%", "estimated", "Audit estimate", last.captured_at,
                 "Not measurable from public data; carried over from the last audit"))
        if len(snaps) >= 4:
            g = snaps.followers.iloc[-1] / snaps.followers.iloc[-4] - 1
            M(Metric("Public profile", "Follower growth, last 3 snapshots", g * 100, "%", st, src, last.captured_at))
        if (pd.Timestamp(as_of) - pd.Timestamp(last.captured_at)).days > config.STALE_DAYS:
            pack.warnings.append(f"Public numbers are from {last.captured_at}, older than {config.STALE_DAYS} days.")
        if last.fake_follower_pct >= config.FAKE_PCT_WARN:
            pack.warnings.append(f"About {last.fake_follower_pct:.0f}% fake followers estimated.")
    else:
        pack.requests.append("Public profile numbers (run an Apify refresh)")

    organic = db.read("""SELECT COUNT(*) n, AVG(views) v FROM content_item WHERE account_id = ? AND is_sponsored = 0""",
                      (int(account_id),), path=path).iloc[0]
    sponsored = db.read("""SELECT COUNT(*) n, AVG(views) v FROM content_item WHERE account_id = ? AND is_sponsored = 1""",
                        (int(account_id),), path=path).iloc[0]
    if sponsored.n and organic.n and organic.v:
        M(Metric("Engagement", "Sponsored vs organic views", _f(sponsored.v) / _f(organic.v) * 100, "%", "public", "Recent posts",
                 None, f"{int(sponsored.n)} sponsored vs {int(organic.n)} organic posts"))

    # ------------------------------------------------------------------ private insights (Instagram only)
    if acc.platform == "instagram":
        conn = db.read("SELECT * FROM meta_connection WHERE account_id = ?", (int(account_id),), path=path)
        ins = db.read("SELECT * FROM insight_snapshot WHERE account_id = ? ORDER BY captured_at DESC, source", (int(account_id),), path=path)
        meta_rows = ins[ins.source == "meta_api"]
        self_rows = ins[ins.source == "self_reported"]
        row, st, src = None, "missing", ""
        if len(meta_rows):
            row, st, src = meta_rows.iloc[0], "verified", "Meta API"
        elif len(self_rows):
            row, st, src = self_rows.iloc[0], "self_reported", "Creator screenshot"
            if isinstance(row.proof_file, str) and row.proof_file:
                pack.proof_files.append(row.proof_file)
        if conn.empty:
            pack.warnings.append("Creator has not connected the Meta Instagram API: private numbers come from screenshots.")
        elif conn.iloc[0].status != "connected":
            pack.warnings.append(f"Meta API connection {conn.iloc[0].status}: ask the creator to log in again.")
        fields = [("Reach per reel (30 days)", "avg_reach"), ("Views per reel (30 days)", "avg_views"), ("Saves per reel", "avg_saves"),
                  ("Shares per reel", "avg_shares"), ("Story views", "story_avg_views"), ("Link clicks per story", "story_link_clicks")]
        for label, col in fields:
            v = _f(row[col]) if row is not None else None
            M(Metric("Private insights", label, v, "", st if v is not None else "missing", src if v is not None else "", row.captured_at if row is not None else None))
        if row is not None and all(_f(row[c]) is not None for c in ("avg_likes", "avg_comments", "avg_saves", "avg_shares", "avg_views")) and row.avg_views:
            full = (row.avg_likes + row.avg_comments + row.avg_saves + row.avg_shares) / row.avg_views * 100
            M(Metric("Engagement", "Engagement per view, incl. saves and shares", full, "%", st, src, row.captured_at,
                     "The agency's full definition"))
        if row is not None and _f(row.story_avg_views) and _f(row.story_link_clicks) is not None:
            M(Metric("Private insights", "Story link click rate", row.story_link_clicks / row.story_avg_views * 100, "%", st, src, row.captured_at))
        missing = [lbl for lbl, col in fields if row is None or _f(row[col]) is None]
        if missing:
            pack.requests.append("Screenshot of Instagram insights for the last 30 days: " + ", ".join(m.lower() for m in missing))
        # cross-check self-reported against public
        if st == "self_reported" and last is not None and _f(row.avg_views) and last.avg_views:
            ratio = row.avg_views / last.avg_views
            if ratio > CROSS_CHECK_RATIO:
                pack.warnings.append(f"Self-reported views ({row.avg_views:,.0f}) are {ratio:.1f}x the public average "
                                     f"({last.avg_views:,.0f}). Ask for a screen recording before using them.")
        if st == "self_reported" and isinstance(row.proof_file, str) and row.proof_file and not (config.ROOT / "data" / row.proof_file).exists():
            pack.warnings.append("The screenshot file is not stored in this demo database (synthetic example).")

    # ------------------------------------------------------------------ audience
    aud = db.read("SELECT * FROM audience_snapshot WHERE account_id = ? ORDER BY captured_at DESC LIMIT 1", (int(account_id),), path=path)
    if len(aud):
        a = aud.iloc[0]
        conn_ok = acc.platform == "instagram" and not db.read(
            "SELECT 1 FROM meta_connection WHERE account_id = ? AND status = 'connected'", (int(account_id),), path=path).empty
        st, src = ("verified", "Meta API") if conn_ok else ("public", "Agency database")
        top = json.loads(a.top_countries)
        M(Metric("Audience", "Women in audience", _f(a.female_pct), "%", st, src, a.captured_at))
        M(Metric("Audience", "Audience aged 25-34", _f(a.age_25_34), "%", st, src, a.captured_at))
        if top:
            M(Metric("Audience", f"Audience in {top[0]['country']}", top[0]["share"] * 100, "%", st, src, a.captured_at))
    else:
        for lbl in ("Women in audience", "Audience by age", "Audience by country"):
            M(Metric("Audience", lbl, None, "", "missing", "", None))
        pack.requests.append("Screenshot of the audience tab (age, gender, top countries)")

    # ------------------------------------------------------------------ price
    rate = db.read("""SELECT deliverable, price_eur, valid_to FROM rate_card WHERE influencer_id = ? AND platform = ?""",
                   (int(acc.influencer_id), acc.platform), path=path)
    for r in rate.itertuples():
        est = acc.origin == "real_public"
        M(Metric("Price", f"Fee: {r.deliverable.replace('_', ' ')}", float(r.price_eur), "EUR", "estimated" if est else "public",
                 "Market formula" if est else "Rate card", None, "Estimate, confirm with the creator" if est else f"valid until {r.valid_to}"))
    return pack


# ----------------------------------------------------------------------------- export
def to_markdown(pack: StatsPack) -> str:
    L = [f"# Stats pack: {pack.creator} (@{pack.handle}, {pack.platform})", "",
         f"*Prepared {pack.as_of} by the agency. Profile: {pack.url}. Every number shows where it comes from.*", ""]
    for g, rows in pack.frame().groupby("Group", sort=False):
        L += [f"## {g}", "", "| Metric | Value | Status | Source | As of | Note |", "|---|---:|---|---|---|---|"]
        L += [f"| {r.Metric} | {r.Value} | {r.Status} | {r.Source} | {r['As of']} | {r.Note} |" for _, r in rows.iterrows()]
        L.append("")
    if pack.warnings:
        L += ["## Checks", ""] + [f"- {w}" for w in pack.warnings] + [""]
    if pack.requests:
        L += ["## Still to request from the creator", ""] + [f"- {r}" for r in pack.requests] + [""]
    L.append("Status: Verified = Meta Instagram API with the creator's permission; Public = scraped or imported public data; "
             "Self-reported = typed in from the creator's screenshot; Estimate = not measured yet.")
    return "\n".join(L)


def save_self_report(account_id: int, values: dict, period_end: date, proof_name: str | None, proof_bytes: bytes | None,
                     path=None) -> int:
    """Store numbers typed in from a creator's screenshot, with the screenshot file (kept under data/uploads)."""
    proof = None
    if proof_bytes:
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        safe = "".join(c for c in (proof_name or "screenshot.png") if c.isalnum() or c in "._-")[-60:]
        fn = f"{int(account_id)}_{period_end.isoformat()}_{safe}"
        (UPLOAD_DIR / fn).write_bytes(proof_bytes)
        proof = f"uploads/{fn}"
    cols = ["reels_counted", "avg_reach", "avg_views", "avg_likes", "avg_comments", "avg_saves", "avg_shares", "story_avg_views", "story_link_clicks"]
    vals = [values.get(c) for c in cols]
    con = db.connect(path)
    try:
        cur = con.execute(f"""INSERT OR REPLACE INTO insight_snapshot (account_id, captured_at, source, {', '.join(cols)}, proof_file, note)
                              VALUES (?,?,?,{','.join('?' * len(cols))},?,?)""",
                          (int(account_id), period_end.isoformat(), "self_reported", *vals, proof,
                           "typed in from a screenshot the creator sent"))
        con.execute("""INSERT INTO import_log (started_at, connector, mode, accounts, items, est_cost_usd, status, message)
                       VALUES (?,?,?,?,?,?,?,?)""", (datetime.now().isoformat(timespec="seconds"), "self_reported", "manual", 1, 1, 0.0,
                                                     "ok", f"screenshot numbers for account {int(account_id)}"))
        con.commit()
        return cur.lastrowid
    finally:
        con.close()
