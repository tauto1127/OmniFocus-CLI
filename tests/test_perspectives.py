"""Tests for :mod:`omnifocus.perspectives` (headless perspective evaluation)."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from omnifocus import perspectives as P
from omnifocus.errors import OFPerspectiveNotFound
from omnifocus.models import Folder, OFModel, Perspective, Project, Tag, Task

EPOCH = datetime(2020, 1, 1, tzinfo=UTC)
NOW = datetime(2026, 11, 21, 12, 0)  # naive wall clock (OF_TIMEZONE=UTC in tests)


@pytest.fixture(autouse=True)
def _utc(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OF_TIMEZONE", "UTC")


def task(tid: str, name: str | None = None, **kw: Any) -> Task:
    base = Task(
        id=tid,
        name=name or tid,
        parent_task_id=None,
        project_id=None,
        inbox=False,
        completed=None,
        flagged=False,
        due=None,
        start=None,
        hidden=None,
        note="",
        rank=0,
        repetition_rule=None,
        estimated_minutes=None,
        added=EPOCH,
        modified=EPOCH,
    )
    return replace(base, **kw)


def project(pid: str, name: str | None = None, **kw: Any) -> Project:
    base = Project(
        id=pid,
        name=name or pid,
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
    return replace(base, **kw)


def model_of(*objs: Any, **extra: Any) -> OFModel:
    m = OFModel()
    for obj in objs:
        if isinstance(obj, Task):
            m.tasks[obj.id] = obj
        elif isinstance(obj, Project):
            m.projects[obj.id] = obj
        elif isinstance(obj, Tag):
            m.tags[obj.id] = obj
        elif isinstance(obj, Folder):
            m.folders[obj.id] = obj
        elif isinstance(obj, Perspective):
            m.perspectives[obj.id] = obj
    m.settings.update(extra.get("settings", {}))
    return m


def ctx_of(m: OFModel) -> P._Context:
    return P._Context.build(m, NOW)


def ids(items: list[Any]) -> list[str]:
    return [i.id for i in items]


def custom(rules: list[Any], aggregation: str = "all", **kw: Any) -> Perspective:
    return Perspective(id="cp", name="Custom", filter_rules=rules, aggregation=aggregation, **kw)


def run(m: OFModel, rules: list[Any], aggregation: str = "all", **kw: Any) -> list[str]:
    persp = custom(rules, aggregation, **kw)
    return ids(P.evaluate_custom(persp, ctx_of(m)).items)


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------


class TestTimezone:
    def test_of_timezone(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OF_TIMEZONE", "Asia/Tokyo")
        assert str(P.user_timezone()) == "Asia/Tokyo"

    def test_invalid_falls_back_to_tz(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OF_TIMEZONE", "Not/AZone")
        monkeypatch.setenv("TZ", "Europe/Warsaw")
        assert str(P.user_timezone()) == "Europe/Warsaw"

    def test_local_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("OF_TIMEZONE")
        monkeypatch.delenv("TZ", raising=False)
        assert P.user_timezone() is not None

    def test_local_fallback_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("OF_TIMEZONE")
        monkeypatch.delenv("TZ", raising=False)

        class _Fake:
            def astimezone(self) -> Any:
                return self

            tzinfo = None

        class _DT:
            @staticmethod
            def now(*_a: Any) -> Any:
                return _Fake()

        monkeypatch.setattr(P, "datetime", _DT)
        assert P.user_timezone() is UTC

    def test_to_local_naive(self) -> None:
        assert P._to_local_naive(None, UTC) is None
        naive = datetime(2026, 1, 1, 9)
        assert P._to_local_naive(naive, UTC) == naive
        aware = datetime(2026, 1, 1, 9, tzinfo=UTC)
        assert P._to_local_naive(aware, UTC) == naive

    def test_add_months_clamps(self) -> None:
        assert P._add_months(datetime(2026, 1, 31), 1) == datetime(2026, 2, 28)
        assert P._add_months(datetime(2026, 11, 15), 3) == datetime(2027, 2, 15)
        assert P._add_months(datetime(2026, 3, 31), -1) == datetime(2026, 2, 28)

    def test_shift(self) -> None:
        base = datetime(2026, 1, 15)
        assert P._shift(base, 1, "year") == datetime(2027, 1, 15)
        assert P._shift(base, -2, "month") == datetime(2025, 11, 15)
        assert P._shift(base, 1, "week") == datetime(2026, 1, 22)
        assert P._shift(base, 2, "day") == datetime(2026, 1, 17)
        assert P._shift(base, 3, "hour") == datetime(2026, 1, 15, 3)
        assert P._shift(base, 1, "fortnight") is None

    def test_context_default_now(self) -> None:
        ctx = P._Context.build(OFModel())
        assert ctx.now.tzinfo is None


# ---------------------------------------------------------------------------
# Availability
# ---------------------------------------------------------------------------


class TestAvailability:
    def test_remaining_completed_dropped(self) -> None:
        m = model_of(
            project("p"),
            task("a", project_id="p", parent_task_id="p"),
            task("b", project_id="p", parent_task_id="p", completed=EPOCH),
            task("c", project_id="p", parent_task_id="p", hidden=EPOCH),
        )
        assert run(m, [{"actionAvailability": "remaining"}]) == ["a"]
        assert run(m, [{"actionAvailability": "completed"}]) == ["b"]
        assert run(m, [{"actionAvailability": "dropped"}]) == ["c"]
        assert sorted(run(m, [{"actionAvailability": "all"}])) == ["a", "b", "c"]

    def test_inherited_completion_and_drop(self) -> None:
        m = model_of(
            project("done", status="done"),
            project("drop", status="dropped"),
            project("live"),
            task("g", project_id="live", parent_task_id="live", completed=EPOCH),
            task("g1", project_id="live", parent_task_id="g"),
            task("h", project_id="live", parent_task_id="live", hidden=EPOCH),
            task("h1", project_id="live", parent_task_id="h"),
            task("x", project_id="done", parent_task_id="done"),
            task("y", project_id="drop", parent_task_id="drop"),
        )
        ctx = ctx_of(m)
        assert ctx.is_completed(m.tasks["g1"]) and ctx.is_completed(m.tasks["x"])
        assert ctx.is_dropped(m.tasks["h1"]) and ctx.is_dropped(m.tasks["y"])
        assert run(m, [{"actionAvailability": "remaining"}]) == []

    def test_dropped_folder_drops_projects(self) -> None:
        m = model_of(
            Folder("f", "Old", None, 0, EPOCH, EPOCH, hidden=EPOCH),
            Folder("f2", "Child", "f", 0, EPOCH, EPOCH),
            project("p", folder_id="f2"),
            task("t", project_id="p", parent_task_id="p"),
        )
        ctx = ctx_of(m)
        assert ctx.is_dropped(m.projects["p"])
        assert ctx.dropped_at(m.tasks["t"]) == EPOCH
        assert not ctx.is_remaining(m.tasks["t"])

    def test_project_completion_flags(self) -> None:
        m = model_of(project("p", completed=EPOCH), project("q", status="dropped"))
        ctx = ctx_of(m)
        assert ctx.is_completed(m.projects["p"])
        assert ctx.dropped_at(m.projects["q"]) == EPOCH
        assert ctx.dropped_at(m.projects["p"]) is None

    def test_defer_and_on_hold(self) -> None:
        m = model_of(
            Tag("hold", "Hold", None, 0, on_hold=True),
            project("p"),
            project("onhold", status="inactive"),
            project("later", start=NOW + timedelta(days=1)),
            task("deferred", project_id="p", parent_task_id="p", start=NOW + timedelta(hours=1)),
            task("ready", project_id="p", parent_task_id="p", start=NOW - timedelta(hours=1)),
            task("tagged", project_id="p", parent_task_id="p", tag_ids=("hold",)),
            task("ph", project_id="onhold", parent_task_id="onhold"),
            task("pl", project_id="later", parent_task_id="later"),
            task("inbox", inbox=True),
        )
        assert sorted(run(m, [{"actionAvailability": "available"}])) == ["inbox", "ready"]

    def test_on_hold_tag_on_parent_group(self) -> None:
        m = model_of(
            Tag("hold", "Hold", None, 0, on_hold=True),
            project("p"),
            task("g", project_id="p", parent_task_id="p", tag_ids=("hold",)),
            task("child", project_id="p", parent_task_id="g"),
        )
        assert run(m, [{"actionAvailability": "available"}]) == []

    def test_sequential_project_and_group(self) -> None:
        m = model_of(
            project("seq", order="sequential"),
            task("s1", project_id="seq", parent_task_id="seq", rank=1),
            task("s2", project_id="seq", parent_task_id="seq", rank=2),
            project("par"),
            task("g", project_id="par", parent_task_id="par", order="sequential"),
            task("g1", project_id="par", parent_task_id="g", rank=1, completed=EPOCH),
            task("g2", project_id="par", parent_task_id="g", rank=2),
            task("g3", project_id="par", parent_task_id="g", rank=3),
            project("single", singleton=True, order="sequential"),
            task("l1", project_id="single", parent_task_id="single", rank=1),
            task("l2", project_id="single", parent_task_id="single", rank=2),
        )
        available = run(m, [{"actionAvailability": "available"}])
        assert "s1" in available and "s2" not in available
        assert "g2" in available and "g3" not in available
        assert "g" in available  # group with an available child
        assert "l1" in available and "l2" in available

    def test_group_without_available_children(self) -> None:
        m = model_of(
            project("p"),
            task("g", project_id="p", parent_task_id="p"),
            task("c", project_id="p", parent_task_id="g", start=NOW + timedelta(days=2)),
        )
        ctx = ctx_of(m)
        assert not ctx.is_available(m.tasks["g"])
        assert ctx.is_available(m.tasks["g"]) is False  # memoised path

    def test_first_available(self) -> None:
        m = model_of(
            project("p"),
            task("a", project_id="p", parent_task_id="p", rank=1, start=NOW + timedelta(days=1)),
            task("b", project_id="p", parent_task_id="p", rank=2),
            task("c", project_id="p", parent_task_id="p", rank=3),
            task("g", project_id="p", parent_task_id="p", rank=4),
            task("g1", project_id="p", parent_task_id="g"),
            task("i", inbox=True),
            project("q"),
        )
        assert run(m, [{"actionAvailability": "firstAvailable"}]) == ["i", "b"]
        ctx = ctx_of(m)
        assert not ctx.is_first_available(m.projects["q"])
        assert not ctx.is_first_available(m.tasks["g"])

    def test_outline_skips_cycles(self) -> None:
        ctx = ctx_of(model_of())
        a = task("a")
        ctx.children["a"] = [a]
        assert ids(ctx.outline([a])) == ["a"]

    def test_ancestor_cycle_guard(self) -> None:
        m = model_of(task("a", parent_task_id="b"), task("b", parent_task_id="a"))
        ctx = ctx_of(m)
        assert ids(ctx.ancestors(m.tasks["a"])) == ["b"]

    def test_folder_chain_cycle(self) -> None:
        m = model_of(
            Folder("f1", "A", "f2", 0, EPOCH, EPOCH), Folder("f2", "B", "f1", 0, EPOCH, EPOCH)
        )
        assert ctx_of(m).folder_chain("f1") == ["f1", "f2"]

    def test_folder_chain_missing_folder(self) -> None:
        assert ctx_of(model_of()).folder_chain("ghost") == ["ghost"]


# ---------------------------------------------------------------------------
# Effective attributes
# ---------------------------------------------------------------------------


class TestEffective:
    def test_due_defer_planned_flag_inherit(self) -> None:
        m = model_of(
            project("p", due=datetime(2026, 12, 1), start=datetime(2026, 11, 1), flagged=True),
            task("g", project_id="p", parent_task_id="p", due=datetime(2026, 11, 30)),
            task("c", project_id="p", parent_task_id="g", planned=datetime(2026, 11, 25)),
            task("c2", project_id="p", parent_task_id="c"),
            project("plain"),
            task("lone", project_id="plain", parent_task_id="plain"),
        )
        ctx = ctx_of(m)
        c2 = m.tasks["c2"]
        assert ctx.effective_due(c2) == datetime(2026, 11, 30)
        assert ctx.effective_defer(c2) == datetime(2026, 11, 1)
        assert ctx.effective_planned(c2) == datetime(2026, 11, 25)
        assert ctx.effective_flagged(c2)
        assert ctx.effective_due(m.projects["p"]) == datetime(2026, 12, 1)
        assert ctx.effective_defer(m.projects["p"]) == datetime(2026, 11, 1)
        assert ctx.effective_planned(m.projects["p"]) is None
        assert not ctx.effective_flagged(m.projects["plain"])
        assert not ctx.effective_flagged(m.tasks["lone"])
        assert ctx.effective_due(m.tasks["lone"]) is None

    def test_flag_from_parent_task(self) -> None:
        m = model_of(task("g", flagged=True, inbox=True), task("c", parent_task_id="g"))
        assert ctx_of(m).effective_flagged(m.tasks["c"])

    def test_date_fields(self) -> None:
        m = model_of(
            task(
                "t",
                completed=datetime(2026, 11, 20, tzinfo=UTC),
                hidden=None,
                added=datetime(2026, 1, 1, tzinfo=UTC),
                modified=datetime(2026, 2, 1, tzinfo=UTC),
            )
        )
        ctx = ctx_of(m)
        t = m.tasks["t"]
        assert ctx.date_field(t, "completed") == datetime(2026, 11, 20)
        assert ctx.date_field(t, "dropped") is None
        assert ctx.date_field(t, "added") == datetime(2026, 1, 1)
        assert ctx.date_field(t, "changed") == datetime(2026, 2, 1)
        assert ctx.date_field(t, "bogus") is None
        assert ctx.warnings and "bogus" in ctx.warnings[0]
        ctx.warn(ctx.warnings[0])
        assert len(ctx.warnings) == 1


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------


@pytest.fixture
def rich_model() -> OFModel:
    return model_of(
        Tag("home", "Home", None, 0),
        Tag("kitchen", "Kitchen", "home", 0),
        Tag("work", "Work", None, 0),
        Tag("hold", "Hold", None, 0, on_hold=True),
        Tag("gone", "Gone", None, 0, hidden=EPOCH),
        Folder("f", "Area", None, 0, EPOCH, EPOCH),
        project("p", "Proj", folder_id="f", tag_ids=("work",)),
        project("sal", "Single", singleton=True),
        project("hold-p", "Paused", status="inactive"),
        project("done-p", "Done", status="done"),
        project("drop-p", "Dropped", status="dropped"),
        project("pend", "Pending", start=NOW + timedelta(days=3)),
        project("stall", "Stalled"),
        task("cook", "Cook dinner", project_id="p", parent_task_id="p", tag_ids=("kitchen",)),
        task(
            "both",
            "Both tags",
            project_id="p",
            parent_task_id="p",
            tag_ids=("home", "work"),
            due=NOW + timedelta(hours=10),
            estimated_minutes=10,
            repetition_rule="FREQ=DAILY",
            note="buy milk later",
        ),
        task("held", "Held", project_id="p", parent_task_id="p", tag_ids=("hold",)),
        task("ghost", "Ghost", project_id="p", parent_task_id="p", tag_ids=("gone",)),
        task("salt", "Salt", project_id="sal", parent_task_id="sal", start=NOW - timedelta(days=1)),
        task("paused", "Paused task", project_id="hold-p", parent_task_id="hold-p"),
        task(
            "stl",
            "Stalled task",
            project_id="stall",
            parent_task_id="stall",
            start=NOW + timedelta(days=9),
        ),
        task("in", "Inbox thing", inbox=True, flagged=True, estimated_minutes=60),
    )


class TestRules:
    def test_tags_any_all_untagged(self, rich_model: OFModel) -> None:
        assert sorted(run(rich_model, [{"actionHasAnyOfTags": ["home"]}])) == ["both", "cook"]
        assert run(rich_model, [{"actionHasAllOfTags": ["home", "work"]}]) == ["both"]
        assert run(rich_model, [{"actionHasAllOfTags": "bad"}]) != []
        untagged = run(rich_model, [{"actionIsUntagged": True}])
        assert "in" in untagged and "cook" not in untagged
        assert run(rich_model, [{"actionHasAnyOfTags": "bad"}]) == []

    def test_tag_status(self, rich_model: OFModel) -> None:
        assert run(rich_model, [{"actionHasTagWithStatus": "onHold"}]) == ["held"]
        assert run(rich_model, [{"actionHasTagWithStatus": "dropped"}]) == ["ghost"]
        assert "cook" in run(rich_model, [{"actionHasTagWithStatus": "active"}])
        assert "held" in run(rich_model, [{"actionHasTagWithStatus": "remaining"}])
        persp = custom([{"actionHasTagWithStatus": "weird"}])
        result = P.evaluate_custom(persp, ctx_of(rich_model))
        assert any("weird" in w for w in result.warnings)

    def test_focus_and_project_kinds(self, rich_model: OFModel) -> None:
        assert sorted(run(rich_model, [{"actionWithinFocus": ["f"]}])) == [
            "both",
            "cook",
            "ghost",
            "held",
        ]
        assert run(rich_model, [{"actionWithinFocus": ["sal"]}]) == ["salt"]
        assert run(rich_model, [{"actionWithinFocus": ["cook"]}]) == ["cook"]
        assert run(rich_model, [{"actionWithinFocus": "bad"}]) == []
        assert run(rich_model, [{"actionHasNoProject": True}]) == ["in"]
        assert run(rich_model, [{"actionIsInSingleActionsList": True}]) == ["salt"]
        projects_only = run(rich_model, [{"actionIsProject": True}])
        assert "p" in projects_only and "cook" not in projects_only
        assert "p" in run(
            rich_model,
            [{"aggregateRules": [{"actionIsProjectOrGroup": True}], "aggregateType": "all"}],
        )
        assert run(rich_model, [{"actionIsGroup": True}]) == []
        assert "cook" in run(rich_model, [{"actionIsLeaf": True}])

    def test_focus_inside_subtask(self) -> None:
        m = model_of(task("g", inbox=True), task("c", parent_task_id="g"))
        assert run(m, [{"actionWithinFocus": ["g"]}]) == ["g", "c"]

    def test_project_status_rule(self, rich_model: OFModel) -> None:
        def status(value: str) -> list[str]:
            return run(rich_model, [{"actionHasProjectWithStatus": value}])

        assert "cook" in status("active")
        assert status("onHold") == ["paused"]
        assert status("completed") == [] and status("dropped") == []
        assert "paused" in status("remaining")
        assert status("pending") == []
        assert status("stalled") == ["stl"]
        persp = custom([{"actionHasProjectWithStatus": "odd"}])
        assert P.evaluate_custom(persp, ctx_of(rich_model)).warnings

    def test_project_status_extra_branches(self) -> None:
        m = model_of(
            project("pend", start=NOW + timedelta(days=1)),
            task("t", project_id="pend", parent_task_id="pend"),
            project("hold", status="inactive"),
            task("u", project_id="hold", parent_task_id="hold"),
        )
        assert run(m, [{"actionHasProjectWithStatus": "pending"}]) == ["t"]
        assert run(m, [{"actionHasProjectWithStatus": "stalled"}]) == ["t"]

    def test_status_dates_duration_repeat_search(self, rich_model: OFModel) -> None:
        assert run(rich_model, [{"actionStatus": "flagged"}]) == ["in"]
        assert run(rich_model, [{"actionStatus": "due"}]) == ["both"]
        assert run(rich_model, [{"actionHasDueDate": True}]) == ["both"]
        assert "salt" in run(rich_model, [{"actionHasDeferDate": True}])
        assert sorted(run(rich_model, [{"actionHasDuration": True}])) == ["both", "in"]
        assert run(rich_model, [{"actionWithinDuration": 15}]) == ["both"]
        assert run(rich_model, [{"actionWithinDuration": "x"}]) == []
        assert run(rich_model, [{"actionRepeats": True}]) == ["both"]
        assert run(rich_model, [{"actionMatchingSearch": ["MILK"]}]) == ["both"]
        assert run(rich_model, [{"actionMatchingSearch": "cook"}]) == ["cook"]
        persp = custom([{"actionStatus": "odd"}])
        assert P.evaluate_custom(persp, ctx_of(rich_model)).warnings

    def test_aggregations(self, rich_model: OFModel) -> None:
        rules = [{"actionHasNoProject": True}, {"actionRepeats": True}]
        assert sorted(run(rich_model, rules, "any")) == ["both", "in"]
        assert "both" not in run(rich_model, rules, "none")
        nested = [
            {"actionAvailability": "remaining"},
            {"aggregateRules": [{"actionHasNoProject": True}], "aggregateType": "none"},
        ]
        assert "in" not in run(rich_model, nested)
        assert P._aggregate([None, None], "any") is True
        assert P._aggregate([], "none") is True

    def test_disabled_unknown_malformed(self, rich_model: OFModel) -> None:
        everything = run(rich_model, [{"disabledRule": {"actionHasNoProject": True}}])
        assert "cook" in everything
        persp = custom(
            [
                {"somethingNew": 1},
                "not-a-dict",
                {},
                {"aggregateRules": "bad"},
                {"actionAvailability": "sometimes"},
            ]
        )
        result = P.evaluate_custom(persp, ctx_of(rich_model))
        assert len(result.warnings) == 4
        assert "cook" in ids(result.items)

    def test_rules_error_reported(self, rich_model: OFModel) -> None:
        persp = custom([], rules_error="filterRules is not valid JSON")
        result = P.evaluate_custom(persp, ctx_of(rich_model))
        assert result.warnings == ["filterRules is not valid JSON"]


class TestDateRules:
    @pytest.fixture
    def dated(self) -> OFModel:
        return model_of(
            task("today", due=datetime(2026, 11, 21, 18), inbox=True),
            task("yday", due=datetime(2026, 11, 20, 9), inbox=True),
            task("tmrw", due=datetime(2026, 11, 22, 9), inbox=True),
            task("week", due=datetime(2026, 11, 26, 9), inbox=True),
            task("none", inbox=True),
        )

    def rule(self, **kw: Any) -> list[Any]:
        return [{"actionDateField": "due", **kw}]

    def test_relative_days(self, dated: OFModel) -> None:
        assert run(dated, self.rule(actionDateIsToday=True)) == ["today"]
        assert run(dated, self.rule(actionDateIsYesterday=True)) == ["yday"]
        assert run(dated, self.rule(actionDateIsTomorrow=True)) == ["tmrw"]

    def test_in_the_next_and_past(self, dated: OFModel) -> None:
        nxt = {"relativeAfterAmount": 7, "relativeComponent": "day"}
        assert sorted(run(dated, self.rule(actionDateIsInTheNext=nxt))) == ["tmrw", "today", "week"]
        past = {"relativeBeforeAmount": 2, "relativeComponent": "day"}
        assert run(dated, self.rule(actionDateIsInThePast=past)) == ["yday"]

    def test_bad_relative_specs(self, dated: OFModel) -> None:
        persp = custom(self.rule(actionDateIsInTheNext={"relativeAfterAmount": "x"}))
        result = P.evaluate_custom(persp, ctx_of(dated))
        assert result.warnings and len(result.items) == 5
        persp = custom(self.rule(actionDateIsInTheNext="oops"))
        assert P.evaluate_custom(persp, ctx_of(dated)).warnings
        bad_unit = {"relativeAfterAmount": 1, "relativeComponent": "decade"}
        persp = custom(self.rule(actionDateIsInTheNext=bad_unit))
        assert P.evaluate_custom(persp, ctx_of(dated)).warnings

    def test_date_specs(self, dated: OFModel) -> None:
        on = self.rule(actionDateIsOnDateSpec={"dynamic": "2026-11-22"})
        assert run(dated, on) == ["tmrw"]
        after = self.rule(actionDateIsAfterDateSpec={"dynamic": "tomorrow"})
        assert run(dated, after) == ["tmrw", "week"]
        before = self.rule(actionDateIsBeforeDateSpec={"date": "2026-11-21T00:00:00+00:00"})
        assert run(dated, before) == ["yday"]
        between = self.rule(
            actionDateIsAfterDateSpec={"dynamic": "today"},
            actionDateIsBeforeDateSpec={"dynamic": "nonsense"},
        )
        result = P.evaluate_custom(custom(between), ctx_of(dated))
        assert sorted(ids(result.items)) == ["tmrw", "today", "week"] and result.warnings

    def test_unparseable_specs(self, dated: OFModel) -> None:
        for spec in ("text", {"dynamic": 5}, {"dynamic": "next fortnight"}):
            result = P.evaluate_custom(
                custom(self.rule(actionDateIsOnDateSpec=spec)), ctx_of(dated)
            )
            assert result.warnings and len(result.items) == 5

    def test_unknown_date_rule(self, dated: OFModel) -> None:
        result = P.evaluate_custom(custom(self.rule(actionDateIsSoon=True)), ctx_of(dated))
        assert result.warnings and len(result.items) == 5

    def test_missing_dates_do_not_match(self, dated: OFModel) -> None:
        past = {"relativeBeforeAmount": 1, "relativeComponent": "year"}
        assert "none" not in run(dated, self.rule(actionDateIsInThePast=past))
        assert "none" not in run(dated, self.rule(actionDateIsToday=True))


# ---------------------------------------------------------------------------
# Descriptions
# ---------------------------------------------------------------------------


class TestDescribe:
    def test_rules(self, rich_model: OFModel) -> None:
        ctx = ctx_of(rich_model)
        assert P.describe_rule("x", ctx) == "malformed rule"
        assert P.describe_rule({"disabledRule": {"actionHasNoProject": True}}, ctx).startswith(
            "(disabled)"
        )
        assert '"Home"' in P.describe_rule({"actionHasAnyOfTags": ["home"]}, ctx)
        assert "<nope>" in P.describe_rule({"actionHasAllOfTags": ["nope"]}, ctx)
        assert '"Proj"' in P.describe_rule({"actionWithinFocus": ["p"]}, ctx)
        assert '"Area"' in P.describe_rule({"actionWithinFocus": ["f"]}, ctx)
        assert '"Cook dinner"' in P.describe_rule({"actionWithinFocus": ["cook"]}, ctx)
        assert "(none)" in P.describe_rule({"actionWithinFocus": "bad"}, ctx)
        assert P.describe_rule({"actionHasNoProject": True}, ctx).startswith("in the Inbox")
        assert P.describe_rule({"actionStatus": "due"}, ctx) == "status is due"
        assert P.describe_rule({"actionRepeats": True}, ctx) == "actionRepeats = True"
        assert "due date" in P.describe_rule(
            {"actionDateField": "due", "actionDateIsToday": True}, ctx
        )
        assert P.describe_rule({"actionDateField": "due"}, ctx) == "due date rule"
        agg = {"aggregateRules": [{"actionIsUntagged": True}], "aggregateType": "any"}
        assert P.describe_rule(agg, ctx).startswith("(")
        assert P.describe_rule({"aggregateRules": "bad"}, ctx) == "()"

    def test_perspective(self, rich_model: OFModel) -> None:
        ctx = ctx_of(rich_model)
        rules = [{"actionAvailability": "remaining"}, {"actionHasNoProject": True}]
        assert " AND " in P.describe_perspective(custom(rules), ctx)
        assert " OR " in P.describe_perspective(custom(rules, "any"), ctx)
        assert P.describe_perspective(custom(rules, "none"), ctx).startswith("NONE OF")
        assert "everything" in P.describe_perspective(custom([]), ctx)
        assert "could not" in P.describe_perspective(custom([], rules_error="bad"), ctx)


# ---------------------------------------------------------------------------
# Built-ins, sorting, lookup and payloads
# ---------------------------------------------------------------------------


@pytest.fixture
def agenda() -> OFModel:
    return model_of(
        Tag("t", "Tag", None, 0),
        project("p", "Proj", rank=1),
        project("fl", "Flag proj", flagged=True, rank=2),
        project("rv", "Review me", last_review=EPOCH, review_interval="@1w"),
        project("rv-drop", "Dropped review", status="dropped", review_interval="@1w"),
        task("in1", "Inbox one", inbox=True, rank=1),
        task("in1c", "Inbox child", parent_task_id="in1"),
        task("in2", "Inbox done", inbox=True, completed=EPOCH),
        task("over", "Overdue", project_id="p", parent_task_id="p", due=NOW - timedelta(days=3)),
        task("tod", "Today", project_id="p", parent_task_id="p", due=NOW + timedelta(hours=2)),
        task("fut", "Future", project_id="p", parent_task_id="p", due=NOW + timedelta(days=5)),
        task("pl", "Planned", project_id="p", parent_task_id="p", planned=NOW - timedelta(days=1)),
        task(
            "df", "Defer today", project_id="p", parent_task_id="p", start=NOW + timedelta(hours=1)
        ),
        task(
            "ft", "Flagged task", project_id="p", parent_task_id="p", flagged=True, tag_ids=("t",)
        ),
        task(
            "fd",
            "Flag deferred",
            project_id="p",
            parent_task_id="p",
            flagged=True,
            start=NOW + timedelta(days=2),
        ),
        task("inflag", "In flagged proj", project_id="fl", parent_task_id="fl"),
        task("gone", "Done today", project_id="p", parent_task_id="p", due=NOW, completed=EPOCH),
    )


def builtin(key: str) -> P.BuiltinPerspective:
    return next(b for b in P.BUILTINS if b.key == key)


class TestBuiltins:
    def test_inbox(self, agenda: OFModel) -> None:
        result = P.evaluate_builtin(builtin("inbox"), ctx_of(agenda))
        assert ids(result.items) == ["in1", "in1c"]

    def test_forecast(self, agenda: OFModel) -> None:
        result = P.evaluate_builtin(builtin("forecast"), ctx_of(agenda))
        assert ids(result.items) == ["over", "tod", "pl"]
        assert result.sort == "due date"
        agenda.settings["_ForecastIncludeDeferredItems"] = True
        result = P.evaluate_builtin(builtin("forecast"), ctx_of(agenda))
        assert "df" in ids(result.items)

    def test_flagged_default_available(self, agenda: OFModel) -> None:
        result = P.evaluate_builtin(builtin("flagged"), ctx_of(agenda))
        assert ids(result.items) == ["ft", "fl", "inflag"]

    def test_flagged_setting(self, agenda: OFModel) -> None:
        agenda.settings["ProcessFlaggedItemsv2"] = {"filterIdentifier": "incomplete"}
        assert "fd" in ids(P.evaluate_builtin(builtin("flagged"), ctx_of(agenda)).items)
        agenda.settings["ProcessFlaggedItemsv2"] = {"filterIdentifier": "all"}
        result = P.evaluate_builtin(builtin("flagged"), ctx_of(agenda))
        assert "fd" in ids(result.items) and "all" in result.description

    def test_review(self, agenda: OFModel) -> None:
        result = P.evaluate_builtin(builtin("review"), ctx_of(agenda))
        assert ids(result.items) == ["rv"]


class TestSorting:
    def test_date_sorts(self) -> None:
        m = model_of(
            task("old", inbox=True, completed=datetime(2026, 1, 1, tzinfo=UTC)),
            task("new", inbox=True, completed=datetime(2026, 2, 1)),
            task("never", inbox=True),
        )
        ctx = ctx_of(m)
        items, label = P._sorted(list(m.tasks.values()), ctx, "completed")
        assert ids(items) == ["new", "old", "never"] and "newest" in label

    def test_custom_sort_hint(self) -> None:
        m = model_of(
            task("a", inbox=True, modified=datetime(2026, 1, 1, tzinfo=UTC)),
            task("b", inbox=True, modified=datetime(2026, 3, 1, tzinfo=UTC)),
        )
        persp = custom([{"actionAvailability": "remaining"}], sort="modified")
        assert ids(P.evaluate_custom(persp, ctx_of(m)).items) == ["b", "a"]

    def test_outline_order(self) -> None:
        m = model_of(
            Folder("f", "F", None, 5, EPOCH, EPOCH),
            project("p2", "B", rank=2),
            project("p1", "A", rank=1, folder_id="f"),
            task("x", project_id="p2", parent_task_id="p2"),
            task("y", project_id="p1", parent_task_id="p1"),
            task("y1", project_id="p1", parent_task_id="y"),
            task("i", inbox=True),
            task("i1", parent_task_id="i"),
        )
        items, label = P._sorted(list(m.tasks.values()), ctx_of(m), None)
        assert label == "outline"
        assert ids(items) == ["i", "i1", "x", "y", "y1"]


class TestLookup:
    @pytest.fixture
    def m(self) -> OFModel:
        return model_of(
            Perspective("k1", "DeepWork", [{"actionAvailability": "remaining"}]),
            Perspective("k2", "Deep Focus", []),
            Perspective("ProcessCompleted", "Completed", [{"actionAvailability": "completed"}]),
            Perspective("ProcessInbox.v2", "Inbox shadow", []),
            settings={"PerspectiveOrder_v3": ["ProcessForecast.v2", "k1"]},
        )

    def test_order_and_shadowing(self, m: OFModel) -> None:
        names = [ref.name for ref in P.all_perspectives(m)]
        assert names[:2] == ["Forecast", "DeepWork"]
        assert "Inbox shadow" not in names

    def test_find_variants(self, m: OFModel) -> None:
        assert P.find_perspective(m, "予測").name == "Forecast"
        assert P.find_perspective(m, "ｄｅｅｐｗｏｒｋ").name == "DeepWork"
        assert P.find_perspective(m, "k2").name == "Deep Focus"
        assert P.find_perspective(m, "完了").name == "Completed"
        assert P.find_perspective(m, "focus").name == "Deep Focus"

    def test_find_errors(self, m: OFModel) -> None:
        with pytest.raises(OFPerspectiveNotFound, match="ambiguous"):
            P.find_perspective(m, "deep")
        with pytest.raises(OFPerspectiveNotFound, match="Available perspectives"):
            P.find_perspective(m, "zzz")
        with pytest.raises(OFPerspectiveNotFound, match="Available perspectives"):
            P.find_perspective(m, "  ")
        dup = model_of(Perspective("a", "Same", []), Perspective("b", "Same", []))
        with pytest.raises(OFPerspectiveNotFound, match="ambiguous"):
            P.find_perspective(dup, "same")

    def test_list_rows(self, m: OFModel) -> None:
        rows = P.list_perspectives(m, now=datetime(2026, 11, 21, tzinfo=UTC))
        kinds = {row["name"]: row["kind"] for row in rows}
        assert kinds["Forecast"] == "builtin" and kinds["DeepWork"] == "custom"
        assert all("shows" in row for row in rows)

    def test_order_setting_not_a_list(self) -> None:
        m = model_of(settings={"PerspectiveOrder_v3": "junk"})
        assert [r.name for r in P.all_perspectives(m)][0] == "Inbox"


class TestPayload:
    def test_custom_payload_rows(self) -> None:
        long_note = "n" * 400
        m = model_of(
            Tag("t", "Home", None, 0),
            project("p", "Proj"),
            task("g", "Group", project_id="p", parent_task_id="p"),
            task(
                "c",
                "Child",
                project_id="p",
                parent_task_id="g",
                tag_ids=("t", "ghost-tag"),
                due=NOW - timedelta(days=1),
                estimated_minutes=5,
                note=long_note,
                flagged=True,
            ),
            Perspective("k", "Mine", [{"actionHasAnyOfTags": ["t"]}]),
        )
        payload = P.perspective_payload(m, "mine", now=NOW)
        row = payload["items"][0]
        assert row["parent_task"] == "Group" and row["project"] == "Proj"
        assert row["tags"] == ["Home"] and row["overdue"] is True
        assert row["estimated_minutes"] == 5 and row["note"].endswith("…")
        assert row["flagged"] is True and row["available"] is True
        assert "truncated" not in payload and "warnings" not in payload

    def test_truncation_warning_and_available_filter(self) -> None:
        m = model_of(
            *[task(f"t{i}", inbox=True, rank=i) for i in range(5)],
            task("later", inbox=True, rank=9, start=NOW + timedelta(days=1)),
            Perspective("k", "All", [{"actionAvailability": "remaining"}, {"oddRule": 1}]),
        )
        payload = P.perspective_payload(m, "All", limit=2, now=NOW)
        assert payload["returned"] == 2 and payload["total"] == 6
        assert "truncated" in payload and payload["warnings"]
        available = P.perspective_payload(m, "All", limit=0, only_available=True, now=NOW)
        assert available["total"] == 5 and available["returned"] == 1
        big = P.perspective_payload(m, "All", limit=10_000, now=NOW)
        assert big["returned"] == 6

    def test_builtin_payload_with_projects(self) -> None:
        m = model_of(project("fl", "Flag proj", flagged=True))
        payload = P.perspective_payload(m, "flagged", now=NOW)
        assert payload["kind"] == "builtin"
        assert payload["items"][0]["kind"] == "project"
        assert "project" not in payload["items"][0]

    def test_uses_projects_detection(self) -> None:
        assert P._uses_projects([{"actionIsProject": True}])
        assert P._uses_projects(["junk", {"aggregateRules": [{"actionIsProjectOrGroup": True}]}])
        assert not P._uses_projects([{"aggregateRules": "bad"}])


class TestCoverageEdges:
    def test_defer_and_planned_date_rules(self) -> None:
        m = model_of(
            task("d", inbox=True, start=datetime(2026, 11, 21, 8)),
            task("p", inbox=True, planned=datetime(2026, 11, 21, 9)),
        )
        assert run(m, [{"actionDateField": "defer", "actionDateIsToday": True}]) == ["d"]
        assert run(m, [{"actionDateField": "planned", "actionDateIsToday": True}]) == ["p"]

    def test_blocked_memo(self) -> None:
        m = model_of(task("a", inbox=True))
        ctx = ctx_of(m)
        assert ctx.is_blocked(m.tasks["a"]) is False
        assert ctx.is_blocked(m.tasks["a"]) is False

    def test_tag_descendants_already_requested(self) -> None:
        m = model_of(Tag("h", "Home", None, 0), Tag("k", "Kitchen", "h", 0))
        assert ctx_of(m).tag_with_descendants(["h", "k"]) == {"h", "k"}

    def test_focus_on_project_rows(self) -> None:
        m = model_of(
            Folder("f", "Area", None, 0, EPOCH, EPOCH),
            project("p", folder_id="f"),
            project("q"),
        )
        rules = [{"actionIsProject": True}, {"actionWithinFocus": ["f"]}]
        assert run(m, rules) == ["p"]
