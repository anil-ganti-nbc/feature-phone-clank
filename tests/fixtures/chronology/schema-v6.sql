-- FEATURE-01 schema v4. Append-only observations; only workflow columns mutate.

CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY,
    source_key TEXT NOT NULL UNIQUE,
    manufacturer TEXT NOT NULL,
    source_type TEXT NOT NULL,        -- catalogue | support | newsroom | certification
    region TEXT,
    base_url TEXT NOT NULL,
    config_json TEXT NOT NULL DEFAULT '{}',
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES sources(id),
    product_key TEXT NOT NULL UNIQUE,   -- '<source_key>:<identity>'
    manufacturer TEXT NOT NULL,
    model TEXT NOT NULL,
    model_number TEXT,
    region TEXT,
    url TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',   -- active | removed
    consecutive_absences INTEGER NOT NULL DEFAULT 0,  -- healthy runs in a row this product was missing
    first_seen_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_products_source ON products(source_id, last_seen_at);

CREATE TABLE IF NOT EXISTS observations (
    id INTEGER PRIMARY KEY,
    product_id INTEGER NOT NULL REFERENCES products(id),
    content_hash TEXT NOT NULL,
    fields_json TEXT NOT NULL DEFAULT '{}',
    spec_completeness TEXT NOT NULL DEFAULT 'complete',  -- complete | incomplete
    price REAL,
    currency TEXT,
    availability TEXT,
    observed_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(product_id, content_hash)
);
CREATE INDEX IF NOT EXISTS idx_observations_product ON observations(product_id, observed_at);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    product_id INTEGER NOT NULL REFERENCES products(id),
    collector TEXT NOT NULL,
    event_type TEXT NOT NULL,         -- ChangeType
    field TEXT,                       -- legacy single-field columns (Stage 1/2 schema);
    previous_value_json TEXT,         -- Stage 3 events use changed_fields_json instead and
    new_value_json TEXT,              -- leave these three NULL.
    changed_fields_json TEXT NOT NULL DEFAULT '[]',  -- [{"field","old_value","new_value"}, ...]
    previous_observation_id INTEGER REFERENCES observations(id),
    current_observation_id INTEGER REFERENCES observations(id),
    dedup_key TEXT,                   -- Event.dedup_key(); UNIQUE index below is the actual
                                       -- duplicate-protection mechanism (brief section 13)
    alert_level TEXT NOT NULL,        -- AlertLevel
    confidence TEXT NOT NULL,         -- Confidence
    detected_at TEXT NOT NULL DEFAULT (datetime('now')),
    meta_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_events_product ON events(product_id, detected_at);
CREATE INDEX IF NOT EXISTS idx_events_alert_level ON events(alert_level, detected_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_events_dedup ON events(dedup_key) WHERE dedup_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS collector_runs (
    id INTEGER PRIMARY KEY,
    source_key TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL DEFAULT 'running',  -- running | ok | failed | blocked_zero_result | out_of_scope
    products_observed INTEGER,
    previous_products_observed INTEGER,
    stats_json TEXT NOT NULL DEFAULT '{}',
    provenance TEXT NOT NULL DEFAULT 'UNKNOWN',
    qualification_scope TEXT,
    qualification_epoch_id INTEGER,
    qualification_material_identity TEXT,
    qualification_gate_status TEXT NOT NULL DEFAULT 'UNKNOWN'
);
CREATE INDEX IF NOT EXISTS idx_runs_source ON collector_runs(source_key, started_at);

-- Qualification evidence is deliberately separate from the continuity/data-
-- loss registry.  Epoch rows are append-only lineage; the current row per
-- scope is held in qualification_state.  A reset and a terminal fact are
-- independent rows so a changed execution cannot consume the same evidence
-- slot as its terminal record.
CREATE TABLE IF NOT EXISTS qualification_state (
    scope_key TEXT PRIMARY KEY,
    epoch_id INTEGER NOT NULL,
    material_identity TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS qualification_epochs (
    id INTEGER PRIMARY KEY,
    scope_key TEXT NOT NULL,
    material_identity TEXT NOT NULL,
    prior_material_identity TEXT,
    reset_reason TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(scope_key, id)
);
CREATE INDEX IF NOT EXISTS idx_qualification_epochs_scope ON qualification_epochs(scope_key, id);
CREATE TABLE IF NOT EXISTS qualification_events (
    id INTEGER PRIMARY KEY,
    run_id INTEGER REFERENCES collector_runs(id),
    scope_key TEXT NOT NULL,
    epoch_id INTEGER NOT NULL REFERENCES qualification_epochs(id),
    event_type TEXT NOT NULL, -- RESET | TERMINAL
    provenance TEXT NOT NULL DEFAULT 'UNKNOWN',
    material_identity TEXT NOT NULL,
    prior_material_identity TEXT,
    status TEXT,
    counts_for_qualification INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(run_id, event_type)
);
CREATE INDEX IF NOT EXISTS idx_qualification_events_scope ON qualification_events(scope_key, epoch_id, event_type);

CREATE TABLE IF NOT EXISTS run_errors (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES collector_runs(id),
    message TEXT NOT NULL,
    occurred_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Notification state (brief section 14 / 15). No provider writes to this in
-- Stage 1 — Discord delivery is Stage 4 — but the shape is settled now so
-- that stage doesn't require a schema migration to bolt on.
-- Classification quarantine (Stage 2, brief section 4 / user constraint 2).
-- A candidate URL that a collector's deterministic classifier could not
-- confidently resolve to 'feature_phone' (rejected smartphones included, for
-- audit) is logged here with its evidence instead of silently entering or
-- silently being dropped from the product catalogue. One row per
-- (source_key, slug); re-classification on a later run updates it in place
-- rather than growing unboundedly.
CREATE TABLE IF NOT EXISTS classification_log (
    id INTEGER PRIMARY KEY,
    source_key TEXT NOT NULL,
    slug TEXT NOT NULL,
    url TEXT NOT NULL,
    classification TEXT NOT NULL,     -- feature_phone | smartphone | ambiguous
    evidence_json TEXT NOT NULL DEFAULT '{}',
    first_seen_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(source_key, slug)
);
CREATE INDEX IF NOT EXISTS idx_classification_log_class ON classification_log(classification, last_seen_at);

CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY,
    event_id INTEGER REFERENCES events(id),
    provider TEXT NOT NULL,
    dedup_key TEXT NOT NULL UNIQUE,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',  -- pending | sent | failed | suppressed
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    sent_at TEXT,
    not_before TEXT  -- durable retry floor (RFC 429 Retry-After); NULL = eligible now
);
CREATE INDEX IF NOT EXISTS idx_notifications_status ON notifications(status);

-- Durable operator policy for delivery activation (v6). Key/value so a
-- future policy knob does not need another migration; the only key in use
-- today is `discord.activation_cutoff` (see core/delivery_policy.py).
CREATE TABLE IF NOT EXISTS delivery_policy (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
