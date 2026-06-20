"""公用 test fixtures。"""

import os
from pathlib import Path

import pytest

# 测试环境视为可信本地:默认放行无 token 鉴权(verify_token fail-closed 的逃生口),
# 否则所有命中受保护端点、未设 API_TOKEN 的用例都会 503。需测 fail-closed 的用例自行清此项。
os.environ.setdefault("API_ALLOW_NO_AUTH", "1")


@pytest.fixture
def configs_dir():
    """项目根目录的 configs/ 示例配置。"""
    return Path(__file__).parent.parent / "configs"


@pytest.fixture
def tmp_data_dir(tmp_path):
    """临时 data 目录，模拟 /data/。"""
    (tmp_path / "db").mkdir()
    (tmp_path / "jobs").mkdir()
    (tmp_path / "prompts").mkdir()
    return tmp_path
