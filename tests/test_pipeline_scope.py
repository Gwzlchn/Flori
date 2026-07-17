from shared.models import JobPart
from shared.pipeline_scope import expand_pipeline_steps, validate_pipeline_scopes


def _part(part_id: str, index: int) -> JobPart:
    return JobPart(id=part_id, job_id="job-1", part_index=index)


def test_expand_pipeline_steps_maps_parts_and_fans_in_in_order() -> None:
    steps = [
        {"name": "01_download", "scope": "part", "depends_on": []},
        {"name": "02_whisper", "scope": "part", "depends_on": ["01_download"]},
        {"name": "09_merge_parts", "scope": "job", "fan_in": ["02_whisper"]},
        {"name": "10_mechanical", "scope": "job", "depends_on": ["09_merge_parts"]},
    ]
    expanded = expand_pipeline_steps(steps, [_part("pt_b", 2), _part("pt_a", 1)])
    assert list(expanded) == [
        "part:pt_a::01_download", "part:pt_b::01_download",
        "part:pt_a::02_whisper", "part:pt_b::02_whisper",
        "09_merge_parts", "10_mechanical",
    ]
    assert expanded["part:pt_b::02_whisper"]["depends_on"] == [
        "part:pt_b::01_download"
    ]
    assert expanded["09_merge_parts"]["depends_on"] == [
        "part:pt_a::02_whisper", "part:pt_b::02_whisper",
    ]


def test_validate_pipeline_scopes_rejects_implicit_cross_scope_dependency() -> None:
    pipelines = {"video": {"steps": [
        {"name": "01_download", "scope": "part", "depends_on": []},
        {"name": "09_merge_parts", "scope": "job", "depends_on": ["01_download"]},
    ]}}
    try:
        validate_pipeline_scopes(pipelines)
    except ValueError as exc:
        assert "requires fan_in" in str(exc)
    else:
        raise AssertionError("cross-scope dependency must fail")
