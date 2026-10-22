-- Influencer Chooser: database schema (SQLite)
-- All data in this repository is SYNTHETIC. See README.md and DATA_SOURCES.md.

PRAGMA foreign_keys = ON;

-- ------------------------------------------------------------------- meta
CREATE TABLE meta (
    key     TEXT PRIMARY KEY,
    value   TEXT NOT NULL
);

-- ---------------------------------------------------------------- taxonomy
CREATE TABLE niche (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    parent_id   INTEGER REFERENCES niche(id)
);

-- ------------------------------------------------------------- influencers
CREATE TABLE influencer (
    id                  INTEGER PRIMARY KEY,
    display_name        TEXT NOT NULL UNIQUE,
    country             TEXT NOT NULL,              -- ISO 3166-1 alpha-2
    city                TEXT,
    languages           TEXT NOT NULL,              -- comma separated ISO 639-1
    bio_text            TEXT NOT NULL,              -- input for embeddings
    tone                TEXT NOT NULL,              -- casual, humorous, educational, ...
    status              TEXT NOT NULL CHECK (status IN ('active','paused','offboarded')),
    availability        TEXT NOT NULL CHECK (availability IN ('open','limited','unavailable')),
    relationship_score  INTEGER NOT NULL CHECK (relationship_score BETWEEN 1 AND 5),
    agency_notes        TEXT,
    onboarded_at        DATE NOT NULL,
    consent_basis       TEXT NOT NULL CHECK (consent_basis IN ('contract','legitimate_interest')),
    consent_until       DATE NOT NULL,              -- GDPR retention limit
    profile_embedding   BLOB,                       -- filled by the app (NULL in the dataset)
    origin              TEXT NOT NULL DEFAULT 'synthetic'
                        CHECK (origin IN ('synthetic','real_public'))   -- real_public = real creator, public data only
);

CREATE TABLE influencer_niche (
    influencer_id   INTEGER NOT NULL REFERENCES influencer(id),
    niche_id        INTEGER NOT NULL REFERENCES niche(id),
    is_primary      INTEGER NOT NULL CHECK (is_primary IN (0,1)),
    PRIMARY KEY (influencer_id, niche_id)
);

CREATE TABLE account (
    id              INTEGER PRIMARY KEY,
    influencer_id   INTEGER NOT NULL REFERENCES influencer(id),
    platform        TEXT NOT NULL CHECK (platform IN ('instagram','tiktok','youtube')),
    handle          TEXT NOT NULL,
    url             TEXT NOT NULL,
    verified        INTEGER NOT NULL CHECK (verified IN (0,1)),
    UNIQUE (platform, handle)
);

-- Time series: one row per account per month-end.
-- engagement_rate = (likes + comments + shares + saves) / followers * 100 for ALL platforms,
-- so values are comparable across platforms. Snapshots from Apify (public data) only see likes and
-- comments, so their engagement_rate is (likes + comments) / followers * 100.
-- Engagement per view (the agency's definition) is derived: engagement_rate * followers / avg_views.
CREATE TABLE account_snapshot (
    id                  INTEGER PRIMARY KEY,
    account_id          INTEGER NOT NULL REFERENCES account(id),
    captured_at         DATE NOT NULL,
    followers           INTEGER NOT NULL,
    avg_views           INTEGER NOT NULL,
    engagement_rate     REAL NOT NULL,              -- percent of followers
    fake_follower_pct   REAL NOT NULL,              -- estimated share of fake/inactive followers
    posts_per_week      REAL NOT NULL,
    source              TEXT NOT NULL CHECK (source IN ('manual','csv_import','api','ai_estimated','apify','meta_api','self_reported')),
    confidence          REAL NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    UNIQUE (account_id, captured_at)
);

-- Quarterly audience description per account.
CREATE TABLE audience_snapshot (
    id              INTEGER PRIMARY KEY,
    account_id      INTEGER NOT NULL REFERENCES account(id),
    captured_at     DATE NOT NULL,
    female_pct      REAL NOT NULL,
    age_18_24       REAL NOT NULL,
    age_25_34       REAL NOT NULL,
    age_35_44       REAL NOT NULL,
    age_45_plus     REAL NOT NULL,
    top_countries   TEXT NOT NULL,                  -- JSON: [{"country":"AT","share":0.64}, ...]
    UNIQUE (account_id, captured_at)
);

-- ------------------------------------------------------------------ brands
CREATE TABLE brand (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    category    TEXT NOT NULL,
    hq_country  TEXT NOT NULL
);

CREATE TABLE brand_competitor (               -- stored in both directions
    brand_id        INTEGER NOT NULL REFERENCES brand(id),
    competitor_id   INTEGER NOT NULL REFERENCES brand(id),
    PRIMARY KEY (brand_id, competitor_id)
);

-- ---------------------------------------------------------------- content
CREATE TABLE content_item (
    id              INTEGER PRIMARY KEY,
    account_id      INTEGER NOT NULL REFERENCES account(id),
    posted_at       DATE NOT NULL,
    format          TEXT NOT NULL,
    caption         TEXT NOT NULL,
    hashtags        TEXT,
    views           INTEGER NOT NULL,
    likes           INTEGER NOT NULL,
    comments        INTEGER NOT NULL,
    is_sponsored    INTEGER NOT NULL CHECK (is_sponsored IN (0,1)),
    brand_id        INTEGER REFERENCES brand(id),
    is_disclosed    INTEGER CHECK (is_disclosed IN (0,1)),   -- NULL for organic posts
    embedding       BLOB,                                     -- filled by the app
    external_id     TEXT UNIQUE                               -- platform post id for imported posts (Apify), NULL for synthetic
);

-- ------------------------------------------------------------- commercials
CREATE TABLE rate_card (
    id                      INTEGER PRIMARY KEY,
    influencer_id           INTEGER NOT NULL REFERENCES influencer(id),
    platform                TEXT NOT NULL,
    deliverable             TEXT NOT NULL,
    price_eur               REAL NOT NULL,
    usage_rights_months     INTEGER NOT NULL,
    exclusivity_premium_pct REAL NOT NULL,                   -- surcharge per 30-day exclusivity window
    valid_from              DATE NOT NULL,
    valid_to                DATE NOT NULL
);

CREATE TABLE safety_flag (
    id              INTEGER PRIMARY KEY,
    influencer_id   INTEGER NOT NULL REFERENCES influencer(id),
    flag_type       TEXT NOT NULL,
    severity        INTEGER NOT NULL CHECK (severity BETWEEN 1 AND 3),
    source          TEXT NOT NULL,
    evidence_url    TEXT,
    flagged_at      DATE NOT NULL,
    expires_at      DATE
);

CREATE TABLE exclusivity (
    id              INTEGER PRIMARY KEY,
    influencer_id   INTEGER NOT NULL REFERENCES influencer(id),
    brand_id        INTEGER NOT NULL REFERENCES brand(id),
    valid_from      DATE NOT NULL,
    valid_to        DATE NOT NULL
);

-- ------------------------------------------------------ campaigns & results
CREATE TABLE campaign (
    id              INTEGER PRIMARY KEY,
    brand_id        INTEGER NOT NULL REFERENCES brand(id),
    name            TEXT NOT NULL,
    goal            TEXT NOT NULL CHECK (goal IN ('awareness','traffic','sales','app_installs')),
    budget_eur      REAL NOT NULL,
    start_date      DATE NOT NULL,
    end_date        DATE NOT NULL,
    target_country  TEXT NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('completed','running','planned'))
);

CREATE TABLE participation (
    id              INTEGER PRIMARY KEY,
    campaign_id     INTEGER NOT NULL REFERENCES campaign(id),
    influencer_id   INTEGER NOT NULL REFERENCES influencer(id),
    account_id      INTEGER NOT NULL REFERENCES account(id),
    deliverable     TEXT NOT NULL,
    fee_eur         REAL NOT NULL,
    reach_p10       INTEGER NOT NULL,                        -- forecast that was shown before the campaign
    reach_p50       INTEGER NOT NULL,
    reach_p90       INTEGER NOT NULL,
    model_version   TEXT NOT NULL,
    reach_naive     INTEGER                                  -- plain average-views forecast, the clean baseline for learning
);

CREATE TABLE result (
    id                  INTEGER PRIMARY KEY,
    participation_id    INTEGER NOT NULL UNIQUE REFERENCES participation(id),
    actual_reach        INTEGER NOT NULL,
    actual_engagement   INTEGER NOT NULL,
    conversions         INTEGER NOT NULL,                    -- goal specific: clicks, sales or installs
    delivered_on_time   INTEGER NOT NULL CHECK (delivered_on_time IN (0,1)),
    client_rating       INTEGER NOT NULL CHECK (client_rating BETWEEN 1 AND 5),
    measured_at         DATE NOT NULL
);

-- ----------------------------------------- audit trail written by the app
-- One row per saved decision. Weights, brief and profile are stored so the
-- recommendation can be reproduced and defended later.
CREATE TABLE run (
    id              INTEGER PRIMARY KEY,
    created_at      DATETIME NOT NULL,
    title           TEXT NOT NULL,
    campaign_id     INTEGER REFERENCES campaign(id),   -- set when converted into a campaign
    brand_id        INTEGER REFERENCES brand(id),
    objective       TEXT,
    budget_eur      REAL,
    weights_json    TEXT NOT NULL,
    profile_json    TEXT,
    brief_text      TEXT,
    notes           TEXT,
    model_version   TEXT NOT NULL
);

CREATE TABLE run_item (
    id                  INTEGER PRIMARY KEY,
    run_id              INTEGER NOT NULL REFERENCES run(id),
    influencer_id       INTEGER NOT NULL REFERENCES influencer(id),
    account_id          INTEGER REFERENCES account(id),
    rank                INTEGER,
    total_score         REAL NOT NULL,
    criterion_scores    TEXT NOT NULL,                       -- JSON
    selected            INTEGER NOT NULL DEFAULT 0,          -- 1 = part of the chosen mix
    deliverable         TEXT,
    fee_eur             REAL,
    reach_p10           INTEGER,
    reach_p50           INTEGER,
    reach_p90           INTEGER,
    reach_naive         INTEGER,
    ai_explanation      TEXT
);


-- ------------------------------------------------- persona (who the creator is)
-- Free-text description of the creator as a person: life stage, family, situation. Versioned: the
-- newest row per influencer is the one used. Drafted by the AI from bio and recent posts, written or
-- corrected by the agency, or seeded with the dataset. No fixed fields, so any brief requirement
-- (a mother, expecting a baby, a couple, a student ...) can be judged against it.
CREATE TABLE persona (
    id              INTEGER PRIMARY KEY,
    influencer_id   INTEGER NOT NULL REFERENCES influencer(id),
    description     TEXT NOT NULL,
    source          TEXT NOT NULL CHECK (source IN ('seed','ai','rule','agency')),
    based_on        TEXT,                                    -- what it was written from
    created_at      DATETIME NOT NULL
);

-- Cached persona-fit judgements: one per brief requirement and persona version, so the AI is asked once.
CREATE TABLE persona_check (
    id              INTEGER PRIMARY KEY,
    influencer_id   INTEGER NOT NULL REFERENCES influencer(id),
    persona_id      INTEGER NOT NULL REFERENCES persona(id),
    requirement     TEXT NOT NULL,
    verdict         TEXT NOT NULL CHECK (verdict IN ('fits','does_not_fit','unclear')),
    evidence        TEXT,                                    -- exact quote from the persona description
    reason          TEXT,
    source          TEXT NOT NULL CHECK (source IN ('ai','rule')),
    created_at      DATETIME NOT NULL,
    UNIQUE (persona_id, requirement)
);

-- --------------------------------------------- data connections (Apify, Meta)
-- Creators who connected their Instagram account to the agency app (Meta Instagram API, mocked in the demo).
CREATE TABLE meta_connection (
    account_id      INTEGER PRIMARY KEY REFERENCES account(id),
    connected_at    DATE NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('connected','expired','revoked')),
    scopes          TEXT NOT NULL
);

-- Private Instagram insights per account and month: numbers only the creator can see (reach, saves,
-- shares, story link clicks). From the Meta API, or typed in from a screenshot the creator sent.
CREATE TABLE insight_snapshot (
    id                  INTEGER PRIMARY KEY,
    account_id          INTEGER NOT NULL REFERENCES account(id),
    captured_at         DATE NOT NULL,                       -- end of the 30-day period
    source              TEXT NOT NULL CHECK (source IN ('meta_api','self_reported')),
    reels_counted       INTEGER,
    avg_reach           REAL,
    avg_views           REAL,
    avg_likes           REAL,
    avg_comments        REAL,
    avg_saves           REAL,
    avg_shares          REAL,
    story_avg_views     REAL,
    story_link_clicks   REAL,                                -- average link clicks per story with a link
    proof_file          TEXT,                                -- screenshot file for self-reported numbers
    note                TEXT,
    UNIQUE (account_id, captured_at, source)
);

-- Insights of the posts made for a campaign: Meta API (mocked) at 1, 7 and 30 days after posting.
CREATE TABLE post_insight (
    id                  INTEGER PRIMARY KEY,
    participation_id    INTEGER NOT NULL REFERENCES participation(id),
    media_type          TEXT NOT NULL CHECK (media_type IN ('REELS','STORY','FEED')),
    posted_at           DATE,
    captured_at         DATE NOT NULL,
    days_after_post     INTEGER,
    reach               INTEGER,
    views               INTEGER,
    likes               INTEGER,
    comments            INTEGER,
    saves               INTEGER,
    shares              INTEGER,
    link_clicks         INTEGER,
    source              TEXT NOT NULL CHECK (source IN ('meta_api','self_reported'))
);

-- Every data import (Apify run, Meta refresh, manual entry), for the audit trail and the Data sources page.
CREATE TABLE import_log (
    id              INTEGER PRIMARY KEY,
    started_at      DATETIME NOT NULL,
    connector       TEXT NOT NULL,                           -- apify, meta_api, self_reported, persona
    mode            TEXT NOT NULL,                           -- live, mock, import_last_run, manual
    accounts        INTEGER NOT NULL DEFAULT 0,
    items           INTEGER NOT NULL DEFAULT 0,
    est_cost_usd    REAL,
    status          TEXT NOT NULL CHECK (status IN ('ok','partial','failed')),
    message         TEXT
);

-- ------------------------------------------------------------------ indexes
CREATE INDEX idx_account_influencer   ON account(influencer_id);
CREATE INDEX idx_snapshot_account     ON account_snapshot(account_id, captured_at);
CREATE INDEX idx_audience_account     ON audience_snapshot(account_id, captured_at);
CREATE INDEX idx_content_account      ON content_item(account_id, posted_at);
CREATE INDEX idx_content_brand        ON content_item(brand_id);
CREATE INDEX idx_rate_influencer      ON rate_card(influencer_id);
CREATE INDEX idx_participation_camp   ON participation(campaign_id);
CREATE INDEX idx_participation_inf    ON participation(influencer_id);
CREATE INDEX idx_persona_influencer   ON persona(influencer_id, created_at);
CREATE INDEX idx_insight_account      ON insight_snapshot(account_id, captured_at);
CREATE INDEX idx_post_insight_part    ON post_insight(participation_id);

-- -------------------------------------------------------------------- views
-- Latest snapshot per account.
CREATE VIEW v_latest_snapshot AS
SELECT s.*
FROM account_snapshot s
JOIN (SELECT account_id, MAX(captured_at) AS m
      FROM account_snapshot GROUP BY account_id) l
  ON l.account_id = s.account_id AND l.m = s.captured_at;

-- Reliability and prediction bias per influencer, from completed campaigns.
CREATE VIEW v_influencer_reliability AS
SELECT p.influencer_id,
       COUNT(*)                                  AS n_campaigns,
       AVG(r.delivered_on_time)                  AS on_time_rate,
       AVG(r.client_rating)                      AS avg_client_rating,
       AVG(1.0 * r.actual_reach / p.reach_p50)   AS avg_actual_vs_predicted
FROM participation p
JOIN result r ON r.participation_id = p.id
GROUP BY p.influencer_id;

-- Prediction error per participation (input for the calibration page).
CREATE VIEW v_prediction_error AS
SELECT p.id AS participation_id, p.influencer_id, p.campaign_id,
       p.reach_p10, p.reach_p50, p.reach_p90, r.actual_reach,
       1.0 * r.actual_reach / p.reach_p50                                    AS ratio,
       CASE WHEN r.actual_reach BETWEEN p.reach_p10 AND p.reach_p90 THEN 1 ELSE 0 END AS inside_p10_p90
FROM participation p
JOIN result r ON r.participation_id = p.id;

-- Brand conflicts: (a) influencer promoted a competitor in the 90 days before the
-- campaign start, (b) influencer is under an exclusivity contract with a competitor
-- that overlaps the campaign window.
CREATE VIEW v_campaign_conflicts AS
SELECT c.id AS campaign_id, a.influencer_id, 'competitor_promo_90d' AS conflict_type,
       ci.brand_id AS conflicting_brand_id, ci.posted_at AS evidence_date
FROM campaign c
JOIN brand_competitor bc ON bc.brand_id = c.brand_id
JOIN content_item ci     ON ci.brand_id = bc.competitor_id AND ci.is_sponsored = 1
JOIN account a           ON a.id = ci.account_id
WHERE julianday(c.start_date) - julianday(ci.posted_at) BETWEEN 0 AND 90
UNION ALL
SELECT c.id, e.influencer_id, 'exclusivity_contract', e.brand_id, e.valid_from
FROM campaign c
JOIN brand_competitor bc ON bc.brand_id = c.brand_id
JOIN exclusivity e       ON e.brand_id = bc.competitor_id
WHERE e.valid_from <= c.end_date AND e.valid_to >= c.start_date;
