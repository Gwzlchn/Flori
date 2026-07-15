"""公用 test fixtures。"""

import os
from pathlib import Path
from unittest.mock import AsyncMock

import fakeredis.aioredis
import pytest
from httpx import ASGITransport, AsyncClient

from api.main import create_app
from shared.config import load_config
from shared.redis_client import RedisClient
from tests.current_schema_db import (
    build_current_schema_template,
    clone_current_schema_database,
)

# 测试环境视为可信本地:默认放行无 token 鉴权(verify_token fail-closed 的逃生口),
# 否则所有命中受保护端点、未设 API_TOKEN 的用例都会 503。需测 fail-closed 的用例自行清此项。
os.environ.setdefault("API_ALLOW_NO_AUTH", "1")


# 出网熔断(autouse,全套件)
# 测试进程永不持有真实 AI provider 密钥:即便将来有人写了忘记 mock _client 的 provider 用例、
# 且宿主/CI 恰好 export 了真 key,也不会真打外网/烧钱。把"靠每个用例自觉 mock"升级成结构性保证。
# 用例自身若要测 {NAME}_API_KEY 透传,会在 body 里 monkeypatch.setenv(晚于本 autouse,正常生效)。
@pytest.fixture(autouse=True)
def _no_real_ai_keys(monkeypatch):
    for _k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY",
               "KIMI_API_KEY", "MOONSHOT_API_KEY"):
        monkeypatch.delenv(_k, raising=False)


def make_fakeredis() -> RedisClient:
    """fakeredis 版 RedisClient 的单一构造来源,protocol=2 等参数只在这里定义。
    需关闭的用例自行 `await client.close()`(或用就近的 async fixture 包裹)。"""
    client = RedisClient.__new__(RedisClient)
    client._url = "redis://fake"
    client._redis = fakeredis.aioredis.FakeRedis(decode_responses=True, protocol=2)
    return client


# API 测试共用 fixture,各 test_api_*.py 直接复用。
# client 依赖 app;db 被各非 api 测试以本地同名 fixture 覆盖(就近优先),互不影响;
# test_api_search 自带带 seed 的 db 覆盖。
# app:多数纯 CRUD 路由不触 redis,默认给 AsyncMock 即可。真正需要路由特异 redis
# 行为(publish/ping/事件流等)的文件就近覆盖本 fixture(jobs/workers/admin/bili/collections/runner)。
def make_redis_mock() -> AsyncMock:
    """API 测试默认 redis AsyncMock。get_traffic 须返回真 dict——裸 AsyncMock 的
    `await get_traffic()` 回 AsyncMock,对其 .get() 又得 coroutine,污染 /api/status、
    /api/workers 等读流量的端点。单一构造来源,各文件就近 mock 复用。"""
    rc = AsyncMock()
    rc.get_traffic.return_value = {"total": 0, "by_worker": {}}
    return rc


@pytest.fixture
def app(db, test_config):
    return create_app(db=db, redis=make_redis_mock(), config=test_config)


@pytest.fixture
def test_config(tmp_path, configs_dir):
    cfg = load_config(config_dir=configs_dir, data_dir=tmp_path)
    cfg.jobs_dir = tmp_path / "jobs"
    cfg.jobs_dir.mkdir()
    cfg.prompts_dir = tmp_path / "prompts"
    cfg.prompts_dir.mkdir()
    return cfg


@pytest.fixture
def db(test_config, current_schema_db_template):
    d = clone_current_schema_database(
        current_schema_db_template,
        test_config.db_path,
    )
    yield d
    d.close()


@pytest.fixture(scope="session")
def current_schema_db_template(tmp_path_factory):
    """每个 pytest worker 只迁移一次，每个用例仍使用独立空库。"""
    path = tmp_path_factory.mktemp("current-schema") / "template.db"
    return build_current_schema_template(path)


@pytest.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def configs_dir():
    """项目根目录的 configs/ 示例配置。"""
    return Path(__file__).parent.parent / "configs"


@pytest.fixture
def tmp_data_dir(tmp_path):
    """临时 data 目录,模拟 /data/。"""
    (tmp_path / "db").mkdir()
    (tmp_path / "jobs").mkdir()
    (tmp_path / "prompts").mkdir()
    return tmp_path
