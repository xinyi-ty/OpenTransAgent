from __future__ import annotations

import pytest

from tools import registry
from tools.registry import BUILTIN_TOOL_DEFINITIONS, TOOL_DEFINITIONS, create_tools


def test_create_tools_excludes_requested_tools() -> None:
    tools = create_tools(exclude={"reflect"}, workspace_root=".")
    names = [tool.name for tool in tools]

    assert "reflect" not in names
    assert "read_file" in names


def test_create_tools_does_not_create_builtin_tools() -> None:
    tools = create_tools(workspace_root=".")
    names = {tool.name for tool in tools}

    assert "finish" not in names
    assert "think" not in names
    assert set(BUILTIN_TOOL_DEFINITIONS).isdisjoint(names)


def test_tool_definitions_are_valid() -> None:
    registry.validate_tool_definitions()

    assert set(TOOL_DEFINITIONS).isdisjoint(BUILTIN_TOOL_DEFINITIONS)
    assert all(desc.strip() for desc in TOOL_DEFINITIONS.values())
    assert all(desc.strip() for desc in BUILTIN_TOOL_DEFINITIONS.values())


def test_validate_tool_definitions_rejects_duplicate_builtin(monkeypatch) -> None:
    monkeypatch.setitem(BUILTIN_TOOL_DEFINITIONS, "read_file", "duplicate")

    with pytest.raises(ValueError, match="同时出现在"):
        registry.validate_tool_definitions()


def test_validate_tool_definitions_rejects_empty_description(monkeypatch) -> None:
    monkeypatch.setitem(TOOL_DEFINITIONS, "temporary_tool", "")

    with pytest.raises(ValueError, match="缺少工具描述"):
        registry.validate_tool_definitions()
