-- Four tables. Field meaning and state rules live in docs/contracts.md.

CREATE TABLE domains (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    name    TEXT NOT NULL UNIQUE,
    created TEXT NOT NULL
) STRICT;

CREATE TABLE collections (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    domain_id INTEGER NOT NULL REFERENCES domains (id),
    name      TEXT NOT NULL,
    feed_url  TEXT,
    created   TEXT NOT NULL,
    UNIQUE (domain_id, name)
) STRICT;

CREATE TABLE jobs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    domain_id     INTEGER NOT NULL REFERENCES domains (id),
    collection_id INTEGER REFERENCES collections (id),
    pipeline      TEXT NOT NULL CHECK (pipeline IN ('video', 'arxiv')),
    platform      TEXT NOT NULL CHECK (platform IN ('bilibili', 'youtube', 'arxiv')),
    source_id     TEXT NOT NULL,
    url           TEXT NOT NULL,
    translate     TEXT NOT NULL CHECK (translate IN ('auto', 'off')),
    status        TEXT NOT NULL CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'canceled')),
    title         TEXT,
    vault_path    TEXT,
    created       TEXT NOT NULL,
    updated       TEXT NOT NULL
) STRICT;

-- SQLite treats NULLs as distinct in unique indexes, so unsorted jobs map to 0.
CREATE UNIQUE INDEX jobs_source_once ON jobs (domain_id, ifnull(collection_id, 0), platform, source_id);

CREATE INDEX jobs_status ON jobs (status);

CREATE TABLE steps (
    job_id    INTEGER NOT NULL REFERENCES jobs (id),
    seq       INTEGER NOT NULL,
    name      TEXT NOT NULL CHECK (name IN ('video-fetch', 'video-transcribe', 'video-frames', 'arxiv-fetch', 'arxiv-convert', 'note', 'translate', 'publish')),
    status    TEXT NOT NULL CHECK (status IN ('pending', 'running', 'succeeded', 'failed', 'canceled')),
    attempts  INTEGER NOT NULL DEFAULT 0,
    exit_code INTEGER,
    error     TEXT,
    started   TEXT,
    finished  TEXT,
    PRIMARY KEY (job_id, seq)
) STRICT;
