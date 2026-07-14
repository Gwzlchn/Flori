PRAGMA user_version = 0;

CREATE TABLE jobs (
    id TEXT PRIMARY KEY,
    content_type TEXT NOT NULL,
    pipeline TEXT NOT NULL,
    collection_id TEXT,
    url TEXT,
    title TEXT,
    domain TEXT NOT NULL DEFAULT 'general',
    source TEXT,
    style_tags TEXT DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'pending',
    progress_pct INTEGER DEFAULT 0,
    meta TEXT DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    error TEXT
);

INSERT INTO jobs (
    id, content_type, pipeline, url, title, domain, source, created_at, updated_at
) VALUES (
    'legacy-v0-job', 'article', 'article', 'https://example.com/v0',
    '无版本历史任务', 'general', 'http_article',
    '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00'
);

CREATE TABLE prompt_overrides (
    scope TEXT NOT NULL DEFAULT 'global',
    domain TEXT NOT NULL DEFAULT '',
    pipeline TEXT NOT NULL,
    step TEXT NOT NULL,
    content TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL,
    PRIMARY KEY (scope, domain, pipeline, step)
);

INSERT INTO prompt_overrides (
    scope, domain, pipeline, step, content, updated_at
) VALUES (
    'global', '', 'article', '04_smart', '无版本历史 Prompt',
    '2026-01-01T00:00:00+00:00'
);
