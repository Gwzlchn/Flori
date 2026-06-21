"""步骤测试公用 fixture。"""


def make_step_config(tmp_path, step_name="test", pool="cpu", **overrides):
    """构建指定步骤名的 config。"""
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir(exist_ok=True)
    cfg = {
        "step": {"name": step_name, "pool": pool, "timeout_sec": 60, "retries": 0},
        "ai": {},
        "domain": {"name": "general"},
        "style_tags": [],
        "paths": {
            "data_dir": str(tmp_path),
            "prompts_dir": str(prompts_dir),
            "config_dir": str(tmp_path),
        },
        "providers": {},
    }
    cfg.update(overrides)
    return cfg
