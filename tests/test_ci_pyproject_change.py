"""验证 CI 对 pyproject 实质变化的路径分类."""

from scripts import ci_pyproject_change
from scripts.ci_pyproject_change import has_relevant_change


BASE = """\
[project]
name = "flori"
version = "1.5.6"
dependencies = ["redis>=8,<9"]

[tool.pytest.ini_options]
testpaths = ["tests"]
"""


def test_only_project_version_change_is_ignored() -> None:
    head = BASE.replace('version = "1.5.6"', 'version = "1.5.7"')

    assert has_relevant_change(BASE, head) is False


def test_removing_project_version_requires_backend_build() -> None:
    head = BASE.replace('version = "1.5.6"\n', "")

    assert has_relevant_change(BASE, head) is True


def test_invalid_project_version_requires_backend_build() -> None:
    head = BASE.replace('version = "1.5.6"', 'version = "next"')

    assert has_relevant_change(BASE, head) is True


def test_comment_and_format_changes_are_ignored() -> None:
    head = BASE.replace('name = "flori"', '# package name\nname="flori"')

    assert has_relevant_change(BASE, head) is False


def test_dependency_change_requires_backend_build() -> None:
    head = BASE.replace('"redis>=8,<9"', '"redis>=8,<10"')

    assert has_relevant_change(BASE, head) is True


def test_test_configuration_change_requires_backend_build() -> None:
    head = BASE.replace('testpaths = ["tests"]', 'testpaths = ["tests", "integration"]')

    assert has_relevant_change(BASE, head) is True


def test_invalid_toml_conservatively_requires_backend_build(monkeypatch, capsys) -> None:
    monkeypatch.setattr(ci_pyproject_change, "git_file", lambda _revision: "dependencies = [")

    assert ci_pyproject_change.main(["ci_pyproject_change.py", "base", "head"]) == 0
    assert capsys.readouterr().out == "true\n"
