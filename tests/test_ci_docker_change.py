"""验证 Dockerfile 纯注释变化不触发运行镜像发布."""

import subprocess

import pytest

from scripts import ci_docker_change
from scripts.ci_docker_change import has_relevant_change


BASE = """\
# 普通说明
FROM python:3.11-slim
RUN echo ok
"""


def test_only_ordinary_comments_are_ignored() -> None:
    head = BASE.replace("# 普通说明", "# 更新后的普通说明")

    assert has_relevant_change("docker/base.Dockerfile", BASE, head) is False


def test_instruction_change_requires_backend_build() -> None:
    head = BASE.replace("RUN echo ok", "RUN echo changed")

    assert has_relevant_change("docker/base.Dockerfile", BASE, head) is True


@pytest.mark.parametrize(
    ("base_directive", "head_directive"),
    [
        ("# syntax=docker/dockerfile:1", "# syntax=docker/dockerfile:1.9"),
        ("#syntax=docker/dockerfile:1", "#syntax=docker/dockerfile:1.9"),
        ("#\tcheck = skip=JSONArgsRecommended", "#\tcheck = error=true"),
    ],
)
def test_parser_directive_change_requires_backend_build(
    base_directive: str, head_directive: str
) -> None:
    base = base_directive + "\n" + BASE
    head = head_directive + "\n" + BASE

    assert has_relevant_change("docker/base.Dockerfile", base, head) is True


def test_heredoc_comment_content_is_not_ignored() -> None:
    base = "FROM alpine\nRUN <<'payload.marker'\n# payload-a\npayload.marker\n"
    head = base.replace("payload-a", "payload-b")

    assert has_relevant_change("docker/base.Dockerfile", base, head) is True


def test_comment_containing_heredoc_marker_is_still_ignored() -> None:
    base = BASE + "# example: RUN <<EOF\n"
    head = BASE + "# example: RUN <<PAYLOAD\n"

    assert has_relevant_change("docker/base.Dockerfile", base, head) is False


def test_non_dockerfile_change_requires_backend_build() -> None:
    assert has_relevant_change("docker/entrypoint.sh", "# a", "# b") is True


def test_custom_frontend_pathspec_ignores_dockerfile_comment(
    monkeypatch, capsys
) -> None:
    seen: list[str] = []
    monkeypatch.setattr(
        ci_docker_change,
        "changed_paths",
        lambda _base, _head, pathspecs: seen.extend(pathspecs)
        or ["frontend/Dockerfile"],
    )
    monkeypatch.setattr(
        ci_docker_change,
        "git_file",
        lambda revision, _path: BASE
        if revision == "base"
        else BASE.replace("# 普通说明", "# 新说明"),
    )

    assert (
        ci_docker_change.main(
            ["ci_docker_change.py", "base", "head", "frontend"]
        )
        == 0
    )
    assert seen == ["frontend"]
    assert capsys.readouterr().out == "false\n"


def test_git_errors_conservatively_require_backend_build(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        ci_docker_change,
        "changed_paths",
        lambda _base, _head, _pathspecs: (_ for _ in ()).throw(
            subprocess.CalledProcessError(1, "git")
        ),
    )

    assert ci_docker_change.main(["ci_docker_change.py", "base", "head"]) == 0
    assert capsys.readouterr().out == "true\n"
