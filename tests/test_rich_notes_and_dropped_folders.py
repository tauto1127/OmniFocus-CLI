"""Rich-text notes survive read/write, and dropped folders hide their projects."""

from __future__ import annotations

import dataclasses
import io
import pickle
import xml.etree.ElementTree as ET
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from omnifocus.api_common import folder_summary, project_summary
from omnifocus.models import PARSER_VERSION, Folder, OFModel, Project, RichNote, Task
from omnifocus.parser import _serialize_local, build_model
from omnifocus.writer import TaskWriter
from tests.conftest import make_zip
from tests.test_api_service import _service
from tests.test_store import _make_store

NS = "{http://www.omnigroup.com/namespace/OmniFocus/v2}"
NOW = datetime(2026, 11, 27, 1, 0, 0, tzinfo=UTC)
DROPPED = datetime(2026, 3, 2, 0, 16, 49, tzinfo=UTC)

RICH_NOTE = (
    "<note><text>"
    "<p><run><lit>完了条件：</lit></run><run><style/><lit>500GB未満</lit></run></p>"
    "<p/>"
    '<p><run><lit>see <a href="x">link</a> &amp; more</lit></run></p>'
    "</text></note>"
)

BUNDLE = f"""<?xml version="1.0" encoding="UTF-8"?>
<omnifocus xmlns="http://www.omnigroup.com/namespace/OmniFocus/v2">
  <folder id="f1"><folder/><name>Research</name><rank>1</rank><hidden/></folder>
  <folder id="f2"><folder idref="f1"/><name>卒論</name><rank>2</rank>
    <hidden>2026-03-02T00:16:49.000Z</hidden></folder>
  <task id="p1"><project><folder idref="f2"/><status>active</status></project>
    <name>評価</name>{RICH_NOTE}<rank>1</rank></task>
  <task id="t1"><task idref="p1"/><name>plain</name><note>  just text  </note></task>
  <task id="t2"><task idref="p1"/><name>empty</name><note/></task>
  <task id="t3"><task idref="p1"/><name>none</name></task>
  <context id="c1"><name>@home</name>{RICH_NOTE}</context>
</omnifocus>
"""


def _folder(fid: str, parent: str | None = None, hidden: datetime | None = None) -> Folder:
    return Folder(
        id=fid,
        name=fid,
        parent_folder_id=parent,
        rank=0,
        added=NOW,
        modified=NOW,
        hidden=hidden,
    )


def _project(pid: str, folder_id: str | None, status: str = "active") -> Project:
    return Project(
        id=pid,
        name=pid,
        folder_id=folder_id,
        status=status,
        singleton=False,
        rank=0,
        added=NOW,
        modified=NOW,
        flagged=False,
        due=None,
        start=None,
        note="",
        completed=None,
    )


def _dropped_model() -> OFModel:
    model = OFModel()
    model.folders["live"] = _folder("live")
    model.folders["gone"] = _folder("gone", parent="live", hidden=DROPPED)
    model.folders["deep"] = _folder("deep", parent="gone")
    model.projects["ok"] = _project("ok", "live")
    model.projects["held"] = _project("held", "deep", status="inactive")
    model.projects["buried"] = _project("buried", "deep")
    model.projects["finished"] = _project("finished", "deep", status="done")
    model.projects["loose"] = _project("loose", None)
    model.projects["done_loose"] = _project("done_loose", None, status="done")
    return model


class TestRichNoteParsing:
    def test_rich_project_note_reads_as_plain_text(self) -> None:
        model = build_model(make_zip(BUNDLE))
        note = model.projects["p1"].note
        assert note == "完了条件：500GB未満\n\nsee link & more"
        assert isinstance(note, RichNote)
        assert note.xml.startswith("<note><text><p><run><lit>完了条件")
        assert "&amp; more" in note.xml
        assert model.parser_version == PARSER_VERSION

    def test_rich_tag_note_and_plain_notes(self) -> None:
        model = build_model(make_zip(BUNDLE))
        assert model.tags["c1"].note.startswith("完了条件")
        assert model.tasks["t1"].note == "just text"
        assert not isinstance(model.tasks["t1"].note, RichNote)
        assert model.tasks["t2"].note == ""
        assert model.tasks["t3"].note == ""

    def test_serialize_local_escapes_attributes_text_and_tail(self) -> None:
        el = ET.fromstring(  # noqa: S314
            '<n xmlns="urn:x"><a k="&quot;&lt;">t&amp;</a>tail&gt;<b/></n>'
        )
        assert _serialize_local(el) == '<n><a k="&quot;&lt;">t&amp;</a>tail&gt;<b/></n>'

    def test_rich_note_pickles_and_string_ops_drop_xml(self) -> None:
        note = RichNote("hello", "<note><text/></note>")
        restored = pickle.loads(pickle.dumps(note))  # noqa: S301
        assert isinstance(restored, RichNote)
        assert restored == "hello" and restored.xml == "<note><text/></note>"
        assert type(note + "!") is str


def _unzip_contents(data: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return zf.read("contents.xml").decode("utf-8")


class TestRichNoteWriting:
    def _task(self, note: str) -> Task:
        return Task(
            id="t1",
            name="X",
            parent_task_id="p1",
            project_id="p1",
            inbox=False,
            completed=None,
            flagged=False,
            due=None,
            start=None,
            hidden=None,
            note=note,
            rank=1,
            repetition_rule=None,
            estimated_minutes=None,
            added=NOW,
            modified=NOW,
        )

    def test_complete_task_resends_rich_note_unchanged(self) -> None:
        note = build_model(make_zip(BUNDLE)).projects["p1"].note
        writer = TaskWriter(head_id="tail01", parent_tail_id="tail00")
        _fname, data = writer.complete_task(self._task(note))
        root = ET.fromstring(_unzip_contents(data))  # noqa: S314
        note_el = root.find(f"{NS}task/{NS}note")
        assert note_el is not None
        lits = ["".join(lit.itertext()) for lit in note_el.iter(f"{NS}lit")]
        assert lits == ["完了条件：", "500GB未満", "see link & more"]

    def test_edited_note_is_written_as_plain_text(self) -> None:
        writer = TaskWriter(head_id="tail01", parent_tail_id="tail00")
        _fname, data = writer.complete_task(self._task("new text"))
        root = ET.fromstring(_unzip_contents(data))  # noqa: S314
        note_el = root.find(f"{NS}task/{NS}note")
        assert note_el is not None and note_el.text == "new text"


class TestEffectiveProjectStatus:
    def test_folder_dropped_at_walks_ancestors(self) -> None:
        model = _dropped_model()
        assert model.folder_dropped_at("deep") == DROPPED
        assert model.folder_dropped_at("live") is None
        assert model.folder_dropped_at("missing") is None
        assert model.folder_dropped_at(None) is None

    def test_folder_cycle_terminates(self) -> None:
        model = OFModel()
        model.folders["a"] = _folder("a", parent="b")
        model.folders["b"] = _folder("b", parent="a")
        assert model.folder_dropped_at("a") is None

    def test_effective_status(self) -> None:
        model = _dropped_model()
        assert model.effective_project_status(model.projects["buried"]) == "dropped"
        assert model.effective_project_status(model.projects["held"]) == "dropped"
        assert model.effective_project_status(model.projects["finished"]) == "done"
        assert model.effective_project_status(model.projects["ok"]) == "active"
        assert [p.id for p in model.active_projects] == ["ok", "loose"]

    def test_summaries_report_effective_status(self) -> None:
        model = _dropped_model()
        buried = project_summary(model.projects["buried"], model)
        assert buried["status"] == "dropped"
        assert buried["own_status"] == "active"
        assert buried["dropped_by_folder"] is True
        assert "own_status" not in project_summary(model.projects["ok"], model)
        assert folder_summary(model.folders["deep"], model)["dropped"] is True
        assert folder_summary(model.folders["live"], model)["dropped"] is False


class TestApiDroppedFolders:
    @pytest.mark.asyncio
    async def test_list_projects_uses_effective_status(self) -> None:
        service = _service(model=_dropped_model())
        active = await service.list_projects(status="active")
        assert [p["id"] for p in active] == ["ok", "loose"]
        dropped = await service.list_projects(status="dropped")
        assert {p["id"] for p in dropped} == {"held", "buried"}

    @pytest.mark.asyncio
    async def test_review_skips_projects_in_dropped_folders(self) -> None:
        service = _service(model=_dropped_model())
        queue = await service.list_projects_for_review(due_only=False)
        assert {p["id"] for p in queue} == {"ok", "loose"}

    @pytest.mark.asyncio
    async def test_list_tasks_project_status_uses_effective_status(self) -> None:
        model = _dropped_model()
        task = TestRichNoteWriting()._task("")
        model.tasks["t1"] = dataclasses.replace(task, project_id="buried")
        service = _service(model=model)
        assert await service.list_tasks(project_status="active") == []
        assert [t["id"] for t in await service.list_tasks(project_status="all")] == ["t1"]

    @pytest.mark.asyncio
    async def test_list_folders_hides_dropped_by_default(self) -> None:
        service = _service(model=_dropped_model())
        listed = await service.list_folders()
        assert [f["id"] for f in listed] == ["live"]
        assert listed[0]["child_folder_ids"] == []
        everything = await service.list_folders(include_dropped=True)
        assert {f["id"] for f in everything} == {"live", "gone", "deep"}
        live = next(f for f in everything if f["id"] == "live")
        assert live["child_folder_ids"] == ["gone"]

    @pytest.mark.asyncio
    async def test_folder_tree_matches_remaining_view(self) -> None:
        service = _service(model=_dropped_model())
        tree = await service.get_folder_tree()
        assert [node["folder"]["id"] for node in tree["folders"]] == ["live"]
        assert tree["folders"][0]["children"] == []
        assert [p["id"] for p in tree["folders"][0]["projects"]] == ["ok"]
        assert [p["id"] for p in tree["no_folder_projects"]] == ["loose"]
        assert tree["dangling_folder_projects"] == []

    @pytest.mark.asyncio
    async def test_folder_tree_include_all_marks_dropped_projects(self) -> None:
        service = _service(model=_dropped_model())
        tree = await service.get_folder_tree(include_all=True)
        deep = tree["folders"][0]["children"][0]["children"][0]
        by_id = {p["id"]: p for p in deep["projects"]}
        assert by_id["buried"]["status"] == "dropped"
        assert by_id["buried"]["own_status"] == "active"
        assert by_id["finished"]["status"] == "done"
        assert "own_status" not in by_id["finished"]
        assert {p["id"] for p in tree["no_folder_projects"]} == {"loose", "done_loose"}


class TestCacheParserVersion:
    @pytest.mark.asyncio
    async def test_current_bare_model_cache_is_reused(self, tmp_path: Path) -> None:
        store, client = _make_store(tmp_path)
        cached = OFModel(parser_version=PARSER_VERSION)
        (tmp_path / "of_model.pkl").write_bytes(pickle.dumps(cached))
        assert store._load_from_cache() is not None
        assert client.get_file.call_count == 0

    @pytest.mark.asyncio
    async def test_old_parser_version_cache_is_rebuilt(self, tmp_path: Path) -> None:
        store, client = _make_store(tmp_path)
        (tmp_path / "of_model.pkl").write_bytes(pickle.dumps(OFModel(parser_version=1)))
        assert store._load_from_cache() is None
        assert isinstance(client, AsyncMock)
