"""步骤测试公用 fixture。"""

from pathlib import Path


def make_step_config(tmp_path, step_name="test", pool="cpu", pipeline="article", **overrides):
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir(exist_ok=True)
    cfg = {
        "step": {"name": step_name, "pipeline": pipeline, "pool": pool,
                 "timeout_sec": 60, "retries": 0},
        "ai": {},
        "domain": {"name": "general"},
        "style_tags": [],
        "paths": {
            "data_dir": str(tmp_path),
            "prompts_dir": str(prompts_dir),
            "config_dir": str(Path(__file__).resolve().parents[2] / "configs"),
        },
        "providers": {},
    }
    cfg.update(overrides)
    return cfg


def make_job_dir(tmp_path, *subdirs, name="job"):
    """建 job 工作目录和指定子目录。各步子目录集合不同,故用变长参数而非固定集合。"""
    job_dir = tmp_path / name
    job_dir.mkdir(exist_ok=True)
    for d in subdirs:
        (job_dir / d).mkdir(exist_ok=True)
    return job_dir
