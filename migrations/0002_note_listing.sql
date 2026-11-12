-- What the note list shows and sorts by, copied from the step manifests when publish succeeds.
-- Jobs published earlier keep NULL.

ALTER TABLE jobs ADD COLUMN author TEXT;
ALTER TABLE jobs ADD COLUMN published TEXT;
ALTER TABLE jobs ADD COLUMN duration_ms INTEGER;
ALTER TABLE jobs ADD COLUMN description TEXT;
