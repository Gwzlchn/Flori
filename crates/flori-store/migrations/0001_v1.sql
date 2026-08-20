BEGIN IMMEDIATE;

CREATE TABLE schema_meta(
  version INTEGER PRIMARY KEY CHECK(version=1),
  contract_revision TEXT NOT NULL CHECK(contract_revision='flori.v1'),
  applied_at_ms INTEGER NOT NULL CHECK(applied_at_ms>=0)
);
CREATE TABLE pipelines(
  id TEXT PRIMARY KEY, key TEXT NOT NULL UNIQUE, current_revision_id TEXT,
  created_at_ms INTEGER NOT NULL CHECK(created_at_ms>=0),
  FOREIGN KEY(current_revision_id) REFERENCES pipeline_revisions(id)
    DEFERRABLE INITIALLY DEFERRED
);
CREATE TABLE pipeline_revisions(
  id TEXT PRIMARY KEY, pipeline_id TEXT NOT NULL REFERENCES pipelines(id) ON DELETE CASCADE,
  compiler_version INTEGER NOT NULL CHECK(compiler_version=1), git_commit TEXT NOT NULL,
  yaml_sha256 TEXT NOT NULL, yaml_text TEXT NOT NULL,
  created_at_ms INTEGER NOT NULL CHECK(created_at_ms>=0),
  UNIQUE(pipeline_id,yaml_sha256)
);
CREATE TABLE sources(
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL CHECK(kind IN ('arxiv','pdf_url','pdf_upload','bilibili_video','bilibili_channel','youtube_video','youtube_channel','local_video')),
  canonical_ref TEXT NOT NULL, title TEXT,
  domain_id TEXT NOT NULL REFERENCES domains(id) ON DELETE RESTRICT,
  credential_id TEXT REFERENCES credentials(id) ON DELETE SET NULL,
  current_job_id TEXT, previous_job_id TEXT,
  request_key TEXT NOT NULL UNIQUE, request_sha256 TEXT NOT NULL,
  created_at_ms INTEGER NOT NULL CHECK(created_at_ms>=0),
  updated_at_ms INTEGER NOT NULL CHECK(updated_at_ms>=created_at_ms),
  UNIQUE(kind,canonical_ref), CHECK(current_job_id IS NULL OR current_job_id<>previous_job_id),
  FOREIGN KEY(current_job_id) REFERENCES jobs(id) DEFERRABLE INITIALLY DEFERRED,
  FOREIGN KEY(previous_job_id) REFERENCES jobs(id) DEFERRABLE INITIALLY DEFERRED
);
CREATE TABLE source_inputs(
  id TEXT PRIMARY KEY, source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
  name TEXT NOT NULL, media_type TEXT NOT NULL, size_bytes INTEGER NOT NULL CHECK(size_bytes>=0),
  sha256 TEXT NOT NULL, relative_path TEXT NOT NULL,
  created_at_ms INTEGER NOT NULL CHECK(created_at_ms>=0), UNIQUE(source_id,name)
);
CREATE TABLE jobs(
  id TEXT PRIMARY KEY, source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
  pipeline_revision_id TEXT NOT NULL REFERENCES pipeline_revisions(id),
  trigger TEXT NOT NULL CHECK(trigger IN ('initial','pipeline_rerun','task_rerun','subscription')),
  rerun_of_job_id TEXT REFERENCES jobs(id) ON DELETE SET NULL, rerun_from_task_key TEXT,
  state TEXT NOT NULL CHECK(state IN ('queued','running','succeeded','failed','canceled')),
  prompt_snapshot_id TEXT NOT NULL UNIQUE, prompt_snapshot_sha256 TEXT NOT NULL,
  prompt_snapshot_json TEXT NOT NULL, inputs_json TEXT NOT NULL,
  request_key TEXT NOT NULL UNIQUE, request_sha256 TEXT NOT NULL,
  created_at_ms INTEGER NOT NULL CHECK(created_at_ms>=0), started_at_ms INTEGER, finished_at_ms INTEGER,
  error_code TEXT, error_message TEXT,
  CHECK(started_at_ms IS NULL OR started_at_ms>=created_at_ms),
  CHECK(finished_at_ms IS NULL OR finished_at_ms>=COALESCE(started_at_ms,created_at_ms))
);
CREATE TABLE tasks(
  id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  task_key TEXT NOT NULL,
  executor TEXT NOT NULL CHECK(executor IN ('document.acquire','document.extract','ai.document_translate','ai.document_note','video.acquire','video.subscription','video.transcribe','video.frames','video.mechanical_note','ai.video_note','core.validate','core.publish')),
  spec_json TEXT NOT NULL, input_bindings_json TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('pending','ready','leased','succeeded','failed','canceled','skipped')),
  pinned_runner_id TEXT REFERENCES runners(id), selected_model TEXT, selected_effort TEXT,
  runner_config_revision INTEGER, attempt_limit INTEGER NOT NULL CHECK(attempt_limit BETWEEN 1 AND 3),
  timeout_ms INTEGER NOT NULL CHECK(timeout_ms BETWEEN 1000 AND 86400000), current_attempt_id TEXT,
  ready_at_ms INTEGER, started_at_ms INTEGER, finished_at_ms INTEGER,
  error_code TEXT, error_message TEXT, UNIQUE(job_id,task_key),
  FOREIGN KEY(current_attempt_id) REFERENCES attempts(id) ON DELETE SET NULL
    DEFERRABLE INITIALLY DEFERRED
);
CREATE TABLE attempts(
  id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  attempt_no INTEGER NOT NULL CHECK(attempt_no>0), runner_id TEXT REFERENCES runners(id),
  state TEXT NOT NULL CHECK(state IN ('leased','succeeded','failed','expired','canceled')),
  model TEXT, effort TEXT, runner_config_revision INTEGER,
  lease_expires_at_ms INTEGER NOT NULL CHECK(lease_expires_at_ms>=0),
  last_log_sequence INTEGER NOT NULL DEFAULT 0 CHECK(last_log_sequence>=0),
  started_at_ms INTEGER NOT NULL CHECK(started_at_ms>=0), finished_at_ms INTEGER,
  error_code TEXT, error_message TEXT, UNIQUE(task_id,attempt_no),
  CHECK(finished_at_ms IS NULL OR finished_at_ms>=started_at_ms)
);
CREATE TABLE uploads(
  id TEXT PRIMARY KEY, owner_kind TEXT NOT NULL CHECK(owner_kind IN ('source','attempt','materialize')),
  owner_id TEXT NOT NULL, request_key TEXT, request_sha256 TEXT, commit_json TEXT,
  name TEXT NOT NULL, target_id TEXT NOT NULL,
  source_artifact_id TEXT REFERENCES artifacts(id) ON DELETE SET NULL,
  staging_path TEXT NOT NULL, final_relative_path TEXT NOT NULL,
  expected_size_bytes INTEGER NOT NULL CHECK(expected_size_bytes>=0), expected_sha256 TEXT NOT NULL,
  received_bytes INTEGER NOT NULL CHECK(received_bytes>=0 AND received_bytes<=expected_size_bytes),
  state TEXT NOT NULL CHECK(state IN ('receiving','verified','moved')),
  created_at_ms INTEGER NOT NULL CHECK(created_at_ms>=0),
  updated_at_ms INTEGER NOT NULL CHECK(updated_at_ms>=created_at_ms),
  UNIQUE(owner_kind,owner_id,name),
  CHECK(owner_kind='attempt' OR (request_sha256 IS NOT NULL AND commit_json IS NOT NULL))
);
CREATE UNIQUE INDEX uploads_request_key_unique ON uploads(request_key)
  WHERE request_key IS NOT NULL AND owner_kind IN ('source','materialize');
CREATE TABLE artifacts(
  id TEXT PRIMARY KEY, source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
  job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  attempt_id TEXT REFERENCES attempts(id),
  origin TEXT NOT NULL CHECK(origin IN ('produced','materialized')),
  materialized_from_artifact_id TEXT REFERENCES artifacts(id) ON DELETE SET NULL,
  name TEXT NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN ('source_original','document_structure','figure','table_region','translation','subtitle','transcript','keyframe','danmaku','parts_manifest','subscription_manifest','mechanical_note','smart_note','summary','terms','evidence','task_log','ai_audit')),
  media_type TEXT NOT NULL, file_name TEXT NOT NULL, size_bytes INTEGER NOT NULL CHECK(size_bytes>=0),
  sha256 TEXT NOT NULL, relative_path TEXT NOT NULL,
  retention TEXT NOT NULL CHECK(retention IN ('source','published','failed_audit')),
  created_at_ms INTEGER NOT NULL CHECK(created_at_ms>=0),
  CHECK((origin='produced' AND attempt_id IS NOT NULL) OR (origin='materialized' AND attempt_id IS NULL)),
  UNIQUE(attempt_id,name)
);
CREATE UNIQUE INDEX artifacts_materialized_name_unique ON artifacts(job_id,task_id,name)
  WHERE origin='materialized';
CREATE TABLE runners(
  id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE,
  state TEXT NOT NULL CHECK(state IN ('enabled','disabled')),
  token_digest TEXT UNIQUE, registration_token_digest TEXT UNIQUE,
  registration_expires_at_ms INTEGER, config_revision INTEGER NOT NULL CHECK(config_revision>=0),
  max_concurrency INTEGER NOT NULL CHECK(max_concurrency>0),
  tags_json TEXT NOT NULL, tools_json TEXT NOT NULL, ai_models_json TEXT NOT NULL,
  default_model TEXT, default_effort TEXT, last_seen_at_ms INTEGER,
  created_at_ms INTEGER NOT NULL CHECK(created_at_ms>=0),
  updated_at_ms INTEGER NOT NULL CHECK(updated_at_ms>=created_at_ms)
);
CREATE TABLE credentials(
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL CHECK(kind IN ('bilibili_cookie','youtube_cookie')),
  name TEXT NOT NULL UNIQUE, plaintext_value TEXT NOT NULL,
  created_at_ms INTEGER NOT NULL CHECK(created_at_ms>=0),
  updated_at_ms INTEGER NOT NULL CHECK(updated_at_ms>=created_at_ms)
);
CREATE TABLE prompts(
  key TEXT PRIMARY KEY, content TEXT NOT NULL, sha256 TEXT NOT NULL,
  updated_at_ms INTEGER NOT NULL CHECK(updated_at_ms>=0)
);
CREATE TABLE ai_usage(
  id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  attempt_id TEXT NOT NULL REFERENCES attempts(id) ON DELETE CASCADE,
  invocation_key TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('started','final')),
  tool TEXT NOT NULL CHECK(tool IN ('qoder_cli','codex_cli')), model TEXT NOT NULL, effort TEXT NOT NULL,
  origin TEXT CHECK(origin IN ('observed','estimated','unavailable')),
  input_tokens INTEGER CHECK(input_tokens>=0), output_tokens INTEGER CHECK(output_tokens>=0),
  cost_micros INTEGER CHECK(cost_micros>=0), credits_micros INTEGER CHECK(credits_micros>=0),
  created_at_ms INTEGER NOT NULL CHECK(created_at_ms>=0), finalized_at_ms INTEGER,
  UNIQUE(attempt_id,invocation_key),
  CHECK((state='started' AND origin IS NULL AND input_tokens IS NULL AND output_tokens IS NULL AND cost_micros IS NULL AND credits_micros IS NULL AND finalized_at_ms IS NULL)
     OR (state='final' AND origin IS NOT NULL AND finalized_at_ms IS NOT NULL AND finalized_at_ms>=created_at_ms))
);
CREATE TABLE job_events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  scope TEXT NOT NULL CHECK(scope IN ('system','source','job','runner')),
  scope_id TEXT, kind TEXT NOT NULL, payload_json TEXT NOT NULL,
  created_at_ms INTEGER NOT NULL CHECK(created_at_ms>=0)
);
CREATE TABLE domains(
  id TEXT PRIMARY KEY, slug TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
  description TEXT, profile_text TEXT NOT NULL,
  created_at_ms INTEGER NOT NULL CHECK(created_at_ms>=0),
  updated_at_ms INTEGER NOT NULL CHECK(updated_at_ms>=created_at_ms)
);
CREATE TABLE collections(
  id TEXT PRIMARY KEY, domain_id TEXT NOT NULL REFERENCES domains(id) ON DELETE RESTRICT,
  name TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('manual','subscription')),
  subscription_source_id TEXT REFERENCES sources(id) ON DELETE SET NULL,
  enabled INTEGER NOT NULL CHECK(enabled IN (0,1)), fanout_limit INTEGER,
  last_synced_at_ms INTEGER, last_sync_error TEXT,
  created_at_ms INTEGER NOT NULL CHECK(created_at_ms>=0),
  updated_at_ms INTEGER NOT NULL CHECK(updated_at_ms>=created_at_ms), UNIQUE(domain_id,name),
  CHECK((kind='manual' AND subscription_source_id IS NULL AND fanout_limit IS NULL)
     OR (kind='subscription' AND fanout_limit BETWEEN 1 AND 100
         AND (subscription_source_id IS NOT NULL OR enabled=0)))
);
CREATE TABLE collection_sources(
  collection_id TEXT NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
  source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
  added_at_ms INTEGER NOT NULL CHECK(added_at_ms>=0), PRIMARY KEY(collection_id,source_id)
);
CREATE TABLE glossary_terms(
  id TEXT PRIMARY KEY, domain_id TEXT NOT NULL REFERENCES domains(id) ON DELETE CASCADE,
  term TEXT NOT NULL, normalized_term TEXT NOT NULL, explanation TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('active','hidden')),
  created_at_ms INTEGER NOT NULL CHECK(created_at_ms>=0),
  updated_at_ms INTEGER NOT NULL CHECK(updated_at_ms>=created_at_ms),
  UNIQUE(domain_id,normalized_term)
);
CREATE TABLE concept_occurrences(
  id TEXT PRIMARY KEY, term_id TEXT NOT NULL REFERENCES glossary_terms(id) ON DELETE CASCADE,
  source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
  job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  evidence_id TEXT NOT NULL REFERENCES evidence(id) ON DELETE CASCADE,
  source_order INTEGER NOT NULL CHECK(source_order>=0),
  created_at_ms INTEGER NOT NULL CHECK(created_at_ms>=0)
);
CREATE TABLE concept_edges(
  from_term_id TEXT NOT NULL REFERENCES glossary_terms(id) ON DELETE CASCADE,
  to_term_id TEXT NOT NULL REFERENCES glossary_terms(id) ON DELETE CASCADE,
  relation TEXT NOT NULL, job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  evidence_id TEXT NOT NULL REFERENCES evidence(id) ON DELETE CASCADE,
  weight INTEGER NOT NULL, PRIMARY KEY(from_term_id,to_term_id,relation,job_id,evidence_id)
);
CREATE TABLE evidence(
  id TEXT PRIMARY KEY, source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
  job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  artifact_id TEXT NOT NULL REFERENCES artifacts(id) ON DELETE CASCADE,
  locator_kind TEXT NOT NULL CHECK(locator_kind IN ('pdf','video')),
  page INTEGER, x1 REAL, y1 REAL, x2 REAL, y2 REAL,
  start_ms INTEGER, end_ms INTEGER,
  keyframe_artifact_id TEXT REFERENCES artifacts(id) ON DELETE SET NULL, quote TEXT NOT NULL,
  CHECK((locator_kind='pdf' AND page>=1 AND x1>=0 AND y1>=0 AND x2>x1 AND y2>y1
         AND start_ms IS NULL AND end_ms IS NULL AND keyframe_artifact_id IS NULL)
     OR (locator_kind='video' AND page IS NULL AND x1 IS NULL AND y1 IS NULL AND x2 IS NULL AND y2 IS NULL
         AND start_ms>=0 AND end_ms>start_ms))
);
CREATE VIRTUAL TABLE search_chunks USING fts5(
  chunk_id UNINDEXED, source_id UNINDEXED, job_id UNINDEXED, artifact_id UNINDEXED,
  title, body, tokenize='trigram'
);
CREATE TABLE search_chunk_evidence(
  chunk_id TEXT NOT NULL, evidence_id TEXT NOT NULL REFERENCES evidence(id) ON DELETE CASCADE,
  PRIMARY KEY(chunk_id,evidence_id)
);

CREATE TRIGGER source_current_job_insert BEFORE INSERT ON sources
WHEN NEW.current_job_id IS NOT NULL OR NEW.previous_job_id IS NOT NULL
BEGIN
  SELECT CASE WHEN EXISTS(
    SELECT 1 FROM jobs WHERE id IN (NEW.current_job_id,NEW.previous_job_id)
      AND (source_id<>NEW.id OR state<>'succeeded')
  ) THEN RAISE(ABORT,'source job pointer mismatch') END;
END;

CREATE TRIGGER source_current_job_update BEFORE UPDATE OF current_job_id,previous_job_id ON sources
BEGIN
  SELECT CASE WHEN EXISTS(
    SELECT 1 FROM jobs WHERE id IN (NEW.current_job_id,NEW.previous_job_id)
      AND (source_id<>NEW.id OR state<>'succeeded')
  ) THEN RAISE(ABORT,'source job pointer mismatch') END;
END;

CREATE TRIGGER collection_subscription_insert BEFORE INSERT ON collections
WHEN NEW.kind='subscription' AND NEW.subscription_source_id IS NOT NULL
BEGIN
  SELECT CASE WHEN NOT EXISTS(
    SELECT 1 FROM sources WHERE id=NEW.subscription_source_id AND domain_id=NEW.domain_id
      AND kind IN ('bilibili_channel','youtube_channel')
  ) THEN RAISE(ABORT,'subscription source mismatch') END;
END;

CREATE TRIGGER collection_subscription_update BEFORE UPDATE OF domain_id,kind,subscription_source_id ON collections
WHEN NEW.kind='subscription' AND NEW.subscription_source_id IS NOT NULL
BEGIN
  SELECT CASE WHEN NOT EXISTS(
    SELECT 1 FROM sources WHERE id=NEW.subscription_source_id AND domain_id=NEW.domain_id
      AND kind IN ('bilibili_channel','youtube_channel')
  ) THEN RAISE(ABORT,'subscription source mismatch') END;
END;

CREATE TRIGGER collection_source_domain_insert BEFORE INSERT ON collection_sources
BEGIN
  SELECT CASE WHEN NOT EXISTS(
    SELECT 1 FROM collections c JOIN sources s ON s.id=NEW.source_id
    WHERE c.id=NEW.collection_id AND c.domain_id=s.domain_id
  ) THEN RAISE(ABORT,'collection source domain mismatch') END;
END;

CREATE TRIGGER attempt_runner_insert BEFORE INSERT ON attempts
WHEN NEW.runner_id IS NULL
BEGIN
  SELECT CASE WHEN NOT EXISTS(
    SELECT 1 FROM tasks WHERE id=NEW.task_id AND executor LIKE 'core.%'
  ) THEN RAISE(ABORT,'runner required') END;
END;

CREATE TRIGGER attempt_runner_update BEFORE UPDATE OF runner_id ON attempts
WHEN NEW.runner_id IS NULL
BEGIN
  SELECT CASE WHEN NOT EXISTS(
    SELECT 1 FROM tasks WHERE id=NEW.task_id AND executor LIKE 'core.%'
  ) THEN RAISE(ABORT,'runner required') END;
END;

INSERT INTO schema_meta VALUES(
  1,'flori.v1',CAST((julianday('now')-2440587.5)*86400000 AS INTEGER)
);
COMMIT;
