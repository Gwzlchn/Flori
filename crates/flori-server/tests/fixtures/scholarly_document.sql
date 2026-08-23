INSERT INTO domains(id,slug,name,profile_text,created_at_ms,updated_at_ms)
VALUES('018f0000-0000-7000-8000-000000000001','research','Research','',0,0);
INSERT INTO pipelines(id,key,created_at_ms)
VALUES('018f0000-0000-7000-8000-000000000002','pdf',0);
INSERT INTO pipeline_revisions(id,pipeline_id,compiler_version,git_commit,yaml_sha256,yaml_text,created_at_ms)
VALUES('018f0000-0000-7000-8000-000000000003','018f0000-0000-7000-8000-000000000002',1,'test','0000000000000000000000000000000000000000000000000000000000000000','tasks: {}',0);
INSERT INTO sources(id,kind,canonical_ref,title,domain_id,request_key,request_sha256,created_at_ms,updated_at_ms)
VALUES('018f0000-0000-7000-8000-000000000004','arxiv','arxiv:1706.03762','Attention','018f0000-0000-7000-8000-000000000001','source','0000000000000000000000000000000000000000000000000000000000000000',0,0);
INSERT INTO jobs(id,source_id,pipeline_revision_id,trigger,state,prompt_snapshot_id,prompt_snapshot_sha256,prompt_snapshot_json,inputs_json,request_key,request_sha256,created_at_ms,started_at_ms,finished_at_ms)
VALUES('018f0000-0000-7000-8000-000000000005','018f0000-0000-7000-8000-000000000004','018f0000-0000-7000-8000-000000000003','initial','succeeded','018f0000-0000-7000-8000-000000000006','0000000000000000000000000000000000000000000000000000000000000000','{}','{"translate":false}','job','0000000000000000000000000000000000000000000000000000000000000000',0,0,1);
INSERT INTO tasks(id,job_id,task_key,executor,spec_json,input_bindings_json,state,attempt_limit,timeout_ms,finished_at_ms)
VALUES('018f0000-0000-7000-8000-000000000007','018f0000-0000-7000-8000-000000000005','acquire','document.acquire','{"executor":"document.acquire","needs":[],"tags":[],"retry":0,"timeout_ms":1000,"artifacts":[]}','{}','succeeded',1,1000,1);
UPDATE sources SET current_job_id='018f0000-0000-7000-8000-000000000005'
WHERE id='018f0000-0000-7000-8000-000000000004';
