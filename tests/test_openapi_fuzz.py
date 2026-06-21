"""OpenAPI 契约/模糊测试(schemathesis)。

用 FastAPI 自带的 /openapi.json 自动派生用例(正/负/边界),默认跑 schemathesis 的
not_a_server_error / status_code_conformance / response_schema_conformance 等检查,
零手写用例就能逼出"畸形输入触发 5xx""响应不符合自己 schema"这类手写 test_api_* 漏掉的洞。

in-process(from_asgi):复用 conftest 的 db/test_config + 本地 fake redis 注入 create_app,
这样 lifespan 不会去连真 Redis/真 SQLite(见 api/main.py:create_app 的注入分支)。

本文件标了 `fuzz` marker,默认套件用 `-m 'not fuzz'` 跳过(慢、且找到的是待修 bug)。手动跑:
    docker compose -f docker-compose.test.yml run --rm test \
      sh -c "pip install -q 'schemathesis>=4,<5' && pytest -m fuzz tests/test_openapi_fuzz.py --no-cov -q"
"""
from __future__ import annotations

import fakeredis.aioredis
import pytest

# 无 schemathesis(如 mutmut 只装核心测试依赖的环境)→ importorskip 让整文件优雅跳过,
# 不在采集期抛 ImportError 拖垮别的 runner(mutmut 基线 pytest 会因此 collection error)。
# schemathesis 依赖 hypothesis,装了前者必有后者,故一处 importorskip 足够。
schemathesis = pytest.importorskip("schemathesis")

from hypothesis import HealthCheck, settings

from api.main import create_app
from shared.redis_client import RedisClient

pytestmark = pytest.mark.fuzz


@pytest.fixture
def fake_redis():
    """真 fakeredis 后端的 RedisClient(比 AsyncMock 高保真:读到的是真值/None,
    不会像 AsyncMock 把协程对象塞进路由导致假 500)。与 test_api_runner 等一致。"""
    rc = RedisClient()
    rc._redis = fakeredis.aioredis.FakeRedis(decode_responses=True, protocol=2)
    return rc


@pytest.fixture
def fuzz_app(db, fake_redis, test_config):
    # 注入 tmp sqlite + fakeredis → create_app 设好 app.state,lifespan 不连真资源。
    return create_app(db=db, redis=fake_redis, config=test_config)


@pytest.fixture
def api_schema(fuzz_app):
    return schemathesis.openapi.from_asgi("/openapi.json", fuzz_app)


# 惰性从 fixture 取 schema(v4 正确入口;见 schemathesis 文档 pytest.from_fixture)
schema = schemathesis.pytest.from_fixture("api_schema")


@schema.parametrize()
@settings(
    max_examples=20,
    # 这些是"生成阶段"的健康检查,非接口缺陷:有的端点(如 runner/.../fail)schema 约束多,
    # hypothesis 会过滤掉大量随机输入而触发 filter_too_much;整体用例较多也可能 too_slow。
    suppress_health_check=[HealthCheck.filter_too_much, HealthCheck.too_slow],
)
def test_api_conformance(case):
    # conftest 已设 API_ALLOW_NO_AUTH=1,受保护端点不会因缺 token 而 503。
    case.call_and_validate()
