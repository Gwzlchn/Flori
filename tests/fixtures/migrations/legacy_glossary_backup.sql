CREATE TABLE glossary_bak_clean_20260617(
    domain TEXT,
    term TEXT,
    definition TEXT,
    related TEXT,
    status TEXT,
    created_at TEXT,
    updated_at TEXT,
    occurrences TEXT,
    is_topic INT,
    definition_locked INT
);

WITH RECURSIVE legacy_rows(n) AS (
    VALUES(1)
    UNION ALL
    SELECT n + 1 FROM legacy_rows WHERE n < 249
)
INSERT INTO glossary_bak_clean_20260617 (
    domain, term, definition, related, status, created_at, updated_at,
    occurrences, is_topic, definition_locked
)
SELECT
    printf('legacy-domain-%03d', n % 11),
    printf('legacy-term-%03d', n),
    CASE WHEN n = 1 THEN X'00FF414243' ELSE printf('旧定义-%03d', n) END,
    printf('["Legacy-%03d"]', n),
    CASE WHEN n % 2 = 0 THEN 'accepted' ELSE 'pending' END,
    printf('2026-06-17T00:%02d:00+08:00', n % 60),
    printf('2026-06-17T01:%02d:00+08:00', n % 60),
    printf('[{"job_id":"legacy-%03d","quote":"大小写Aa-%03d"}]', n, n),
    n % 2,
    (n + 1) % 2
FROM legacy_rows;
