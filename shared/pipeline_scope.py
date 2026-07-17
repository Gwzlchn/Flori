"""把声明式 pipeline 展开为 Job 与 Part 执行节点。"""

from __future__ import annotations

from collections.abc import Iterable

from .models import JobPart
from .step_scope import JOB_SCOPE, execution_step_key, part_scope


def validate_pipeline_scopes(pipelines: dict) -> None:
    """拒绝跨 scope 歧义依赖,fan-in 只允许 Job 聚合 Part。"""
    for pipeline_name, pipeline in pipelines.items():
        steps = pipeline.get("steps", [])
        by_name = {step.get("name"): step for step in steps}
        if len(by_name) != len(steps) or None in by_name:
            raise ValueError(f"pipeline has duplicate or unnamed steps: {pipeline_name}")
        for name, step in by_name.items():
            scope = step.get("scope", JOB_SCOPE)
            if scope not in {JOB_SCOPE, "part"}:
                raise ValueError(f"invalid step scope: {pipeline_name}/{name}/{scope}")
            for dependency in step.get("depends_on", []):
                target = by_name.get(dependency)
                if target is None:
                    # 归一化工具允许加载测试/管理界面的局部 pipeline 片段。
                    # 依赖存在性仍由既有 DAG 校验负责,这里只校验可见节点的 scope。
                    continue
                if target.get("scope", JOB_SCOPE) != scope:
                    raise ValueError(
                        f"cross-scope dependency requires fan_in: "
                        f"{pipeline_name}/{name}/{dependency}"
                    )
            fan_in = step.get("fan_in", [])
            if fan_in and scope != JOB_SCOPE:
                raise ValueError(f"part step cannot fan in: {pipeline_name}/{name}")
            for dependency in fan_in:
                target = by_name.get(dependency)
                if target is None or target.get("scope", JOB_SCOPE) != "part":
                    raise ValueError(
                        f"fan_in must reference part step: "
                        f"{pipeline_name}/{name}/{dependency}"
                    )


def expand_pipeline_steps(
    steps: Iterable[dict], parts: Iterable[JobPart],
) -> dict[str, dict]:
    """按不可变 Part 清单展开运行节点并生成确定性依赖。"""
    templates = {step["name"]: step for step in steps}
    ordered_parts = sorted(parts, key=lambda item: item.part_index)
    expanded: dict[str, dict] = {}
    for name, template in templates.items():
        scope = template.get("scope", JOB_SCOPE)
        if scope == "part":
            for part in ordered_parts:
                scope_key = part_scope(part.id)
                key = execution_step_key(scope_key, name)
                cfg = dict(template)
                cfg.update(
                    name=key,
                    template_step=name,
                    scope_key=scope_key,
                    part_id=part.id,
                    part_index=part.part_index,
                    depends_on=[
                        execution_step_key(scope_key, dependency)
                        for dependency in template.get("depends_on", [])
                    ],
                )
                expanded[key] = cfg
            continue
        cfg = dict(template)
        dependencies = list(template.get("depends_on", []))
        for dependency in template.get("fan_in", []):
            dependencies.extend(
                execution_step_key(part_scope(part.id), dependency)
                for part in ordered_parts
            )
        cfg.update(
            name=name,
            template_step=name,
            scope_key=JOB_SCOPE,
            depends_on=dependencies,
        )
        expanded[name] = cfg
    return expanded
