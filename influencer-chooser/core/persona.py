"""Persona: who the creator is as a person (life stage, family, situation), as free text.

Why free text and not fixed fields: briefs ask for very different things ("a mother", "expecting a baby",
"a couple", "a student", "a dad on parental leave"), and people's lives change. A description that is
redrafted whenever new posts come in, and that the agency can correct at any time, covers all of that.

How it is used (AI reads, code decides):
1. The brief profile has a free-text `creator_persona` requirement (extracted by the AI, editable).
2. `check()` asks the AI, once per requirement and persona version, whether each creator fits:
   "fits", "does_not_fit" or "unclear", with an exact quote from the description as evidence.
3. The code only excludes clear "does_not_fit" verdicts that carry a quote it can verify. "unclear" stays
   in the ranking with a note for a person to check. A person can put anyone back in (FilterSettings.persona_keep).
Without an API key, simple keyword rules give a verdict for parenthood requirements and "unclear" otherwise.
"""
from __future__ import annotations

import re
from datetime import datetime

import pandas as pd

from . import ai, db

VERDICTS = ("fits", "does_not_fit", "unclear")
VERDICT_LABELS = {"fits": "Fits", "does_not_fit": "Does not fit", "unclear": "Unclear, check by hand", None: "Not checked"}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ----------------------------------------------------------------------------- storage
def latest(path=None) -> dict[int, dict]:
    """Newest persona per influencer: {influencer_id: {id, description, source, based_on, created_at}}."""
    df = db.read("""SELECT p.* FROM persona p JOIN (SELECT influencer_id, MAX(id) AS m FROM persona GROUP BY influencer_id) l
                    ON l.m = p.id""", path=path)
    return {int(r.influencer_id): {"id": int(r.id), "description": r.description, "source": r.source,
                                   "based_on": r.based_on, "created_at": r.created_at} for r in df.itertuples()}


def history(influencer_id: int, path=None) -> pd.DataFrame:
    return db.read("SELECT id, created_at, source, based_on, description FROM persona WHERE influencer_id = ? ORDER BY id DESC",
                   (int(influencer_id),), path=path)


def save(influencer_id: int, description: str, source: str, based_on: str = "", path=None) -> int:
    """Store a new version. Older versions are kept (audit trail); the newest one is used."""
    description = (description or "").strip()
    if not description:
        raise ValueError("persona description is empty")
    if source not in ("seed", "ai", "rule", "agency"):
        raise ValueError(f"unknown persona source {source}")
    return db.execute("INSERT INTO persona (influencer_id, description, source, based_on, created_at) VALUES (?,?,?,?,?)",
                      (int(influencer_id), description, source, based_on, _now()), path=path)


# ----------------------------------------------------------------------------- rule-based verdict
PARENT_WORDS = ["parent", "mother", "mom", "mum", "father", "dad", "eltern", "mutter", "mama", "vater", "papa"]
YOUNG_WORDS = ["baby", "babies", "toddler", "newborn", "infant", "kleinkind", "säugling", "month-old"]
EXPECTING_WORDS = ["pregnan", "expecting", "schwanger"]
UNKNOWN_WORDS = ["no children mentioned", "not mentioned", "not confirmed"]
NO_KIDS_WORDS = ["no children", "no kids", "keine kinder", "not a parent", "childless"]


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.;!?])\s+", text or "") if s.strip()]


def _find(text: str, words: list[str]) -> str:
    """First sentence of `text` that contains one of `words` (case-insensitive), or ''."""
    for s in _sentences(text):
        low = s.lower()
        if any(w in low for w in words):
            return s.rstrip(";")
    return ""


def rule_verdict(requirement: str, description: str) -> dict:
    """Keyword verdict for parenthood requirements (the common case in the interview). Anything else: unclear."""
    req, desc = (requirement or "").lower(), (description or "")
    low = desc.lower()
    needs_parent = any(w in req for w in PARENT_WORDS + YOUNG_WORDS + EXPECTING_WORDS)
    if not needs_parent:
        return {"verdict": "unclear", "evidence": "", "reason": "Rules only judge parenthood requirements; use AI or check by hand.",
                "source": "rule"}
    needs_young = any(w in req for w in YOUNG_WORDS + EXPECTING_WORDS)
    young = _find(desc, YOUNG_WORDS) or (lambda m: m.group(0) if m else "")(re.search(r"[^.;]*\b[0-3]-year-old[^.;]*", desc))
    expecting = _find(desc, EXPECTING_WORDS)
    parent = _find(desc, PARENT_WORDS)
    no_kids = "" if any(w in low for w in UNKNOWN_WORDS[:1]) else _find(desc, NO_KIDS_WORDS)
    if no_kids:
        return {"verdict": "does_not_fit", "evidence": no_kids, "reason": "The description says the creator has no children.", "source": "rule"}
    explicit_young = re.search(r"month-old|newborn|\b[0-3]-year-old|toddler", low) or expecting
    if needs_young and not explicit_young and any(w in low for w in UNKNOWN_WORDS[1:]):
        return {"verdict": "unclear", "evidence": _find(desc, UNKNOWN_WORDS[1:]),
                "reason": "The children's ages are not confirmed in the description.", "source": "rule"}
    if needs_young:
        if young or expecting:
            return {"verdict": "fits", "evidence": (young or expecting).strip(), "reason": "Baby, toddler or pregnancy mentioned.", "source": "rule"}
        ages = ([int(a) for a in re.findall(r"\b(\d{1,2})-year-old", desc)]
                + [int(x) for pair in re.findall(r"\((\d{1,2}) and (\d{1,2})\)", desc) for x in pair])
        if parent and ("school-age" in low or (ages and min(ages) >= 4)):
            return {"verdict": "does_not_fit", "evidence": parent, "reason": "Parent, but the children are older than the brief asks for.", "source": "rule"}
        return {"verdict": "unclear", "evidence": parent, "reason": "Parenthood or children's ages are not clear from the description.", "source": "rule"}
    if parent or expecting:
        return {"verdict": "fits", "evidence": parent or expecting, "reason": "The description says the creator is a parent.", "source": "rule"}
    return {"verdict": "unclear", "evidence": "", "reason": "The description does not say whether the creator is a parent.", "source": "rule"}


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().strip('"“”\'').lower())


def validate(verdict: dict, description: str) -> dict:
    """Code-side guard on an AI verdict: a 'does_not_fit' only excludes a creator if its evidence is an exact quote
    from the description. Otherwise it is downgraded to 'unclear' so a person looks at it."""
    v = dict(verdict)
    if v.get("verdict") not in VERDICTS:
        v["verdict"] = "unclear"
    ev = (v.get("evidence") or "").strip()
    quoted = bool(ev) and _norm(ev) in _norm(description)
    if not quoted:
        v["evidence"] = ""
        if v["verdict"] == "does_not_fit":
            v["verdict"] = "unclear"
            v["reason"] = ((v.get("reason") or "") + " (No exact quote from the description, so not excluded automatically.)").strip()
    return v


# ----------------------------------------------------------------------------- checking a brief requirement
def check(requirement: str, client=None, path=None, influencer_ids: list[int] | None = None) -> tuple[dict[int, dict], dict]:
    """Verdict per influencer for one brief requirement. Cached in `persona_check` per persona version, so the AI is
    asked only for creators whose description is new. Returns (verdicts, info)."""
    requirement = (requirement or "").strip()
    info = {"asked_ai": 0, "rules": 0, "cached": 0, "error": None}
    if not requirement:
        return {}, info
    personas = latest(path)
    if influencer_ids is not None:
        personas = {k: v for k, v in personas.items() if k in set(influencer_ids)}
    cached = db.read("SELECT * FROM persona_check WHERE requirement = ?", (requirement,), path=path)
    by_pid = {int(r.persona_id): r for r in cached.itertuples()}
    out: dict[int, dict] = {}
    todo = []
    for iid, p in personas.items():
        r = by_pid.get(p["id"])
        if r is not None and (r.source == "ai" or client is None):
            out[iid] = {"verdict": r.verdict, "evidence": r.evidence or "", "reason": r.reason or "", "source": r.source}
            info["cached"] += 1
        else:
            todo.append((iid, p))
    if todo:
        new: dict[int, dict] = {}
        if client is not None:
            res = ai.check_personas(client, requirement, [{"influencer_id": iid, "description": p["description"]} for iid, p in todo])
            if res.source == "ai":
                for iid, p in todo:
                    if iid in res.data:
                        new[iid] = {**validate(res.data[iid], p["description"]), "source": "ai"}
                info["asked_ai"] = len(new)
            else:
                info["error"] = res.error
        for iid, p in todo:
            if iid not in new:
                new[iid] = rule_verdict(requirement, p["description"])
                info["rules"] += 1
        con = db.connect(path)
        try:
            for iid, v in new.items():
                con.execute("""INSERT OR REPLACE INTO persona_check (influencer_id, persona_id, requirement, verdict, evidence, reason,
                                                                     source, created_at) VALUES (?,?,?,?,?,?,?,?)""",
                            (iid, personas[iid]["id"], requirement, v["verdict"], v.get("evidence", ""), v.get("reason", ""),
                             v["source"], _now()))
            con.commit()
        finally:
            con.close()
        out.update(new)
    return out, info


# ----------------------------------------------------------------------------- redrafting from new content
def source_material(influencer_id: int, path=None, n_posts: int = 16) -> dict:
    """What a persona is written from: bio plus the latest organic captions (synthetic or imported from Apify)."""
    inf = db.read("SELECT display_name, bio_text, city, country FROM influencer WHERE id = ?", (int(influencer_id),), path=path)
    posts = db.read("""SELECT c.posted_at, c.caption FROM content_item c JOIN account a ON a.id = c.account_id
                       WHERE a.influencer_id = ? AND c.is_sponsored = 0 ORDER BY c.posted_at DESC LIMIT ?""",
                    (int(influencer_id), n_posts), path=path)
    r = inf.iloc[0]
    return {"name": r.display_name, "bio": r.bio_text, "location": ", ".join(x for x in (r.city, r.country) if isinstance(x, str)),
            "captions": [f"{p.posted_at}: {p.caption}" for p in posts.itertuples()],
            "latest_post": posts.posted_at.max() if len(posts) else None}


def redraft(influencer_id: int, client, path=None) -> tuple[str, str, str | None]:
    """Write a new persona version from the latest bio and posts, keeping what the previous version (possibly corrected
    by the agency) says unless the new posts contradict it. Returns (description, source, error)."""
    material = source_material(influencer_id, path)
    prev = latest(path).get(int(influencer_id), {})
    res = ai.draft_persona(client, material, prev.get("description", ""))
    based_on = f"bio and {len(material['captions'])} posts up to {material['latest_post'] or 'n/a'}"
    if res.source == "ai" and res.data:
        save(influencer_id, res.data, "ai", based_on, path)
        return res.data, "ai", None
    return prev.get("description", ""), "fallback", res.error or "AI needed to redraft"
