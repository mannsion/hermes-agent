"""Kanban tools are available in ordinary chats without assigning them a task."""

import os
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.agent_init import _load_tools
from agent.delegation_context import (
    delegated_child_context,
    non_dispatcher_owned_context,
)
from agent.prompt_builder import KANBAN_GUIDANCE
from agent.system_prompt import build_system_prompt_parts


CONTEXTS = pytest.mark.parametrize(
    "task_id,context,enabled,worker",
    [
        (None, nullcontext, True, False),
        ("", nullcontext, True, False),
        ("   ", nullcontext, True, False),
        ("t_assigned", nullcontext, True, True),
        ("t_assigned", non_dispatcher_owned_context, True, False),
        ("t_assigned", delegated_child_context, True, False),
        ("t_assigned", nullcontext, False, False),
    ],
    ids=["chat", "empty", "blank", "worker", "cron", "delegate", "disabled"],
)


def _agent():
    return SimpleNamespace(
        quiet_mode=True,
        load_soul_identity=False,
        skip_context_files=True,
        valid_tool_names=set(),
        _task_completion_guidance=False,
        _parallel_tool_call_guidance=False,
        _tool_use_enforcement=False,
        _execution_guidance=False,
        _environment_probe=False,
        _bot_mode_protocol=False,
        _memory_store=None,
        _memory_manager=None,
        model="",
        provider="",
        platform="",
        pass_session_id=False,
        session_id="",
    )


def _set_task(monkeypatch, task_id):
    if task_id is None:
        monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    else:
        monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)


@CONTEXTS
def test_initialized_kanban_prompt_preserves_session_task_ownership(
    monkeypatch, tmp_path, task_id, context, enabled, worker,
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
    (Path(os.environ["HERMES_HOME"]) / "config.yaml").write_text("toolsets: [kanban]\n")
    _set_task(monkeypatch, task_id)
    agent = _agent()
    with context():
        _load_tools(agent, ["kanban"], None if enabled else ["kanban"])
        if enabled and context is not delegated_child_context:
            assert {"kanban_show", "kanban_create"} <= agent.valid_tool_names
        elif not enabled:
            assert "kanban_show" not in agent.valid_tool_names

    # Construction fixes ownership, even if the environment changes before the
    # first prompt build (or the context owning construction has exited).
    _set_task(monkeypatch, None if worker else "t_later")
    stable = build_system_prompt_parts(agent)["stable"]
    assert (KANBAN_GUIDANCE in stable) is worker
    _set_task(monkeypatch, "t_assigned" if worker else None)
    with non_dispatcher_owned_context():
        assert build_system_prompt_parts(agent)["stable"] == stable


@CONTEXTS
def test_prompt_assembly_fallback_caches_task_ownership(
    monkeypatch, tmp_path, task_id, context, enabled, worker,
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
    _set_task(monkeypatch, task_id)
    agent = _agent()
    agent.valid_tool_names = {"kanban_show", "kanban_create"} if enabled else set()
    with context():
        stable = build_system_prompt_parts(agent)["stable"]
    assert (KANBAN_GUIDANCE in stable) is worker

    # Fallback assembly must freeze both an empty and a populated worker block.
    _set_task(monkeypatch, None if worker else "t_later")
    assert build_system_prompt_parts(agent)["stable"] == stable
    with non_dispatcher_owned_context():
        assert build_system_prompt_parts(agent)["stable"] == stable
