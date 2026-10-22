"""Monthly data refresh: the job that runs on the 1st of every month.

1. Apify (public numbers): import the last finished run of the scheduled Apify tasks. Free, because the scheduled run
   already happened in Apify. Live runs that spend credits are separate (core.apify.refresh_now) and need confirmation.
2. Meta Instagram API (private numbers, mocked): one insight snapshot per connected account for the past month.
3. Persona: creators with posts newer than their persona description get a redrafted description (AI only;
   without AI the old description stays and is listed as due).
"""
from __future__ import annotations

from datetime import date, timedelta

from . import apify, config, db, meta_api, persona


def month_end_before(d: date) -> date:
    return d.replace(day=1) - timedelta(days=1)


def personas_due(path=None) -> list[int]:
    """Influencers whose newest post is newer than their newest persona version."""
    df = db.read("""SELECT a.influencer_id, MAX(c.posted_at) AS last_post,
                           (SELECT MAX(p.created_at) FROM persona p WHERE p.influencer_id = a.influencer_id) AS persona_at
                    FROM content_item c JOIN account a ON a.id = c.account_id GROUP BY a.influencer_id""", path=path)
    df = df[df.persona_at.isna() | (df.last_post > df.persona_at.str[:10])]
    return [int(i) for i in df.influencer_id]


def monthly_refresh(period_end: date | None = None, *, apify_client=None, ai_client=None, redraft_personas: bool = True,
                    max_redrafts: int = 20, path=None) -> dict:
    period_end = period_end or month_end_before(date.today())
    out = {"period_end": period_end.isoformat(), "apify": None, "meta": None, "personas": None, "errors": []}
    if apify_client is not None:
        prof_task, reel_task = config.get_secret("APIFY_PROFILE_TASK"), config.get_secret("APIFY_REEL_TASK")
        try:
            if prof_task and reel_task:
                rep = apify.import_last_run(apify_client, prof_task, reel_task, is_task=True, path=path)
            else:
                rep = apify.import_last_run(apify_client, path=path)
            out["apify"] = rep.message()
        except Exception as e:                          # no run yet, bad token, network
            out["errors"].append(f"Apify import: {e}")
    else:
        out["apify"] = "skipped (no Apify token)"
    try:
        out["meta"] = meta_api.monthly_refresh(period_end, path=path)
    except Exception as e:
        out["errors"].append(f"Meta API: {e}")
    due = personas_due(path)
    done = 0
    if redraft_personas and ai_client is not None:
        for iid in due[:max_redrafts]:
            _, src, err = persona.redraft(iid, ai_client, path)
            done += src == "ai"
            if err:
                out["errors"].append(f"Persona {iid}: {err}")
                break
    out["personas"] = {"due": len(due), "redrafted": done}
    return out
