"""Parser tests for perspectives, settings, tag links and newer OmniFocus 4 fields."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import UTC, datetime

from omnifocus.parser import _plist_value, build_model
from tests.conftest import make_zip

HEAD = '<?xml version="1.0" encoding="UTF-8"?>\n<omnifocus xmlns="http://www.omnigroup.com/namespace/OmniFocus/v2">'
TAIL = "</omnifocus>"


def model(body: str, *transactions: str):  # type: ignore[no-untyped-def]
    return build_model(
        make_zip(HEAD + body + TAIL), [make_zip(HEAD + t + TAIL) for t in transactions]
    )


def persp(pid: str, plist_inner: str) -> str:
    return f'<perspective id="{pid}"><plist version="1.0">{plist_inner}</plist></perspective>'


def dict_of(**pairs: str) -> str:
    return "<dict>" + "".join(f"<key>{k}</key>{v}" for k, v in pairs.items()) + "</dict>"


RULES = "[{&quot;actionAvailability&quot;:&quot;remaining&quot;}]"


class TestPerspectiveParsing:
    def test_full_perspective(self) -> None:
        view = dict_of(
            viewMode="<string>context</string>",
            viewModeState=dict_of(context=dict_of(sort="<string>modified</string>")),
        )
        m = model(
            persp(
                "k1",
                dict_of(
                    name="<string> Home </string>",
                    filterRules=f"<string>{RULES}</string>",
                    topLevelFilterAggregation="<string>any</string>",
                    viewState=view,
                ),
            )
        )
        p = m.perspectives["k1"]
        assert p.name == "Home"
        assert p.filter_rules == [{"actionAvailability": "remaining"}]
        assert p.aggregation == "any" and p.view_mode == "context" and p.sort == "modified"
        assert p.rules_error is None

    def test_rules_as_array_and_defaults(self) -> None:
        m = model(
            persp(
                "k2",
                dict_of(
                    name="<string>Arr</string>",
                    filterRules="<array><dict><key>actionHasNoProject</key><true/></dict></array>",
                    topLevelFilterAggregation="<string>weird</string>",
                    viewState=dict_of(viewMode="<integer>3</integer>"),
                ),
            )
        )
        p = m.perspectives["k2"]
        assert p.filter_rules == [{"actionHasNoProject": True}]
        assert p.aggregation == "all" and p.view_mode is None and p.sort is None

    def test_view_state_without_sort(self) -> None:
        view = dict_of(
            viewMode="<string>project</string>",
            viewModeState=dict_of(project=dict_of(collation="<string>none</string>")),
        )
        m = model(persp("k", dict_of(name="<string>V</string>", viewState=view)))
        assert m.perspectives["k"].view_mode == "project"
        assert m.perspectives["k"].sort is None
        assert m.perspectives["k"].filter_rules == []

    def test_bad_rules(self) -> None:
        m = model(
            persp(
                "bad", dict_of(name="<string>Bad</string>", filterRules="<string>{oops</string>")
            ),
            persp("obj", dict_of(name="<string>Obj</string>", filterRules="<string>{}</string>")),
        )
        assert "not valid JSON" in (m.perspectives["bad"].rules_error or "")
        assert m.perspectives["obj"].rules_error == "filterRules is not a JSON list"

    def test_skipped_perspectives(self) -> None:
        m = model(
            persp("noname", dict_of(filterRules=f"<string>{RULES}</string>")),
            persp("blank", dict_of(name="<string>  </string>")),
            persp("arr", "<array/>"),
            '<perspective id="noplist"><added>2026-01-01T00:00:00.000Z</added></perspective>',
            '<perspective id="emptyplist"><plist version="1.0"></plist></perspective>',
        )
        assert m.perspectives == {}

    def test_perspective_update_transaction(self) -> None:
        base = persp("k", dict_of(name="<string>Old</string>"))
        tx = (
            '<perspective id="k" op="update"><plist version="1.0">'
            + dict_of(name="<string>New</string>")
            + "</plist></perspective>"
        )
        assert model(base, tx).perspectives["k"].name == "New"


class TestSettings:
    def test_setting_values(self) -> None:
        body = "".join(
            f'<setting id="{sid}"><plist version="1.0">{value}</plist></setting>'
            for sid, value in [
                ("f", "<false/>"),
                ("t", "<true/>"),
                ("i", "<integer>42</integer>"),
                ("bad-i", "<integer>x</integer>"),
                ("r", "<real>1.5</real>"),
                ("bad-r", "<real>y</real>"),
                ("d", "<data>aGk=</data>"),
                ("bad-d", "<data>!!!</data>"),
                ("s", "<string>hello</string>"),
                ("e", "<string/>"),
                ("arr", "<array><string>a</string><integer>1</integer></array>"),
                ("dict", dict_of(filterIdentifier="<string>incomplete</string>")),
                ("none", ""),
            ]
        )
        s = model(body).settings
        assert s["f"] is False and s["t"] is True
        assert s["i"] == 42 and s["bad-i"] is None
        assert s["r"] == 1.5 and s["bad-r"] is None
        assert s["d"] == b"hi" and s["bad-d"] is None
        assert s["s"] == "hello" and s["e"] == ""
        assert s["arr"] == ["a", 1]
        assert s["dict"] == {"filterIdentifier": "incomplete"}
        assert s["none"] is None

    def test_plist_value_without_namespace(self) -> None:
        assert _plist_value(ET.fromstring("<integer> 7 </integer>"))  # noqa: S314 == 7


class TestTagLinksAndFields:
    def test_task_to_tag_links(self) -> None:
        body = """
        <context id="a"><name>A</name><prohibits-next-action>true</prohibits-next-action></context>
        <context id="b"><name>B</name></context>
        <folder id="f"><name>Old</name><hidden>2026-10-31T11:56:13.441Z</hidden></folder>
        <task id="p"><project><status>active</status></project><name>P</name>
          <order>sequential</order><context idref="a"/></task>
        <task id="t"><task idref="p"/><name>T</name><context idref="a"/>
          <planned>2026-11-20T09:00:00.000</planned></task>
        <task id="u"><inbox>true</inbox><name>U</name><context idref="b"/></task>
        <task-to-tag id="t.b"><task idref="t"/><context idref="b"/>
          <rank-in-task>0002</rank-in-task></task-to-tag>
        <task-to-tag id="t.a"><task idref="t"/><context idref="a"/>
          <rank-in-task>0001</rank-in-task></task-to-tag>
        <task-to-tag id="p.b"><task idref="p"/><context idref="b"/>
          <rank-in-task>0002</rank-in-task></task-to-tag>
        <task-to-tag id="broken"><task idref="t"/></task-to-tag>
        """
        m = model(body)
        assert m.tasks["t"].tag_ids == ("a", "b")
        assert m.tasks["t"].planned == datetime(2026, 11, 20, 9)
        assert m.tasks["u"].tag_ids == ("b",)
        assert m.projects["p"].tag_ids == ("b", "a")
        assert m.projects["p"].order == "sequential"
        assert m.tags["a"].on_hold is True and m.tags["b"].on_hold is False
        assert m.folders["f"].hidden == datetime(2026, 10, 31, 11, 56, 13, 441000, tzinfo=UTC)

    def test_deleted_link_removes_secondary_tag(self) -> None:
        body = """
        <context id="a"><name>A</name></context><context id="b"><name>B</name></context>
        <task id="t"><inbox>true</inbox><name>T</name><context idref="a"/></task>
        <task-to-tag id="t.a"><task idref="t"/><context idref="a"/></task-to-tag>
        <task-to-tag id="t.b"><task idref="t"/><context idref="b"/></task-to-tag>
        """
        assert model(body).tasks["t"].tag_ids == ("a", "b")
        tx = '<task-to-tag id="t.b" op="delete"/>'
        assert model(body, tx).tasks["t"].tag_ids == ("a",)
