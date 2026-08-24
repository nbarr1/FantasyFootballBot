-- mflbot schema.
--
-- Every table ships EMPTY. There are no seed rows, no sample players, no
-- default scoring weights. The correct initial state of this database is
-- "nothing known yet"; ingestion fills it from the real MFL API on the first
-- authenticated run.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------------------
-- League configuration, pulled from the API and treated as the single source
-- of truth for every downstream calculation.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS league_config (
    league_id        TEXT NOT NULL,
    season           INTEGER NOT NULL,
    fetched_at       TEXT NOT NULL,
    -- Verbatim league export, kept so a parser fix can be re-applied to data
    -- already fetched without another API call.
    raw_json         TEXT NOT NULL,
    name             TEXT,
    franchise_count  INTEGER,
    roster_size      INTEGER,
    starter_count    INTEGER,
    taxi_squad_size  INTEGER,
    injured_reserve  INTEGER,
    waiver_type      TEXT,          -- as reported by MFL; never inferred
    trade_deadline   TEXT,
    lineup_deadline  TEXT,
    PRIMARY KEY (league_id, season)
);

CREATE TABLE IF NOT EXISTS lineup_slots (
    league_id     TEXT NOT NULL,
    season        INTEGER NOT NULL,
    slot_index    INTEGER NOT NULL,
    slot_name     TEXT NOT NULL,     -- e.g. "QB", "RB", "FLEX"
    eligible_pos  TEXT NOT NULL,     -- comma-separated position codes
    min_starters  INTEGER NOT NULL,
    max_starters  INTEGER NOT NULL,
    PRIMARY KEY (league_id, season, slot_index)
);

CREATE TABLE IF NOT EXISTS franchises (
    league_id    TEXT NOT NULL,
    season       INTEGER NOT NULL,
    franchise_id TEXT NOT NULL,
    name         TEXT,
    division     TEXT,
    is_owner     INTEGER NOT NULL DEFAULT 0,  -- the user's own team
    bbid_budget  REAL,
    waiver_order INTEGER,
    PRIMARY KEY (league_id, season, franchise_id)
);

-- Parsed scoring rules. One row per (position group, event) rule component.
CREATE TABLE IF NOT EXISTS scoring_rules (
    league_id    TEXT NOT NULL,
    season       INTEGER NOT NULL,
    rule_index   INTEGER NOT NULL,
    positions    TEXT NOT NULL,      -- comma-separated, e.g. "QB,RB"
    event_code   TEXT NOT NULL,      -- MFL abbreviation, e.g. "PY"
    points_expr  TEXT NOT NULL,      -- verbatim expression from MFL
    range_expr   TEXT,               -- verbatim range, if any
    kind         TEXT NOT NULL,      -- per_unit | flat | unparsed
    coefficient  REAL,               -- NULL when kind = 'unparsed'
    range_low    REAL,
    range_high   REAL,
    PRIMARY KEY (league_id, season, rule_index)
);

-- Rules MFL returned that this parser could not interpret. Their presence
-- BLOCKS scoring-dependent features rather than being silently ignored.
CREATE TABLE IF NOT EXISTS scoring_rule_gaps (
    league_id   TEXT NOT NULL,
    season      INTEGER NOT NULL,
    rule_index  INTEGER NOT NULL,
    positions   TEXT,
    event_code  TEXT,
    points_expr TEXT,
    range_expr  TEXT,
    reason      TEXT NOT NULL,
    PRIMARY KEY (league_id, season, rule_index)
);

-- Catalogue of scoring-event abbreviations, from the allRules export, so event
-- codes are never hardcoded in this repository.
CREATE TABLE IF NOT EXISTS rule_definitions (
    event_code   TEXT PRIMARY KEY,
    short_name   TEXT,
    description  TEXT,
    is_player    INTEGER,
    is_team      INTEGER,
    is_coach     INTEGER,
    fetched_at   TEXT NOT NULL
);

-- ---------------------------------------------------------------------------
-- Player universe and league state
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS players (
    player_id   TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    position    TEXT,
    nfl_team    TEXT,
    status      TEXT,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_players_pos ON players (position);
CREATE INDEX IF NOT EXISTS idx_players_team ON players (nfl_team);

CREATE TABLE IF NOT EXISTS rosters (
    league_id     TEXT NOT NULL,
    season        INTEGER NOT NULL,
    franchise_id  TEXT NOT NULL,
    player_id     TEXT NOT NULL,
    roster_status TEXT,              -- ROSTER | TAXI_SQUAD | INJURED_RESERVE
    snapshot_at   TEXT NOT NULL,
    PRIMARY KEY (league_id, season, franchise_id, player_id, snapshot_at)
);
CREATE INDEX IF NOT EXISTS idx_rosters_current
    ON rosters (league_id, season, snapshot_at DESC);

CREATE TABLE IF NOT EXISTS free_agents (
    league_id   TEXT NOT NULL,
    season      INTEGER NOT NULL,
    player_id   TEXT NOT NULL,
    snapshot_at TEXT NOT NULL,
    PRIMARY KEY (league_id, season, player_id, snapshot_at)
);

CREATE TABLE IF NOT EXISTS scores (
    league_id  TEXT NOT NULL,
    season     INTEGER NOT NULL,
    player_id  TEXT NOT NULL,
    week       INTEGER NOT NULL,
    points     REAL NOT NULL,        -- as reported by MFL, under league scoring
    is_final   INTEGER NOT NULL DEFAULT 0,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (league_id, season, player_id, week)
);

CREATE TABLE IF NOT EXISTS projections (
    league_id  TEXT NOT NULL,
    season     INTEGER NOT NULL,
    player_id  TEXT NOT NULL,
    week       INTEGER NOT NULL,
    source     TEXT NOT NULL,        -- e.g. "mfl_projectedScores"
    points     REAL NOT NULL,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (league_id, season, player_id, week, source)
);

CREATE TABLE IF NOT EXISTS transactions (
    league_id      TEXT NOT NULL,
    season         INTEGER NOT NULL,
    transaction_id TEXT NOT NULL,
    timestamp      TEXT NOT NULL,
    trans_type     TEXT,
    franchise_id   TEXT,
    raw_json       TEXT NOT NULL,
    seen_at        TEXT NOT NULL,
    PRIMARY KEY (league_id, season, transaction_id)
);
CREATE INDEX IF NOT EXISTS idx_tx_time ON transactions (league_id, season, timestamp DESC);

CREATE TABLE IF NOT EXISTS news_items (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source        TEXT NOT NULL,
    external_id   TEXT,
    player_id     TEXT,
    player_name   TEXT,
    published_at  TEXT,
    ingested_at   TEXT NOT NULL,
    classification TEXT,             -- injury | usage | depth_chart | other
    headline      TEXT,
    body          TEXT,
    url           TEXT,
    UNIQUE (source, external_id)
);
CREATE INDEX IF NOT EXISTS idx_news_player ON news_items (player_id, published_at DESC);

-- ---------------------------------------------------------------------------
-- Recommendations, approvals, audit
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS recommendations (
    id            TEXT PRIMARY KEY,
    kind          TEXT NOT NULL,     -- lineup | add_drop | trade_proposal | trade_response
    created_at    TEXT NOT NULL,
    expires_at    TEXT NOT NULL,
    status        TEXT NOT NULL,     -- proposed|approved|rejected|executed|failed|expired
    -- The literal payload that would be sent to MFL, canonically serialised.
    payload_json  TEXT NOT NULL,
    payload_hash  TEXT NOT NULL,
    rationale     TEXT NOT NULL,
    evidence_json TEXT NOT NULL,     -- the data behind the rationale
    confidence    TEXT NOT NULL,     -- high | medium | low
    caveats_json  TEXT NOT NULL,
    decided_at    TEXT,
    executed_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_rec_status ON recommendations (status, expires_at);

CREATE TABLE IF NOT EXISTS approval_tokens (
    token_id          TEXT PRIMARY KEY,
    recommendation_id TEXT NOT NULL REFERENCES recommendations(id),
    payload_hash      TEXT NOT NULL,  -- binds the token to an exact payload
    signature         TEXT NOT NULL,
    issued_at         TEXT NOT NULL,
    expires_at        TEXT NOT NULL,
    consumed_at       TEXT,           -- non-NULL once used; tokens are single-use
    approved_by       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    at                TEXT NOT NULL,
    recommendation_id TEXT,
    token_id          TEXT,
    capability        TEXT,
    endpoint_type     TEXT,
    request_summary   TEXT NOT NULL,  -- credential-free; see auth.redact
    outcome           TEXT NOT NULL,  -- submitted | refused | failed | confirmed
    response_summary  TEXT,
    confirmed         INTEGER NOT NULL DEFAULT 0,
    detail_json       TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_time ON audit_log (at DESC);

-- Features disabled at runtime because required data is missing or unparseable.
CREATE TABLE IF NOT EXISTS blocked_features (
    feature     TEXT PRIMARY KEY,
    reason      TEXT NOT NULL,
    gaps_json   TEXT NOT NULL,
    remedy      TEXT,
    blocked_at  TEXT NOT NULL
);

-- Bookkeeping for incremental ingestion (last-seen transaction id, etc).
CREATE TABLE IF NOT EXISTS ingest_state (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
