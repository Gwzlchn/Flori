"""shared/credentials 单测:提取/派生/镜像 + worker 凭证 env 组装。"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from tests.conftest import make_fakeredis
from shared.credentials import (
    DISPATCH_KEYS,
    derive_dispatch,
    extract_bili_sessdata,
    mirror_all_from_db,
    mirror_credential,
    resolve_from_db,
)
from shared.db import Database


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "t.db")
    d.init_schema()
    yield d
    d.close()


@pytest.fixture
async def redis():
    client = make_fakeredis()
    yield client
    await client.close()


class TestExtractAndDerive:
    def test_extract_sessdata(self):
        assert extract_bili_sessdata(json.dumps({"sessdata": "abc%2C"})) == "abc%2C"

    def test_extract_bad_inputs(self):
        assert extract_bili_sessdata(None) is None
        assert extract_bili_sessdata("") is None
        assert extract_bili_sessdata("not json {{{") is None
        assert extract_bili_sessdata(json.dumps({"uname": "x"})) is None

    def test_derive_bili(self):
        raw = json.dumps({"sessdata": "s1"})
        assert derive_dispatch("bili_cookies", raw) == {"bili_sessdata": "s1"}
        assert derive_dispatch("bili_cookies", None) == {"bili_sessdata": None}

    def test_derive_youtube_strips_empty(self):
        assert derive_dispatch("youtube_cookies", "  ") == {"youtube_cookies": None}
        assert derive_dispatch("youtube_cookies", "# nc") == {"youtube_cookies": "# nc"}

    def test_derive_unknown_key_empty(self):
        assert derive_dispatch("other_secret", "v") == {}


class TestMirror:
    @pytest.mark.asyncio
    async def test_mirror_set_and_clear(self, redis):
        await mirror_credential(redis, "bili_cookies", json.dumps({"sessdata": "s1"}))
        assert await redis.get_dispatch_credential("bili_sessdata") == "s1"
        # 清除(登出)→ 镜像删除
        await mirror_credential(redis, "bili_cookies", None)
        assert await redis.get_dispatch_credential("bili_sessdata") is None

    @pytest.mark.asyncio
    async def test_mirror_all_from_db(self, redis, db):
        db.set_credential("bili_cookies", json.dumps({"sessdata": "s2"}))
        db.set_credential("youtube_cookies", "# nc")
        await mirror_all_from_db(redis, db)
        assert await redis.get_dispatch_credential("bili_sessdata") == "s2"
        assert await redis.get_dispatch_credential("youtube_cookies") == "# nc"

    def test_resolve_from_db(self, db):
        db.set_credential("bili_cookies", json.dumps({"sessdata": "s3"}))
        assert resolve_from_db(db, "bili_sessdata") == "s3"
        assert resolve_from_db(db, "youtube_cookies") is None
        assert resolve_from_db(db, "not_a_key") is None


class TestWorkerCredentialEnv:
    """worker._download_credentials_env:按 source 领取 → env;失败降级匿名。"""

    def _worker(self, transport):
        from worker.worker import Worker
        w = Worker.__new__(Worker)
        w.transport = transport
        return w

    @pytest.mark.asyncio
    async def test_non_download_step_empty(self):
        w = self._worker(AsyncMock())
        assert await w._download_credentials_env("02_whisper", "bilibili") == {}
        w.transport.get_credential.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_bili_source_fetches_sessdata_only(self):
        t = AsyncMock()
        t.get_credential.return_value = "sess-1"
        w = self._worker(t)
        env = await w._download_credentials_env("01_download", "bilibili")
        assert env == {"BILI_SESSDATA": "sess-1"}
        t.get_credential.assert_awaited_once_with("bili_sessdata")

    @pytest.mark.asyncio
    async def test_youtube_source_fetches_cookies(self):
        t = AsyncMock()
        t.get_credential.return_value = "# nc"
        w = self._worker(t)
        env = await w._download_credentials_env("01_download", "youtube")
        assert env == {"FLORI_YT_COOKIES": "# nc"}

    @pytest.mark.asyncio
    async def test_non_platform_source_skips(self):
        w = self._worker(AsyncMock())
        assert await w._download_credentials_env("01_download", "arxiv") == {}
        w.transport.get_credential.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unknown_source_tries_both(self):
        t = AsyncMock()
        t.get_credential.side_effect = ["s1", "# nc"]
        w = self._worker(t)
        env = await w._download_credentials_env("01_download", "")
        assert env == {"BILI_SESSDATA": "s1", "FLORI_YT_COOKIES": "# nc"}

    @pytest.mark.asyncio
    async def test_fetch_error_degrades_anonymous(self):
        t = AsyncMock()
        t.get_credential.side_effect = RuntimeError("gateway down")
        w = self._worker(t)
        assert await w._download_credentials_env("01_download", "bilibili") == {}

    @pytest.mark.asyncio
    async def test_unconfigured_value_omitted(self):
        t = AsyncMock()
        t.get_credential.return_value = None
        w = self._worker(t)
        assert await w._download_credentials_env("01_download", "youtube") == {}


class TestStepRunnerExtraEnv:
    def test_subprocess_env_merges_extra(self, tmp_path):
        from worker.step_runner import StepContext, _build_subprocess_env
        ctx = StepContext(
            job_id="j", step="01_download", work_dir=tmp_path, exec_id="e1",
            step_cfg={}, module="m", pool="io",
            extra_env={"BILI_SESSDATA": "tok"},
        )
        env = _build_subprocess_env(ctx)
        assert env["BILI_SESSDATA"] == "tok"
        assert env["STEP_EXEC_ID"] == "e1"

    def test_default_no_extra(self, tmp_path):
        from worker.step_runner import StepContext, _build_subprocess_env
        ctx = StepContext(
            job_id="j", step="s", work_dir=tmp_path, exec_id="e2",
            step_cfg={}, module="m", pool="cpu",
        )
        env = _build_subprocess_env(ctx)
        assert "BILI_SESSDATA" not in env or env["BILI_SESSDATA"] == \
            __import__("os").environ.get("BILI_SESSDATA")


def test_dispatch_keys_frozen_contract():
    # 白名单契约(docs/03 §1.7.1):新增凭证种类须同步文档,此断言防悄悄扩散。
    assert set(DISPATCH_KEYS) == {"bili_sessdata", "youtube_cookies"}
