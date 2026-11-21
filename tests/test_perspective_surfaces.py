"""Tests for perspective support across the service, MCP, CLI, formatting and cache layers."""

from __future__ import annotations

import json
import pickle
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner
from rich.console import Console

from omnifocus.api_service import StoreBackedApiService
from omnifocus.cli import cli
from omnifocus.errors import OFPerspectiveNotFound
from omnifocus.formatting import render_perspective_table, render_perspectives_table
from omnifocus.mcp_server import (
    CORE_TOOLS,
    SERVER_INSTRUCTIONS,
    _handle_complete_task,
    _handle_get_perspective_tasks,
    _handle_list_perspectives,
    call_tool,
    list_tools,
    server,
)
from omnifocus.models import OFModel, Perspective, Project, Tag, Task
from omnifocus.store import _CachePayload
from tests.test_store import _make_store

EPOCH = datetime(2020, 1, 1, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _utc(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OF_TIMEZONE", "UTC")
    monkeypatch.delenv("OF_MCP_TOOLSET", raising=False)


def _task(tid: str, name: str, **kw: Any) -> Task:
    defaults: dict[str, Any] = {
        "parent_task_id": None,
        "project_id": None,
        "inbox": True,
        "completed": None,
        "flagged": False,
        "due": None,
        "start": None,
        "hidden": None,
        "note": "",
        "rank": 0,
        "repetition_rule": None,
        "estimated_minutes": None,
        "added": EPOCH,
        "modified": EPOCH,
    }
    defaults.update(kw)
    return Task(id=tid, name=name, **defaults)


def _model() -> OFModel:
    m = OFModel()
    m.tags["w"] = Tag("w", "waiting", None, 0)
    m.projects["p"] = Project(
        id="p",
        name="Proj",
        folder_id=None,
        status="active",
        singleton=False,
        rank=0,
        added=EPOCH,
        modified=EPOCH,
        flagged=False,
        due=None,
        start=None,
        note="",
        completed=None,
    )
    now = datetime.now()
    m.tasks["a"] = _task("a", "Call Bob", tag_ids=("w",), flagged=True, due=now - timedelta(days=1))
    m.tasks["b"] = _task("b", "Write report", project_id="p", parent_task_id="p", inbox=False)
    m.tasks["c"] = _task("c", "Write report", start=now + timedelta(days=3))
    m.tasks["d"] = _task("d", "Planned only", planned=now - timedelta(hours=1))
    m.perspectives["k"] = Perspective(
        "k", "返信待ち", [{"actionAvailability": "remaining"}, {"actionHasAnyOfTags": ["w"]}]
    )
    m.perspectives["x"] = Perspective("x", "Odd", [{"brandNewRule": 1}])
    return m


def _service(model: OFModel) -> StoreBackedApiService:
    async def _load(_force: bool = False) -> OFModel:
        return model

    @asynccontextmanager
    async def _ctx():  # type: ignore[no-untyped-def]
        yield AsyncMock()

    return StoreBackedApiService(load_model=_load, store_factory=_ctx)


def _json(result: list[Any]) -> Any:
    return json.loads(result[0].text)


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class TestService:
    @pytest.mark.asyncio
    async def test_list_and_get(self) -> None:
        service = _service(_model())
        names = [row["name"] for row in await service.list_perspectives()]
        assert "返信待ち" in names and "Forecast" in names
        payload = await service.get_perspective_tasks(name="返信待ち")
        assert [item["id"] for item in payload["items"]] == ["a"]
        available = await service.get_perspective_tasks(name="inbox", limit=1, only_available=True)
        assert available["returned"] == 1

    @pytest.mark.asyncio
    async def test_unknown_name(self) -> None:
        with pytest.raises(OFPerspectiveNotFound, match="Available perspectives"):
            await _service(_model()).get_perspective_tasks(name="nope")


# ---------------------------------------------------------------------------
# MCP
# ---------------------------------------------------------------------------


def _patched(model: OFModel):  # type: ignore[no-untyped-def]
    return patch("omnifocus.mcp_server._service", return_value=_service(model))


class TestMcp:
    def test_server_has_instructions(self) -> None:
        assert server.instructions == SERVER_INSTRUCTIONS
        assert "get_perspective_tasks" in SERVER_INSTRUCTIONS

    @pytest.mark.asyncio
    async def test_core_toolset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OF_MCP_TOOLSET", "core")
        names = {tool.name for tool in await list_tools()}
        assert names == CORE_TOOLS
        monkeypatch.setenv("OF_MCP_TOOLSET", "full")
        assert len(await list_tools()) == 27

    @pytest.mark.asyncio
    async def test_descriptions_guide_tool_choice(self) -> None:
        tools = {tool.name: tool for tool in await list_tools()}
        assert "search_tasks" in (tools["get_perspective_tasks"].description or "")
        assert tools["get_perspective_tasks"].inputSchema["required"] == ["name"]
        assert "task_id" in tools["complete_task"].inputSchema["properties"]

    @pytest.mark.asyncio
    async def test_perspective_handlers(self) -> None:
        with _patched(_model()):
            rows = _json(await _handle_list_perspectives({}))
            assert any(row["name"] == "Odd" for row in rows)
            data = _json(await _handle_get_perspective_tasks({"name": "Forecast"}))
            assert {item["id"] for item in data["items"]} == {"a", "d"}
            odd = _json(await call_tool("get_perspective_tasks", {"name": "odd", "limit": 5}))
            assert odd["warnings"]
            missing = _json(await _handle_get_perspective_tasks({"name": "nope"}))
            assert "Available perspectives" in missing["error"]


class TestCompleteTaskSafety:
    def _store(self) -> MagicMock:
        store = MagicMock()
        store.__aenter__ = AsyncMock(return_value=store)
        store.__aexit__ = AsyncMock(return_value=None)
        store.complete_task = AsyncMock(return_value={"status": "completed"})
        return store

    async def _call(self, args: dict[str, Any]) -> tuple[Any, MagicMock]:
        store = self._store()
        with patch("omnifocus.mcp_server._load_model", AsyncMock(return_value=_model())):
            with patch("omnifocus.mcp_server.OFocusStore.from_env", return_value=store):
                return _json(await _handle_complete_task(args)), store

    @pytest.mark.asyncio
    async def test_requires_input(self) -> None:
        data, store = await self._call({})
        assert "task_id" in data["error"]
        store.complete_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_task_id(self) -> None:
        data, store = await self._call({"task_id": "b"})
        assert data["status"] == "completed"
        assert store.complete_task.await_args.args[0].id == "b"

    @pytest.mark.asyncio
    async def test_unknown_task_id_does_not_fuzzy_match(self) -> None:
        data, store = await self._call({"task_id": "Call Bob"})
        assert "No remaining task with id" in data["error"]
        store.complete_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_ambiguous_name_returns_candidates(self) -> None:
        data, store = await self._call({"query": "Write report"})
        assert "nothing was completed" in data["error"]
        assert {c["id"] for c in data["candidates"]} == {"b", "c"}
        store.complete_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_unique_exact_name_among_fuzzy_matches(self) -> None:
        data, store = await self._call({"query": "planned only"})
        assert data["status"] == "completed"
        assert store.complete_task.await_args.args[0].id == "d"

    @pytest.mark.asyncio
    async def test_single_fuzzy_match(self) -> None:
        data, store = await self._call({"query": "Call Bo"})
        assert data["status"] == "completed"
        assert store.complete_task.await_args.args[0].id == "a"


# ---------------------------------------------------------------------------
# CLI and formatting
# ---------------------------------------------------------------------------


def _cli_store(model: OFModel) -> MagicMock:
    store = MagicMock()
    store.__aenter__ = AsyncMock(return_value=store)
    store.__aexit__ = AsyncMock(return_value=None)
    store.load = AsyncMock(return_value=model)
    return store


class TestCli:
    def _invoke(self, args: list[str]) -> Any:
        with patch("omnifocus.cli.OFocusStore.from_env", return_value=_cli_store(_model())):
            return CliRunner().invoke(cli, args)

    def test_perspectives_table_and_json(self) -> None:
        result = self._invoke(["perspectives"])
        assert result.exit_code == 0 and "Forecast" in result.output
        result = self._invoke(["perspectives", "--format", "json"])
        assert any(row["name"] == "返信待ち" for row in json.loads(result.output))

    def test_perspective_views(self) -> None:
        result = self._invoke(["perspective", "返信待ち"])
        assert result.exit_code == 0 and "Call Bob" in result.output
        result = self._invoke(["perspective", "inbox", "--format", "json", "--available"])
        payload = json.loads(result.output)
        assert payload["perspective"] == "Inbox" and payload["total"] == 2

    def test_unknown_perspective(self) -> None:
        result = self._invoke(["perspective", "nope"])
        assert result.exit_code != 0
        assert "Available perspectives" in result.output


class TestFormatting:
    def test_render_tables(self) -> None:
        console = Console(record=True, width=200)
        render_perspectives_table(
            [{"name": "Inbox", "kind": "builtin", "shows": "inbox items"}], console
        )
        payload = {
            "perspective": "Mixed",
            "shows": "rules",
            "items": [
                {
                    "id": "1",
                    "kind": "task",
                    "name": "Flag",
                    "flagged": True,
                    "available": True,
                    "due": "2026-01-01T00:00",
                    "overdue": True,
                    "tags": ["t"],
                },
                {
                    "id": "2",
                    "kind": "task",
                    "name": "Later",
                    "available": False,
                    "planned": "2026-02-01T09:00",
                    "project": "P",
                },
                {"id": "3", "kind": "project", "name": "Proj", "available": True},
            ],
            "warnings": ["careful"],
            "truncated": "Only the first 3 items are shown.",
        }
        render_perspective_table(payload, console)
        text = console.export_text()
        for needle in ("Inbox", "⚑ Flag", "Later", "Proj", "careful", "Only the first 3"):
            assert needle in text


# ---------------------------------------------------------------------------
# Cache shape
# ---------------------------------------------------------------------------


class TestCacheShape:
    @pytest.mark.asyncio
    async def test_payload_without_perspectives_is_rebuilt(self, tmp_path: Path) -> None:
        store, client = _make_store(tmp_path)
        await store.load()
        cached = pickle.loads((tmp_path / "of_model.pkl").read_bytes())  # noqa: S301
        old_model = cached.model
        del old_model.__dict__["perspectives"]
        stale = _CachePayload(model=old_model, bundle_fingerprint=cached.bundle_fingerprint)
        (tmp_path / "of_model.pkl").write_bytes(pickle.dumps(stale))
        model = await store.load()
        assert "perspectives" in vars(model)
        # Rebuilt from the immutable bundle-file cache: no second download.
        assert client.get_file.call_count == 1

    @pytest.mark.asyncio
    async def test_bare_legacy_model_without_perspectives(self, tmp_path: Path) -> None:
        store, client = _make_store(tmp_path)
        legacy = OFModel()
        del legacy.__dict__["perspectives"]
        (tmp_path / "of_model.pkl").write_bytes(pickle.dumps(legacy))
        await store.load()
        assert client.get_file.call_count == 1


# ---------------------------------------------------------------------------
# Bundle-file cache and HTTP timeout
# ---------------------------------------------------------------------------


class TestBundleFileCache:
    @pytest.mark.asyncio
    async def test_only_new_files_are_downloaded(self, tmp_path: Path) -> None:
        store, client = _make_store(tmp_path)
        files = await store._get_bundle_files(["00000000000000=a+b.zip", "2026=b+c.zip"])
        assert len(files) == 2 and client.get_file.call_count == 2
        cached = tmp_path / "bundle-files" / "2026=b+c.zip"
        assert cached.is_file() and (cached.stat().st_mode & 0o777) == 0o600
        await store._get_bundle_files(["00000000000000=a+b.zip", "2026=b+c.zip", "2027=c+d.zip"])
        assert client.get_file.call_count == 3

    @pytest.mark.asyncio
    async def test_unsafe_names_are_not_cached(self, tmp_path: Path) -> None:
        store, client = _make_store(tmp_path)
        await store._get_bundle_files(["../escape.zip", ".hidden.zip"])
        await store._get_bundle_files(["../escape.zip"])
        assert client.get_file.call_count == 3
        assert not (tmp_path / "bundle-files").exists()

    def test_prune(self, tmp_path: Path) -> None:
        store, _ = _make_store(tmp_path)
        store._prune_bundle_file_cache({"keep.zip"})  # no directory yet
        files = tmp_path / "bundle-files"
        files.mkdir()
        (files / "keep.zip").write_bytes(b"k")
        (files / "old.zip").write_bytes(b"o")
        store._prune_bundle_file_cache({"keep.zip"})
        assert sorted(p.name for p in files.iterdir()) == ["keep.zip"]


class TestRequestTimeoutEnv:
    def test_values(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from omnifocus.errors import OFError
        from omnifocus.http_api import _DEFAULT_REQUEST_TIMEOUT_SECONDS, _request_timeout_from_env

        monkeypatch.delenv("OF_HTTP_REQUEST_TIMEOUT_SECONDS", raising=False)
        assert _request_timeout_from_env() == _DEFAULT_REQUEST_TIMEOUT_SECONDS
        monkeypatch.setenv("OF_HTTP_REQUEST_TIMEOUT_SECONDS", "120")
        assert _request_timeout_from_env() == 120.0
        for bad in ("abc", "0.5"):
            monkeypatch.setenv("OF_HTTP_REQUEST_TIMEOUT_SECONDS", bad)
            with pytest.raises(OFError, match="OF_HTTP_REQUEST_TIMEOUT_SECONDS"):
                _request_timeout_from_env()
