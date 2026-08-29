"""Module-attribute substitution — a plain context-manager equivalent of
pytest's `monkeypatch` fixture, usable both from the demo CLI (which has no
pytest fixture available) and from the demo test suite. This is the exact
same seam every agent test in this codebase already patches
(`app.agents.planning.InMemoryRunner`, etc.) — every Quipu agent imports
`InMemoryRunner`/`JiraClient` into its own module namespace, so replacing
the name there swaps what `_perform()` constructs without touching agent
code at all. Nothing about agent *behavior* is faked — only the ADK
runner/external-client construction point.
"""

from collections.abc import Iterator
from contextlib import contextmanager, ExitStack


@contextmanager
def patched_attr(module, name: str, value) -> Iterator[None]:
    original = getattr(module, name)
    setattr(module, name, value)
    try:
        yield
    finally:
        setattr(module, name, original)


@contextmanager
def demo_agent_runner_patches(
    *,
    plan_text: str | None = None,
    architecture_text: str | None = None,
    codegen_text: str | None = None,
    testing_text: str | None = None,
    deployment_text: str | None = None,
    detecting_text: str | None = None,
    resolution_text: str | None = None,
    deployment_succeeds: bool = True,
) -> Iterator[None]:
    """Patches only the agent modules whose fixture text was supplied —
    callers pass just what a given step needs. Restores every patched
    attribute on exit, even if the block raises."""
    import app.agents.architecture as architecture_module
    import app.agents.codegen as codegen_module
    import app.agents.deployment as deployment_module
    import app.agents.detecting as detecting_module
    import app.agents.incident_resolution as incident_resolution_module
    import app.agents.planning as planning_module
    import app.agents.testing as testing_module
    import app.tools.deployment_tools as deployment_tools_module

    from app.demo.fakes import (
        FakeCloudRunSettings,
        FakeJiraClient,
        make_codegen_runner,
        make_deployment_runner,
        make_plain_runner,
        make_testing_runner,
    )

    with ExitStack() as stack:
        if plan_text is not None:
            stack.enter_context(patched_attr(planning_module, "InMemoryRunner", make_plain_runner(plan_text)))
            stack.enter_context(patched_attr(planning_module, "JiraClient", FakeJiraClient))
        if architecture_text is not None:
            stack.enter_context(patched_attr(architecture_module, "InMemoryRunner", make_plain_runner(architecture_text)))
        if codegen_text is not None:
            stack.enter_context(patched_attr(codegen_module, "InMemoryRunner", make_codegen_runner(codegen_text)))
        if testing_text is not None:
            stack.enter_context(patched_attr(testing_module, "InMemoryRunner", make_testing_runner(testing_text)))
        if deployment_text is not None:
            stack.enter_context(patched_attr(deployment_tools_module, "get_settings", lambda: FakeCloudRunSettings()))
            stack.enter_context(patched_attr(deployment_module, "get_settings", lambda: FakeCloudRunSettings()))
            stack.enter_context(
                patched_attr(deployment_module, "InMemoryRunner", make_deployment_runner(deployment_text, succeed=deployment_succeeds))
            )
        if detecting_text is not None:
            stack.enter_context(patched_attr(detecting_module, "InMemoryRunner", make_plain_runner(detecting_text)))
        if resolution_text is not None:
            stack.enter_context(patched_attr(incident_resolution_module, "InMemoryRunner", make_plain_runner(resolution_text)))
        yield
