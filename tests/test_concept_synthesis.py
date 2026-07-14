"""概念综合只在来源门与 CAS 前态同时满足时切换版本。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from api.services import concepts
from shared.db import ConceptConflictError
from shared.models import LLMResponse
from shared.prompt_resolver import PromptResolver, TRACKED_TEMPLATE_NAMES


class FakeDatabase:
    def __init__(self, *, locked: bool = False, fingerprint: str = "old"):
        self.row = {
            "domain": "ml",
            "term": "RRF",
            "definition": "old definition",
            "definition_locked": locked,
            "status": "accepted",
            "current_definition_version_id": "cdv_old",
            "lock_revision": 3,
        }
        self.current = {
            "definition_version_id": "cdv_old",
            "definition": "old definition",
            "source_set_fingerprint": fingerprint,
        }
        self.appended: list[dict] = []
        self.append_error: Exception | None = None

    def get_glossary_term(self, domain: str, term: str):
        assert (domain, term) == ("ml", "RRF")
        return dict(self.row)

    def current_concept_definition(self, domain: str, term: str):
        assert (domain, term) == ("ml", "RRF")
        return dict(self.current)

    def append_concept_definition_version(self, **kwargs):
        self.appended.append(kwargs)
        if self.append_error is not None:
            raise self.append_error
        return {
            "created": True,
            "definition_version_id": "cdv_new",
            "supersedes_version_id": "cdv_old",
            **kwargs,
        }


class FakeGateway:
    def __init__(self, content: str = '{"definition":"new definition"}'):
        self.content = content
        self.calls: list[tuple[str, object]] = []
        self.error: Exception | None = None

    async def call(self, step_name: str, request):
        self.calls.append((step_name, request))
        if self.error is not None:
            raise self.error
        return LLMResponse(
            content=self.content,
            provider="test-provider",
            model="test-model",
        )


def _attestation(*, jobs: int = 2, sources: int = 2, fingerprint: str = "new"):
    included = [
        {
            "evidence_id": f"ce_{index:064x}",
            "job_id": f"job-{index}",
            "content_type": "article",
            "source_fingerprint": f"source-{index}",
            "note_type": "smart",
            "section": "S",
            "excerpt": f"fact-{index}",
            "locator": {"kind": "text", "exact": f"quote-{index}"},
        }
        for index in range(1, max(jobs, sources) + 1)
    ]
    return {
        "job_count": jobs,
        "source_fingerprint_count": sources,
        "source_set_fingerprint": fingerprint,
        "included": included,
        "excluded": [],
    }


def _resolver(tmp_path) -> PromptResolver:
    hot = tmp_path / "hot"
    image = tmp_path / "image"
    image.mkdir()
    (image / "concept_resynthesis.md").write_text("tracked prompt", encoding="utf-8")
    return PromptResolver(hot_dir=hot, image_dir=image)


def _config(tmp_path):
    return SimpleNamespace(
        prompts_dir=tmp_path / "hot",
        config_dir=tmp_path,
        providers={"providers": {}},
        pipelines={
            "article": {"steps": [{
                "name": "05_concepts",
                "ai": {"primary": {"provider": "p", "model": "m"}},
            }]},
        },
    )


async def _run(
    monkeypatch, tmp_path, db: FakeDatabase, gateway: FakeGateway,
    attestation: dict,
):
    async def project(*_args):
        return attestation

    monkeypatch.setattr(concepts, "project_concept_attestation", project)
    return await concepts.maybe_resynthesize_concept(
        db,
        object(),
        _config(tmp_path),
        "ml",
        "RRF",
        expected_current_version_id="cdv_old",
        expected_lock_revision=3,
        actor="test:manual",
        strategy="manual_resynthesis",
        gateway=gateway,
        prompt_resolver=_resolver(tmp_path),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(("state", "reason"), [
    ("locked", "locked"),
    ("no_jobs", "no_quorum"),
    ("no_sources", "no_quorum"),
    ("unchanged", "source_set_unchanged"),
])
async def test_noop_states_never_call_provider_or_append(
    monkeypatch, tmp_path, state: str, reason: str,
):
    db = FakeDatabase(
        locked=state == "locked",
        fingerprint="same" if state == "unchanged" else "old",
    )
    attestation = _attestation(
        jobs=1 if state == "no_jobs" else 2,
        sources=1 if state == "no_sources" else 2,
        fingerprint="same" if state == "unchanged" else "new",
    )
    gateway = FakeGateway()

    result = await _run(monkeypatch, tmp_path, db, gateway, attestation)

    assert result["created"] is False and result["reason"] == reason
    assert gateway.calls == []
    assert db.appended == []


@pytest.mark.asyncio
async def test_success_persists_gateway_prompt_input_and_predecessor_metadata(
    monkeypatch, tmp_path,
):
    db = FakeDatabase()
    gateway = FakeGateway()

    result = await _run(monkeypatch, tmp_path, db, gateway, _attestation())

    assert result["created"] is True
    assert len(gateway.calls) == 1 and gateway.calls[0][0] == "concept_resynthesis"
    request = gateway.calls[0][1]
    assert request.temperature == 0 and request.response_format == "json"
    assert "tracked prompt" in request.messages[0]["content"]
    assert '"excerpt":"fact-1"' in request.messages[0]["content"]
    saved = db.appended[0]
    assert saved["strategy"] == "manual_resynthesis"
    assert saved["provider"] == "test-provider" and saved["model"] == "test-model"
    assert len(saved["prompt_hash"]) == len(saved["input_hash"]) == 64
    assert saved["expected_current_version_id"] == "cdv_old"
    assert saved["expected_lock_revision"] == 3
    assert saved["evidence_ids"] == sorted(saved["evidence_ids"])
    assert result["version"]["supersedes_version_id"] == "cdv_old"


@pytest.mark.asyncio
@pytest.mark.parametrize("content", [
    "not json",
    "{}",
    '{"definition":""}',
    '{"definition":"ok","extra":true}',
])
async def test_parse_failure_never_appends_version(monkeypatch, tmp_path, content: str):
    db = FakeDatabase()
    gateway = FakeGateway(content)
    with pytest.raises(concepts.ConceptSynthesisParseError):
        await _run(monkeypatch, tmp_path, db, gateway, _attestation())
    assert db.appended == []


@pytest.mark.asyncio
async def test_provider_failure_never_appends_version(monkeypatch, tmp_path):
    db = FakeDatabase()
    gateway = FakeGateway()
    gateway.error = RuntimeError("provider failed")
    with pytest.raises(RuntimeError, match="provider failed"):
        await _run(monkeypatch, tmp_path, db, gateway, _attestation())
    assert db.appended == []


@pytest.mark.asyncio
async def test_review_mutation_while_provider_in_flight_never_switches_version(
    monkeypatch, tmp_path,
):
    db = FakeDatabase()
    gateway = FakeGateway()
    before = _attestation()
    after = _attestation()
    after["included"] = after["included"][:1]
    after["job_count"] = 1
    after["source_fingerprint_count"] = 1
    after["source_set_fingerprint"] = "changed-after-review"
    projections = iter((before, after))

    async def project(*_args):
        return next(projections)

    monkeypatch.setattr(concepts, "project_concept_attestation", project)
    with pytest.raises(ConceptConflictError, match="evidence changed"):
        await concepts.maybe_resynthesize_concept(
            db,
            object(),
            _config(tmp_path),
            "ml",
            "RRF",
            expected_current_version_id="cdv_old",
            expected_lock_revision=3,
            actor="test:manual",
            strategy="manual_resynthesis",
            gateway=gateway,
            prompt_resolver=_resolver(tmp_path),
        )

    assert len(gateway.calls) == 1
    assert db.appended == []
    assert db.row["current_definition_version_id"] == "cdv_old"


@pytest.mark.asyncio
async def test_excerpt_mutation_with_same_evidence_ids_invalidates_provider_input(
    monkeypatch, tmp_path,
):
    db = FakeDatabase()
    gateway = FakeGateway()
    before = _attestation()
    after = _attestation()
    after["included"][0]["excerpt"] = "fact changed after provider started"
    projections = iter((before, after))

    async def project(*_args):
        return next(projections)

    monkeypatch.setattr(concepts, "project_concept_attestation", project)
    with pytest.raises(ConceptConflictError, match="evidence changed"):
        await concepts.maybe_resynthesize_concept(
            db,
            object(),
            _config(tmp_path),
            "ml",
            "RRF",
            expected_current_version_id="cdv_old",
            expected_lock_revision=3,
            actor="test:manual",
            strategy="manual_resynthesis",
            gateway=gateway,
            prompt_resolver=_resolver(tmp_path),
        )
    assert db.appended == []


@pytest.mark.asyncio
async def test_append_cas_race_propagates_without_second_attempt(monkeypatch, tmp_path):
    db = FakeDatabase()
    db.append_error = ConceptConflictError("race")
    gateway = FakeGateway()
    with pytest.raises(ConceptConflictError, match="race"):
        await _run(monkeypatch, tmp_path, db, gateway, _attestation())
    assert len(db.appended) == 1


@pytest.mark.asyncio
async def test_wrong_expected_revision_conflicts_before_projection_or_provider(
    monkeypatch, tmp_path,
):
    async def project(*_args):
        raise AssertionError("projection must not run")

    monkeypatch.setattr(concepts, "project_concept_attestation", project)
    db = FakeDatabase()
    gateway = FakeGateway()
    with pytest.raises(ConceptConflictError, match="已变化"):
        await concepts.maybe_resynthesize_concept(
            db, object(), _config(tmp_path), "ml", "RRF",
            expected_current_version_id="cdv_stale",
            expected_lock_revision=3,
            actor="test", strategy="manual", gateway=gateway,
            prompt_resolver=_resolver(tmp_path),
        )
    assert gateway.calls == [] and db.appended == []


@pytest.mark.asyncio
async def test_pointer_race_between_row_and_current_read_conflicts_before_provider(
    monkeypatch, tmp_path,
):
    async def project(*_args):
        raise AssertionError("projection must not run")

    monkeypatch.setattr(concepts, "project_concept_attestation", project)
    db = FakeDatabase()
    db.current["definition_version_id"] = "cdv_raced"
    gateway = FakeGateway()
    with pytest.raises(ConceptConflictError, match="已变化"):
        await concepts.maybe_resynthesize_concept(
            db, object(), _config(tmp_path), "ml", "RRF",
            expected_current_version_id="cdv_old",
            expected_lock_revision=3,
            actor="test", strategy="manual", gateway=gateway,
            prompt_resolver=_resolver(tmp_path),
        )
    assert gateway.calls == [] and db.appended == []


def test_concept_resynthesis_prompt_is_tracked():
    assert "concept_resynthesis" in TRACKED_TEMPLATE_NAMES


@pytest.mark.asyncio
async def test_aggregate_input_bound_is_noop_before_provider(monkeypatch, tmp_path):
    db = FakeDatabase()
    gateway = FakeGateway()
    attestation = _attestation()
    attestation["included"][0]["excerpt"] = "证" * (128 * 1024)

    result = await _run(monkeypatch, tmp_path, db, gateway, attestation)

    assert result["created"] is False and result["reason"] == "input_too_large"
    assert gateway.calls == [] and db.appended == []
