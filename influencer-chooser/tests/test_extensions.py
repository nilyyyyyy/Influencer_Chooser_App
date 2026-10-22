"""Interview features: engagement floor, persona check, Apify connector (fake HTTP, no credits), Meta mock, stats pack."""
import json
from datetime import date

import pytest

from core import ai, apify, config, db, meta_api, persona, refresh, scoring, statspack

KLEIN = open(config.BRIEF_DIR / "brief_kleinstern.md", encoding="utf-8").read()
REQ = "The creators should be parents themselves, with a baby or toddler, or expecting a baby"


def _run(**kw):
    p = ai.rule_based_profile(KLEIN)
    v, _ = persona.check(p["creator_persona"])
    s = scoring.FilterSettings(countries=p["target_countries"], budget_eur=p["budget_eur"], client_brand_id=38, category="baby_family",
                               excluded_flags=p["restrictions"]["excluded_flag_types"], forecast_model="tier", **kw)
    return scoring.run_pipeline(db.as_of(), s, p, config.PRESETS["Balanced"], KLEIN, persona_verdicts=v)


# --------------------------------------------------------------------------- engagement floor
def test_engagement_floor_basis_changes_the_pool(fresh_db):
    view, foll, off = _run(), _run(er_basis="follower"), _run(min_engagement_pct=0)
    assert len(off["ranked"]) >= len(view["ranked"]) > len(foll["ranked"])
    assert (view["ranked"].er_view >= 2).all() and (foll["ranked"].er >= 2).all()
    assert "engagement below 2% (per follower)" in set(foll["excluded"].exclude_reason)


# --------------------------------------------------------------------------- persona
def test_rule_based_persona_requirement_from_brief():
    assert "parents themselves" in ai.rule_based_profile(KLEIN)["creator_persona"]
    assert ai.rule_based_profile("# X\nBudget 1000 EUR. Audience: parents in Austria.")["creator_persona"] == ""


@pytest.mark.parametrize("desc,verdict", [
    ("No children of her own; works as an educator.", "does_not_fit"),
    ("Mother of two school-age kids (8 and 11).", "does_not_fit"),
    ("Father of a 4-month-old son, on parental leave.", "fits"),
    ("Expecting her first child (due January 2027).", "fits"),
    ("Mother of a young son. The child's exact age is not confirmed yet.", "unclear"),
    ("No children mentioned; lives with her partner.", "unclear"),
])
def test_rule_verdicts(desc, verdict):
    assert persona.rule_verdict(REQ, desc)["verdict"] == verdict


def test_rules_do_not_judge_other_requirements():
    assert persona.rule_verdict("a student in their early twenties", "Student; no children")["verdict"] == "unclear"


def test_ai_does_not_fit_without_exact_quote_is_downgraded():
    d = "Mother of a 3-year-old daughter."
    v = persona.validate({"verdict": "does_not_fit", "evidence": "has no kids", "reason": "x"}, d)
    assert v["verdict"] == "unclear" and v["evidence"] == ""
    v = persona.validate({"verdict": "does_not_fit", "evidence": "3-year-old daughter", "reason": "too old"}, d)
    assert v["verdict"] == "does_not_fit"


def test_persona_check_is_cached_and_uses_ai_once(fresh_db):
    class Fake:
        calls = 0

        def complete_tool(self, tool, user, max_tokens=3000):
            Fake.calls += 1
            facts = json.loads(user.split("<facts>")[1].split("</facts>")[0])
            return {"checks": [{"influencer_id": f["influencer_id"], "verdict": "unclear", "evidence": "", "reason": "test"} for f in facts]}
    v1, i1 = persona.check("a dog owner", Fake())
    v2, i2 = persona.check("a dog owner", Fake())
    assert i1["asked_ai"] == len(v1) == 82 and i2["cached"] == 82 and Fake.calls == 3      # 82 personas in chunks of 40
    persona.save(82, "Mother of a newborn and owner of a dog.", "agency")
    _, i3 = persona.check("a dog owner", Fake())
    assert i3["asked_ai"] == 1                                                              # only the new version is asked


def test_persona_keep_overrides_verdict(fresh_db):
    out = _run(persona_keep=[17])
    assert "Franziska Auer" in set(out["ranked"].name)


def test_explanations_have_content_angle(fresh_db):
    out = _run()
    res = ai.explain_candidates(None, out["ranked"], ai.rule_based_profile(KLEIN), 5)
    assert all(e["content_angle"] for e in res.data.values())


# --------------------------------------------------------------------------- Apify (no network, no credits)
PROFILES = [{"username": "annabbzn", "followersCount": 18000, "biography": "Mama in Hamburg", "fullName": "Anna", "verified": False},
            {"username": "someone_else", "followersCount": 5}]
REELS = [{"ownerUsername": "annabbzn", "id": str(i), "url": f"https://www.instagram.com/p/{i}/", "timestamp": f"2026-10-{i + 1:02d}T10:00:00.000Z",
          "videoPlayCount": 6000 + 100 * i, "likesCount": 300, "commentsCount": 20, "caption": "Abendroutine", "hashtags": ["mama"],
          "paidPartnership": i == 0} for i in range(6)]


def test_apify_parse_and_import(fresh_db):
    snap = apify.build_snapshot(apify.parse_profiles(PROFILES)["annabbzn"], apify.parse_reels(REELS))
    assert snap["organic_reels"] == 5 and snap["engagement_per_view"] == pytest.approx(320 / 6300 * 100, rel=0.01)
    rep = apify.import_results(PROFILES, REELS, date(2026, 10, 20))
    assert rep.snapshots == 1 and rep.posts == 6 and any("someone_else" in s for s in rep.skipped)
    last = db.read("SELECT * FROM account_snapshot WHERE account_id = 119 ORDER BY captured_at DESC LIMIT 1").iloc[0]
    assert last.source == "apify" and last.followers == 18000
    assert db.as_of() == date(2026, 10, 20)
    assert apify.import_results(PROFILES, REELS, date(2026, 10, 20)).posts == 0          # no duplicate posts
    assert statspack.build(119).metrics[0].status == "public"


def test_apify_live_run_needs_confirmation_and_sends_cap(monkeypatch, fresh_db):
    import requests
    with pytest.raises(apify.ApifyError):
        apify.refresh_now(apify.ApifyClient("tok"), ["annabbzn"], confirm_spend=False)
    sent = []

    class R:
        status_code = 200

        def __init__(self, data):
            self._d = data

        def json(self):
            return self._d

    def post(url, headers=None, params=None, data=None, timeout=None):
        sent.append((url, headers, params, json.loads(data)))
        return R(PROFILES if "profile" in url else REELS)
    monkeypatch.setattr(requests, "post", post)
    rep = apify.refresh_now(apify.ApifyClient("tok"), ["@annabbzn"], confirm_spend=True)
    assert rep.snapshots == 1 and len(sent) == 2
    assert sent[0][1]["Authorization"] == "Bearer tok" and "tok" not in sent[0][0]          # token in header, not URL
    assert float(sent[0][2]["maxTotalChargeUsd"]) <= 0.10 and sent[1][3] == {"username": ["annabbzn"], "resultsLimit": 12}


def test_apify_cost_estimate():
    e = apify.estimate_cost(80)
    assert e["results"] == 1040 and e["usd"] < apify.FREE_MONTHLY_USD


# --------------------------------------------------------------------------- Meta mock and stats pack
def test_meta_mock_has_graph_api_shape_and_monthly_refresh(fresh_db):
    resp = meta_api.MockMetaClient().account_period_insights(5, "2026-10-31")
    m = resp["media"][0]["insights"]["data"][0]
    assert set(m) >= {"name", "period", "values", "id"}
    out = meta_api.monthly_refresh(date(2026, 10, 31))
    assert out["written"] == out["connected"] > 0
    assert meta_api.monthly_refresh(date(2026, 10, 31))["written"] == 0                    # idempotent


def test_campaign_insights_respect_checkpoints(fresh_db):
    from tests.test_app_pages import _campaign_with_connected_creators
    cid = _campaign_with_connected_creators()
    r1 = meta_api.collect_campaign_insights(cid)
    assert r1["fetched"] == 0 and r1["not_due"] == 8                                      # campaign has not started yet
    r2 = meta_api.collect_campaign_insights(cid, pretend_finished=True)
    assert r2["fetched"] == 8
    assert meta_api.collect_campaign_insights(cid, pretend_finished=True)["fetched"] == 0  # not fetched twice
    pid = int(db.read("SELECT participation_id FROM post_insight LIMIT 1").participation_id[0])
    pre = meta_api.prefill_result(pid)
    assert pre["actual_reach"] > 0 and pre["checkpoint_days"] == 30 and pre["link_clicks"] >= 0


def test_stats_pack_flags_inflated_self_report_and_lists_requests(fresh_db):
    truth = __import__("pandas").read_csv(config.ROOT / "data" / "ground_truth" / "planted_traps.csv")
    for aid in truth[truth.trap_type == "inflated_self_report"].entity_id:
        assert any("public average" in w for w in statspack.build(int(aid)).warnings)
    p = statspack.build(118)                                                               # valienchen: nothing private yet
    assert p.coverage()["missing"] > 0 and p.requests
    md = statspack.to_markdown(p)
    assert "Still to request" in md and "valienchen" in md


def test_self_report_is_saved_with_proof(fresh_db, tmp_path, monkeypatch):
    monkeypatch.setattr(statspack, "UPLOAD_DIR", tmp_path)
    statspack.save_self_report(118, {"avg_reach": 9000.0, "avg_views": 12000.0, "story_link_clicks": 40.0}, date(2026, 10, 15),
                               "shot.png", b"\x89PNG")
    p = statspack.build(118)
    assert any(m.status == "self_reported" for m in p.metrics) and p.proof_files and (tmp_path / p.proof_files[0].split("/")[1]).exists()


def test_monthly_refresh_runs_without_apify(fresh_db):
    out = refresh.monthly_refresh(date(2026, 10, 31))
    assert out["apify"].startswith("skipped") and out["meta"]["written"] > 0 and not out["errors"]
