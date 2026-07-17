import pytest

from shared.step_scope import (
    JOB_SCOPE,
    execution_step_key,
    parse_execution_step,
    part_id_from_scope,
    part_scope,
    stable_part_id,
)


def test_job_and_part_execution_keys_round_trip() -> None:
    assert execution_step_key(JOB_SCOPE, "09_merge_parts") == "09_merge_parts"
    scope = part_scope("pt_ab12")
    key = execution_step_key(scope, "02_whisper")
    assert key == "part:pt_ab12::02_whisper"
    assert parse_execution_step(key) == (scope, "02_whisper")
    assert part_id_from_scope(scope) == "pt_ab12"
    assert stable_part_id("job_123", 1) == stable_part_id("job_123", 1)
    assert stable_part_id("job_123", 1) != stable_part_id("job_123", 2)


@pytest.mark.parametrize(
    "value",
    ["../p1", "p/1", "p\\1", "", "p\x00x"],
)
def test_part_scope_rejects_path_like_ids(value: str) -> None:
    with pytest.raises(ValueError):
        part_scope(value)


@pytest.mark.parametrize(
    "value",
    ["part:p1::02_whisper::extra", "other:p1::02_whisper", "part:../p1::x"],
)
def test_execution_key_rejects_ambiguous_or_untrusted_scope(value: str) -> None:
    with pytest.raises(ValueError):
        parse_execution_step(value)
