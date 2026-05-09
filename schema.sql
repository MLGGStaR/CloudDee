-- Docket SQLite schema. Single file, committed to repo (small) or stored as artifact (large).

CREATE TABLE IF NOT EXISTS records (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source          TEXT NOT NULL,           -- e.g. 'ntsb_aviation'
    external_id     TEXT NOT NULL,           -- source-specific unique id
    title           TEXT,
    url             TEXT,
    published_at    TEXT NOT NULL,           -- ISO 8601 UTC
    fetched_at      TEXT NOT NULL,
    raw_text        TEXT NOT NULL,           -- normalized plain text
    raw_json        TEXT,                    -- original payload, for debugging
    UNIQUE(source, external_id)
);

CREATE INDEX IF NOT EXISTS idx_records_source_published
    ON records(source, published_at DESC);

CREATE TABLE IF NOT EXISTS scores (
    record_id           INTEGER PRIMARY KEY,
    drama               INTEGER NOT NULL,    -- 1..10
    novelty             INTEGER NOT NULL,    -- 1..10
    visualization       INTEGER NOT NULL,    -- 1..10
    niche_fit_json      TEXT NOT NULL,       -- {"final-approach": 0..10, ...}
    summary             TEXT,                -- 2-sentence summary, used in dedupe + thumb
    flags_json          TEXT,                -- {"sealed":bool,"minor_involved":bool,...}
    scored_at           TEXT NOT NULL,
    FOREIGN KEY (record_id) REFERENCES records(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS productions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    record_id           INTEGER NOT NULL,
    channel_slug        TEXT NOT NULL,
    status              TEXT NOT NULL,       -- pending, scripted, voiced, rendered, uploaded, failed
    script_path         TEXT,
    audio_path          TEXT,
    video_path          TEXT,
    thumbnail_path      TEXT,
    youtube_video_id    TEXT,
    error               TEXT,
    started_at          TEXT NOT NULL,
    completed_at        TEXT,
    FOREIGN KEY (record_id) REFERENCES records(id) ON DELETE CASCADE,
    UNIQUE(record_id, channel_slug)
);

CREATE INDEX IF NOT EXISTS idx_productions_status
    ON productions(status);

CREATE TABLE IF NOT EXISTS run_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT NOT NULL,                -- ok, partial, failed
    summary     TEXT
);
