-- Prompt texts edited on the web. A row without domain_id replaces the built-in prompt of that
-- name; a row with one is that domain's requirement, appended to the prompt for its jobs and
-- deleted with the domain. Rules: docs/contracts.md.

CREATE TABLE prompts (
    name      TEXT NOT NULL CHECK (name IN ('note-video', 'note-arxiv', 'note-images', 'translate', 'punctuate')),
    domain_id INTEGER REFERENCES domains (id) ON DELETE CASCADE,
    text      TEXT NOT NULL,
    updated   TEXT NOT NULL
) STRICT;

-- SQLite treats NULLs as distinct in unique indexes, so the overrides map to 0.
CREATE UNIQUE INDEX prompts_once ON prompts (ifnull(domain_id, 0), name);
