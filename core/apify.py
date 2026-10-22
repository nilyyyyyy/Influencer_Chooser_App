"""Apify connector: public Instagram numbers for the creators in the database.

Two official Apify actors are used:
  * apify/instagram-profile-scraper  -> followers, bio, business account (1 result per creator)
  * apify/instagram-reel-scraper     -> the latest reels with plays, likes, comments, paid-partnership label
                                        (1 result per reel)

What public scraping can NOT see: reach, saves, shares, story views and link clicks. Those only exist in the
creator's own Instagram insights (Meta API with the creator's permission, or a screenshot), see core.meta_api.

How it runs:
  * Monthly: an Apify schedule runs a saved task on the 1st of each month (set up once in the Apify console).
    The app then imports the last finished run (`import_last_run`); reading a finished dataset costs no credits.
  * On demand: `refresh_now` starts both actors for a list of handles and waits for the results. This spends
    credits, so the app only calls it after an explicit confirmation and passes a hard cost cap.

Token: APIFY_TOKEN from Streamlit secrets or the environment, or typed into the app for one session. Never in the repo.
Pricing (Apify free plan, Oct 2026): about USD 0.0026 per result and USD 5 of free usage per month.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime

import numpy as np
import pandas as pd

from . import db

BASE_URL = "https://api.apify.com/v2"
PROFILE_ACTOR = "apify~instagram-profile-scraper"
REEL_ACTOR = "apify~instagram-reel-scraper"
PRICE_PER_RESULT_USD = 0.0026            # free plan rate for both actors
FREE_MONTHLY_USD = 5.0
REELS_PER_ACCOUNT = 12
CONFIDENCE = 0.9                          # public numbers straight from the platform, but no fake-follower audit


class ApifyError(RuntimeError):
    pass


# ----------------------------------------------------------------------------- cost
def estimate_cost(n_accounts: int, reels_per_account: int = REELS_PER_ACCOUNT) -> dict:
    results = n_accounts * (1 + reels_per_account)
    usd = results * PRICE_PER_RESULT_USD
    return {"accounts": n_accounts, "results": results, "usd": round(usd, 3), "share_of_free_plan": usd / FREE_MONTHLY_USD}


# ----------------------------------------------------------------------------- HTTP client
class ApifyClient:
    """Minimal Apify REST client. The token goes into the Authorization header, never into a URL or a log."""

    def __init__(self, token: str, base_url: str = BASE_URL, timeout: float = 320.0):
        if not token:
            raise ApifyError("No Apify token. Add APIFY_TOKEN to Streamlit secrets or type it into the Data sources page.")
        self.token, self.base_url, self.timeout = token.strip(), base_url.rstrip("/"), timeout

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}

    def _check(self, r) -> list | dict:
        if r.status_code >= 400:
            try:
                msg = r.json().get("error", {}).get("message", "")
            except ValueError:
                msg = r.text[:200]
            hints = {401: "Apify rejected the token (401).", 402: "Not enough Apify credits (402).",
                     403: "Apify refused the request (403).", 404: "Actor, task or run not found (404).",
                     408: "The Apify run took too long (408)."}
            raise ApifyError(f"{hints.get(r.status_code, f'Apify error {r.status_code}.')} {msg}".strip())
        try:
            return r.json()
        except ValueError as e:
            raise ApifyError("Apify returned no JSON.") from e

    def run_sync(self, actor: str, run_input: dict, max_charge_usd: float) -> list[dict]:
        """Start an actor, wait for it and return its dataset items. SPENDS CREDITS (capped by max_charge_usd)."""
        import requests
        r = requests.post(f"{self.base_url}/acts/{actor}/run-sync-get-dataset-items", headers=self._headers(),
                          params={"maxTotalChargeUsd": f"{max_charge_usd:.2f}", "timeout": int(self.timeout - 20)},
                          data=json.dumps(run_input), timeout=self.timeout)
        out = self._check(r)
        return out if isinstance(out, list) else out.get("items", [])

    def last_run_items(self, actor_or_task: str, is_task: bool = False) -> list[dict]:
        """Items of the last successful run of an actor or a saved task (for example the monthly schedule). No credits."""
        import requests
        kind = "actor-tasks" if is_task else "acts"
        r = requests.get(f"{self.base_url}/{kind}/{actor_or_task}/runs/last/dataset/items", headers=self._headers(),
                         params={"status": "SUCCEEDED", "clean": "true"}, timeout=60)
        out = self._check(r)
        return out if isinstance(out, list) else out.get("items", [])

    def last_run_date(self, actor_or_task: str, is_task: bool = False) -> date | None:
        """When the last successful run finished (no credits). None if there is no such run."""
        import requests
        kind = "actor-tasks" if is_task else "acts"
        r = requests.get(f"{self.base_url}/{kind}/{actor_or_task}/runs/last", headers=self._headers(),
                         params={"status": "SUCCEEDED"}, timeout=30)
        if r.status_code == 404:
            return None
        fin = (self._check(r).get("data") or {}).get("finishedAt")
        return date.fromisoformat(fin[:10]) if fin else None

    def whoami(self) -> dict:
        """Check the token (no credits)."""
        import requests
        return self._check(requests.get(f"{self.base_url}/users/me", headers=self._headers(), timeout=30)).get("data", {})


# ----------------------------------------------------------------------------- parsing
def parse_profiles(items: list[dict]) -> dict[str, dict]:
    out = {}
    for it in items or []:
        u = (it.get("username") or "").lower()
        if not u or it.get("error"):
            continue
        out[u] = {"followers": it.get("followersCount"), "biography": it.get("biography") or "", "full_name": it.get("fullName") or "",
                  "is_business": it.get("isBusinessAccount"), "verified": bool(it.get("verified")), "private": bool(it.get("private")),
                  "posts": it.get("postsCount")}
    return out


def _ts(x) -> str | None:
    if not x:
        return None
    try:
        return datetime.fromisoformat(str(x).replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return None


def parse_reels(items: list[dict]) -> list[dict]:
    out = []
    for it in items or []:
        if it.get("error"):
            continue
        views = it.get("videoPlayCount") or it.get("videoViewCount")
        if views is None or not it.get("ownerUsername"):
            continue
        out.append({"username": it["ownerUsername"].lower(), "external_id": str(it.get("id") or it.get("shortCode")),
                    "url": it.get("url"), "posted_at": _ts(it.get("timestamp")), "views": int(views),
                    "likes": max(int(it.get("likesCount") or 0), 0),     # -1 when likes are hidden
                    "comments": int(it.get("commentsCount") or 0), "caption": (it.get("caption") or "")[:2000],
                    "hashtags": " ".join(f"#{h}" for h in (it.get("hashtags") or [])),
                    "sponsored": bool(it.get("paidPartnership")) or bool(it.get("sponsors"))})
    return out


def build_snapshot(profile: dict, reels: list[dict]) -> dict | None:
    """Monthly snapshot from public data. Averages use organic reels only (as the agency does); engagement here is
    (likes + comments), because saves and shares are not public."""
    if not profile or not profile.get("followers"):
        return None
    organic = [r for r in reels if not r["sponsored"]] or reels
    f = int(profile["followers"])
    if organic:
        views = float(np.mean([r["views"] for r in organic]))
        inter = float(np.mean([r["likes"] + r["comments"] for r in organic]))
        dates = sorted(pd.to_datetime([r["posted_at"] for r in reels if r["posted_at"]]))
        span_weeks = max((dates[-1] - dates[0]).days / 7, 1) if len(dates) >= 2 else None
        ppw = round(len(dates) / span_weeks, 1) if span_weeks else None
    else:
        views, inter, ppw = None, None, None
    return {"followers": f, "avg_views": int(views) if views else None,
            "engagement_rate": round(inter / f * 100, 3) if inter is not None and f else None,
            "engagement_per_view": round(inter / views * 100, 2) if inter is not None and views else None,
            "posts_per_week": ppw, "reels": len(reels), "organic_reels": len([r for r in reels if not r["sponsored"]])}


# ----------------------------------------------------------------------------- writing to the database
@dataclass
class ImportReport:
    mode: str
    accounts: int = 0
    snapshots: int = 0
    posts: int = 0
    skipped: list[str] = field(default_factory=list)
    est_cost_usd: float | None = None
    captured_at: str | None = None

    def message(self) -> str:
        s = f"{self.snapshots} snapshot(s), {self.posts} new post(s) for {self.accounts} account(s)"
        return s + (f"; skipped: {', '.join(self.skipped)}" if self.skipped else "")


def instagram_accounts(path=None, real_only: bool = False) -> pd.DataFrame:
    q = """SELECT a.id AS account_id, a.handle, a.influencer_id, i.display_name, i.origin FROM account a
           JOIN influencer i ON i.id = a.influencer_id WHERE a.platform = 'instagram'"""
    if real_only:
        q += " AND i.origin = 'real_public'"
    return db.read(q, path=path)


def import_results(profile_items: list[dict], reel_items: list[dict], captured_at: date | None = None, mode: str = "live",
                   est_cost_usd: float | None = None, path=None) -> ImportReport:
    """Store Apify results: one account_snapshot per matched Instagram account (source 'apify'), new reels as
    content items (skipping ones already imported), an import_log row, and move the data's 'today' forward."""
    captured = (captured_at or date.today()).isoformat()
    profiles, reels = parse_profiles(profile_items), parse_reels(reel_items)
    accounts = instagram_accounts(path)
    by_handle = {h.lower(): r for h, r in zip(accounts.handle, accounts.itertuples())}
    rep = ImportReport(mode=mode, est_cost_usd=est_cost_usd, captured_at=captured)
    con = db.connect(path)
    try:
        for user in sorted(set(profiles) | {r["username"] for r in reels}):
            acc = by_handle.get(user)
            if acc is None:
                rep.skipped.append(f"@{user} (not in database)")
                continue
            mine = [r for r in reels if r["username"] == user]
            snap = build_snapshot(profiles.get(user, {}), mine)
            prev = con.execute("""SELECT * FROM account_snapshot WHERE account_id = ? ORDER BY captured_at DESC LIMIT 1""",
                               (int(acc.account_id),)).fetchone()
            if snap is None and prev is None:
                rep.skipped.append(f"@{user} (no profile data)")
                continue
            rep.accounts += 1
            if snap is not None:
                followers = snap["followers"]
                avg_views = snap["avg_views"] or (prev["avg_views"] if prev else 0)
                er = snap["engagement_rate"] if snap["engagement_rate"] is not None else (prev["engagement_rate"] if prev else 0.0)
                fake = prev["fake_follower_pct"] if prev else 5.0        # Apify cannot measure fake followers: carry over
                ppw = snap["posts_per_week"] or (prev["posts_per_week"] if prev else 0.0)
                con.execute("""INSERT OR REPLACE INTO account_snapshot (account_id, captured_at, followers, avg_views, engagement_rate,
                                   fake_follower_pct, posts_per_week, source, confidence) VALUES (?,?,?,?,?,?,?,?,?)""",
                            (int(acc.account_id), captured, int(followers), int(avg_views), float(er), float(fake), float(ppw),
                             "apify", CONFIDENCE))
                rep.snapshots += 1
            for r in mine:
                cur = con.execute("""INSERT OR IGNORE INTO content_item (account_id, posted_at, format, caption, hashtags, views, likes,
                                         comments, is_sponsored, brand_id, is_disclosed, external_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                                  (int(acc.account_id), r["posted_at"] or captured, "reel", r["caption"], r["hashtags"], r["views"],
                                   r["likes"], r["comments"], int(r["sponsored"]), None, int(r["sponsored"]) if r["sponsored"] else None,
                                   f"ig:{r['external_id']}"))
                rep.posts += cur.rowcount
            bio = profiles.get(user, {}).get("biography")
            if bio and acc.origin == "real_public":
                con.execute("UPDATE influencer SET bio_text = ? WHERE id = ?", (bio, int(acc.influencer_id)))
        status = "ok" if rep.accounts and not rep.skipped else ("partial" if rep.accounts else "failed")
        con.execute("""INSERT INTO import_log (started_at, connector, mode, accounts, items, est_cost_usd, status, message)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (datetime.now().isoformat(timespec="seconds"), "apify", mode, rep.accounts, rep.snapshots + rep.posts,
                     est_cost_usd, status, rep.message()))
        if rep.snapshots:
            cur_as_of = con.execute("SELECT value FROM meta WHERE key = 'as_of'").fetchone()
            if cur_as_of is None or cur_as_of[0] < captured:
                con.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('as_of', ?)", (captured,))
        con.commit()
    finally:
        con.close()
    return rep


# ----------------------------------------------------------------------------- the two ways to run
def refresh_now(client: ApifyClient, handles: list[str], *, confirm_spend: bool, max_charge_usd: float | None = None,
                reels_per_account: int = REELS_PER_ACCOUNT, captured_at: date | None = None, path=None) -> ImportReport:
    """Run both actors now for `handles` and import the results. Spends Apify credits, so it refuses to run unless
    `confirm_spend` is True, and caps each actor run at `max_charge_usd` (default: 1.5x the estimate)."""
    if not confirm_spend:
        raise ApifyError("Live Apify runs spend credits and need an explicit confirmation.")
    handles = [h.strip().lstrip("@").lower() for h in handles if h and h.strip()]
    if not handles:
        raise ApifyError("No Instagram handles to refresh.")
    est = estimate_cost(len(handles), reels_per_account)
    cap = max_charge_usd if max_charge_usd is not None else max(round(est["usd"] * 1.5, 2), 0.05)
    profiles = client.run_sync(PROFILE_ACTOR, {"usernames": handles}, cap)
    reels = client.run_sync(REEL_ACTOR, {"username": handles, "resultsLimit": reels_per_account}, cap)
    return import_results(profiles, reels, captured_at, "live", est["usd"], path)


def import_last_run(client: ApifyClient, profile_source: str = PROFILE_ACTOR, reel_source: str = REEL_ACTOR,
                    is_task: bool = False, captured_at: date | None = None, path=None) -> ImportReport:
    """Import the latest finished runs (for example the monthly scheduled tasks). Reading datasets costs no credits.
    The snapshot is dated with the day the run finished, not the day of the import."""
    if captured_at is None:
        try:
            captured_at = client.last_run_date(profile_source, is_task)
        except ApifyError:
            captured_at = None
    profiles = client.last_run_items(profile_source, is_task)
    reels = client.last_run_items(reel_source, is_task)
    return import_results(profiles, reels, captured_at, "import_last_run", 0.0, path)


def schedule_instructions(handles: list[str]) -> str:
    """What to set up once in the Apify console for the monthly run (shown on the Data sources page)."""
    est = estimate_cost(len(handles))
    return (f"1. In Apify, open *Instagram Profile Scraper* and *Instagram Reel Scraper*, enter the {len(handles)} handles "
            f"(reel scraper: {REELS_PER_ACCOUNT} results per profile) and save each as a task.\n"
            "2. Create a schedule with the cron expression `0 6 1 * *` (06:00 on the 1st of every month) that runs both tasks.\n"
            "3. Put the two task IDs into the app secrets as `APIFY_PROFILE_TASK` and `APIFY_REEL_TASK`. The monthly refresh in the "
            "app then imports the last finished runs, which costs no credits.\n"
            f"Estimated cost per month: {est['results']} results, about USD {est['usd']:.2f} "
            f"({est['share_of_free_plan']:.0%} of the free plan's USD {FREE_MONTHLY_USD:.0f}).")


def auto_import(token: str, path=None) -> str:
    """Called by the app at most once a day when an Apify token is configured: pulls the newest finished scheduled run,
    so the numbers update by themselves after the monthly Apify schedule ran. Free; skipped if already imported."""
    from . import config
    client = ApifyClient(token)
    prof_task, reel_task = config.get_secret("APIFY_PROFILE_TASK"), config.get_secret("APIFY_REEL_TASK")
    src, rsrc, is_task = (prof_task, reel_task, True) if prof_task and reel_task else (PROFILE_ACTOR, REEL_ACTOR, False)
    finished = client.last_run_date(src, is_task)
    if finished is None:
        return "no finished Apify run yet"
    have = db.read("SELECT COUNT(*) n FROM account_snapshot WHERE source = 'apify' AND captured_at = ?", (finished.isoformat(),), path=path).n[0]
    if have:
        return f"run from {finished} already imported"
    return import_last_run(client, src, rsrc, is_task, finished, path).message()
