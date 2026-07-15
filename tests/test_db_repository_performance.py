"""冻结 façade 单次直接委派,等价 SQL 和主键查询计划."""

from __future__ import annotations

import ast
import inspect
import textwrap
import types

import pytest

import shared.db as db_module
from shared.db import Database
from shared.models import Job
from shared.repositories.jobs import JobsReadRepository


def _assert_direct_get_job_facade(function: object) -> None:
    """校验 façade 只有一次原样 repository 委派."""
    assert isinstance(function, types.FunctionType)
    signature = inspect.signature(function)
    parameters = list(signature.parameters.values())
    assert [parameter.name for parameter in parameters] == ["self", "job_id"]
    assert all(
        parameter.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
        and parameter.default is inspect.Parameter.empty
        for parameter in parameters
    )

    source = textwrap.dedent(inspect.getsource(function))
    module = ast.parse(source)
    assert len(module.body) == 1
    node = module.body[0]
    assert isinstance(node, ast.FunctionDef)
    assert node.name == "get_job"
    assert node.decorator_list == []
    assert len(node.body) == 1
    statement = node.body[0]
    assert isinstance(statement, ast.Return)
    call = statement.value
    assert isinstance(call, ast.Call)
    assert isinstance(call.func, ast.Attribute)
    assert isinstance(call.func.value, ast.Name)
    assert call.func.value.id == "_JobsReadRepository"
    assert call.func.attr == "get_job"
    assert [argument.id for argument in call.args if isinstance(argument, ast.Name)] == [
        "self", "job_id",
    ]
    assert len(call.args) == 2
    assert call.keywords == []


def test_job_read_facade_is_one_direct_repository_delegation(
    db: Database, monkeypatch: pytest.MonkeyPatch,
) -> None:
    function = Database.__dict__["get_job"]
    _assert_direct_get_job_facade(function)
    assert db_module._JobsReadRepository is JobsReadRepository
    assert Database.__getattribute__ is object.__getattribute__

    calls: list[tuple[Database, object]] = []
    sentinel = object()
    job_id = object()

    def spy(database: Database, value: object) -> object:
        calls.append((database, value))
        return sentinel

    monkeypatch.setattr(JobsReadRepository, "get_job", spy)
    assert db.get_job(job_id) is sentinel
    assert len(calls) == 1
    assert calls[0][0] is db
    assert calls[0][1] is job_id


def test_facade_shape_validator_rejects_indirect_work() -> None:
    def decorator(function):
        return function

    class ExtraStatement:
        def get_job(self, job_id):
            job_id = str(job_id)
            return _JobsReadRepository.get_job(self, job_id)

    class Decorated:
        @decorator
        def get_job(self, job_id):
            return _JobsReadRepository.get_job(self, job_id)

    class ArgumentTransform:
        def get_job(self, job_id):
            return _JobsReadRepository.get_job(self, str(job_id))

    class DoubleCall:
        def get_job(self, job_id):
            _JobsReadRepository.get_job(self, job_id)
            return _JobsReadRepository.get_job(self, job_id)

    invalid_functions = (
        ExtraStatement.__dict__["get_job"],
        Decorated.__dict__["get_job"],
        ArgumentTransform.__dict__["get_job"],
        DoubleCall.__dict__["get_job"],
    )
    for invalid in invalid_functions:
        with pytest.raises(AssertionError):
            _assert_direct_get_job_facade(invalid)


def test_job_read_facade_preserves_sql_statement_count(db: Database) -> None:
    job = Job(
        id="jobs_repository_trace",
        content_type="article",
        pipeline="article",
        title="repository trace",
    )
    db.create_job(job)

    def trace(call) -> list[str]:
        statements: list[str] = []
        db._conn.set_trace_callback(statements.append)
        try:
            call()
        finally:
            db._conn.set_trace_callback(None)
        return statements

    facade = trace(lambda: db.get_job(job.id))
    direct = trace(lambda: JobsReadRepository.get_job(db, job.id))
    assert facade == direct
    assert len(facade) == 1


def test_job_read_uses_primary_key_query_plan(db: Database) -> None:
    plan = db._conn.execute(
        "EXPLAIN QUERY PLAN SELECT * FROM jobs WHERE id=?", ("missing",)
    ).fetchall()
    detail = "\n".join(str(row[3]) for row in plan)
    assert "SEARCH jobs USING INDEX sqlite_autoindex_jobs_1 (id=?)" in detail
