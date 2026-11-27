"""OmniFocus .ofocus bundle parser.

Parses the ZIP-based ``.ofocus`` bundle format used by OmniFocus 4.  The bundle
consists of one *baseline* ZIP (``00000000000000=<id>.zip``) and zero or more
*transaction* ZIPs (``<ISO8601_timestamp>=<clientID>+<parentID>.zip``).

Each ZIP contains a single ``contents.xml`` file with an ``<omnifocus>``
root element.  The baseline contains the full snapshot; transactions contain
incremental changes using upsert / delete semantics.

Merge semantics
---------------
For every element with an ``id`` attribute in a transaction:

- ``op="update"`` elements merge only the changed fields into the prior snapshot.
- ``op="delete"`` elements remove the id from the model.
- Legacy name-less deletion markers are still accepted for task and context
  elements, but not for folders where partial updates may legitimately omit
  ``<name>``.

Transactions are applied in lexicographic filename order, which equals
chronological order because filenames are ISO 8601 UTC timestamps.

Usage::

    from omnifocus.parser import build_model

    with open("baseline.zip", "rb") as f:
        baseline_bytes = f.read()

    model = build_model(baseline_bytes, transaction_bytes_list=[])
"""

from __future__ import annotations

__author__ = "Maciej Szymczak <maciej@szymczak.at>"

import base64
import io
import json
import xml.etree.ElementTree as ET
import zipfile
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from omnifocus.errors import OFParseError
from omnifocus.models import (
    PARSER_VERSION,
    Folder,
    OFModel,
    Perspective,
    Project,
    RichNote,
    Tag,
    Task,
)
from omnifocus.recursion_limit import ensure_recursion_limit

# OmniFocus v2 XML namespace
_NS = "{http://www.omnigroup.com/namespace/OmniFocus/v2}"

# Sentinel: a datetime that will never match a real modification time
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Low-level ZIP / XML helpers
# ---------------------------------------------------------------------------


def load_xml_from_zip(zip_bytes: bytes) -> ET.Element:
    """Parse ``contents.xml`` from a ZIP archive given as raw bytes.

    Args:
        zip_bytes: Raw bytes of a ``.zip`` file containing ``contents.xml``.

    Returns:
        The root ``ET.Element`` of the parsed XML document.

    Raises:
        OFParseError: If the bytes are not a valid ZIP, if ``contents.xml``
            is missing, or if the XML is malformed.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            try:
                data = zf.read("contents.xml")
            except KeyError as exc:
                raise OFParseError("contents.xml not found in ZIP archive") from exc
    except zipfile.BadZipFile as exc:
        raise OFParseError(f"Invalid ZIP archive: {exc}") from exc

    try:
        return ET.fromstring(data.decode("utf-8"))  # noqa: S314
    except ET.ParseError as exc:
        raise OFParseError(f"Malformed XML in contents.xml: {exc}") from exc
    except UnicodeDecodeError as exc:
        raise OFParseError(f"contents.xml is not valid UTF-8: {exc}") from exc


def _tag(local: str) -> str:
    """Return the fully-qualified tag name with the OmniFocus namespace prefix."""
    return f"{_NS}{local}"


def _text(el: ET.Element, local: str, default: str = "") -> str:
    """Return the text content of a child element, or *default* if absent."""
    child = el.find(_tag(local))
    if child is None or child.text is None:
        return default
    return child.text.strip()


def _local(tag: str) -> str:
    """Return *tag* without its XML namespace."""
    return tag.rsplit("}", 1)[-1]


def _escape_xml(text: str, *, attribute: bool = False) -> str:
    escaped = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return escaped.replace('"', "&quot;") if attribute else escaped


def _serialize_local(el: ET.Element) -> str:
    """Serialize *el* with un-prefixed tag names, as OmniFocus writes them."""
    tag = _local(el.tag)
    attrs = "".join(
        f' {_local(key)}="{_escape_xml(value, attribute=True)}"' for key, value in el.attrib.items()
    )
    children = "".join(_serialize_local(child) + _escape_xml(child.tail or "") for child in el)
    text = _escape_xml(el.text or "")
    if not text and not children:
        return f"<{tag}{attrs}/>"
    return f"<{tag}{attrs}>{text}{children}</{tag}>"


def _note(el: ET.Element) -> str:
    """Return the plain text of *el*'s ``<note>``.

    App-written notes are rich text (``<note><text><p><run><lit>…``); each ``<p>`` becomes
    one line built from its ``<lit>`` runs, and the result is a :class:`RichNote` that keeps
    the original XML for writers. Plain-text notes (as older CLI versions wrote them) are
    returned as-is.
    """
    child = el.find(_tag("note"))
    if child is None:
        return ""
    text_el = child.find(_tag("text"))
    if text_el is None:
        return (child.text or "").strip()
    lines = [
        "".join("".join(lit.itertext()) for lit in paragraph.iter(_tag("lit")))
        for paragraph in text_el.findall(_tag("p"))
    ]
    return RichNote("\n".join(lines).strip("\n"), _serialize_local(child))


def _bool(el: ET.Element, local: str, default: bool = False) -> bool:
    """Return the boolean text value of a child element."""
    raw = _text(el, local)
    if not raw:
        return default
    return raw.lower() == "true"


def _int(el: ET.Element, local: str, default: int | None = None) -> int | None:
    """Return the integer text value of a child element, or *default*."""
    raw = _text(el, local)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _parse_dt_utc(value: str) -> datetime | None:
    """Parse an ISO 8601 UTC timestamp (with trailing ``Z``) to a UTC datetime.

    Args:
        value: A string like ``"2026-03-22T15:40:11.347Z"``.  Returns ``None``
            for empty strings.
    """
    if not value:
        return None
    # Normalise the Z suffix to +00:00 for fromisoformat (Python 3.11+)
    normalised = value.rstrip("Z") + "+00:00" if value.endswith("Z") else value
    try:
        return datetime.fromisoformat(normalised)
    except ValueError:
        return None


def _parse_dt_local(value: str) -> datetime | None:
    """Parse a local-time ISO 8601 datetime (no timezone suffix).

    Args:
        value: A string like ``"2026-03-23T19:00:00.000"``.  Returns ``None``
            for empty strings.
    """
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _idref(el: ET.Element, local: str) -> str | None:
    """Return the ``idref`` attribute of a single child element, or ``None``."""
    child = el.find(_tag(local))
    if child is None:
        return None
    return child.get("idref")


# ---------------------------------------------------------------------------
# Element indexing (merge step)
# ---------------------------------------------------------------------------

# Raw element index: tag-local-name → {id: ET.Element}
_RawIndex = dict[str, dict[str, ET.Element]]


def _merge_update_element(existing: ET.Element, update: ET.Element) -> ET.Element:
    """Merge an ``op="update"`` element into an existing indexed element.

    OmniFocus update transactions often contain only the changed fields. Replacing the
    whole indexed element would discard unchanged fields from earlier snapshots.
    """
    merged = deepcopy(existing)
    for attr_name, attr_value in update.attrib.items():
        if attr_name != "op":
            merged.set(attr_name, attr_value)

    merged_children: dict[str, ET.Element] = {child.tag: child for child in list(merged)}
    for child in update:
        existing_child = merged_children.get(child.tag)
        if existing_child is None:
            merged.append(deepcopy(child))
            continue

        # ``<project>`` acts as a nested container whose fields are often updated
        # partially. Replacing the whole child would drop the earlier folder link.
        # A self-closing ``<project/>`` is different: it changes a project back
        # into a plain task and must replace the prior nested payload entirely.
        if child.tag == _tag("project"):
            if len(list(child)) == 0:
                merged.remove(existing_child)
                cloned_child = deepcopy(child)
                merged.append(cloned_child)
                merged_children[child.tag] = cloned_child
                continue
            nested = _merge_update_element(existing_child, child)
            merged.remove(existing_child)
            merged.append(nested)
            merged_children[child.tag] = nested
            continue

        merged.remove(existing_child)
        cloned_child = deepcopy(child)
        merged.append(cloned_child)
        merged_children[child.tag] = cloned_child
    return merged


def _materialize_reference_snapshot(elem: ET.Element) -> ET.Element:
    """Return a concrete element built from a ``reference-snapshot`` wrapper.

    OmniFocus transactions sometimes ship ``op="reference"`` elements whose
    actual fields live under ``<reference-snapshot>``. Storing the wrapper
    directly would discard fields such as ``<name>`` or parent ``idref`` links.
    """
    snapshot = elem.find(_tag("reference-snapshot"))
    if snapshot is None:
        return elem

    attrs = {key: value for key, value in elem.attrib.items() if key != "op"}
    materialized = ET.Element(elem.tag, attrs)
    for child in snapshot:
        materialized.append(deepcopy(child))
    return materialized


def _index_elements(root: ET.Element, index: _RawIndex) -> None:
    """Index all direct children of *root* into *index* using upsert/delete semantics.

    Elements without an ``id`` attribute are silently ignored (e.g. perspective
    and setting elements that have no id).

    Args:
        root: An ``<omnifocus>`` element from baseline or a transaction ZIP.
        index: Mutable dict to update in-place.
    """
    for elem in root:
        eid = elem.get("id")
        if not eid:
            continue
        local = elem.tag.replace(_NS, "")
        bucket = index.setdefault(local, {})
        op = elem.get("op")

        if op == "update":
            existing = bucket.get(eid)
            if existing is None:
                bucket[eid] = elem
            else:
                bucket[eid] = _merge_update_element(existing, elem)
            continue

        if op == "reference":
            reference = _materialize_reference_snapshot(elem)
            existing = bucket.get(eid)
            if existing is None:
                bucket[eid] = reference
            else:
                bucket[eid] = _merge_update_element(existing, reference)
            continue

        if op == "delete":
            bucket.pop(eid, None)
            continue

        # Legacy deletion marker: element is present but has no <name> child.
        # Applies to task/context elements. Folder updates may legitimately
        # omit ``<name>`` and still carry useful metadata such as parent/rank.
        has_name = elem.find(_tag("name")) is not None
        if not has_name and local in ("task", "context"):
            bucket.pop(eid, None)
        else:
            bucket[eid] = elem


# ---------------------------------------------------------------------------
# Folder builder
# ---------------------------------------------------------------------------


def _build_folder(el: ET.Element) -> Folder | None:
    """Convert a raw ``<folder>`` XML element into a :class:`Folder`.

    Returns ``None`` if mandatory fields are missing or unparseable.
    """
    fid = el.get("id")
    name = _text(el, "name")
    if not fid or not name:
        return None

    added_raw = _text(el, "added")
    modified_raw = _text(el, "modified")

    return Folder(
        id=fid,
        name=name,
        parent_folder_id=_idref(el, "folder"),
        rank=_int(el, "rank", 0) or 0,
        added=_parse_dt_utc(added_raw) or _EPOCH,
        modified=_parse_dt_utc(modified_raw) or _EPOCH,
        hidden=_parse_dt_utc(_text(el, "hidden")),
    )


# ---------------------------------------------------------------------------
# Tag / context builder
# ---------------------------------------------------------------------------


def _build_tag(el: ET.Element) -> Tag | None:
    """Convert a raw ``<context>`` XML element into a :class:`Tag`."""
    tid = el.get("id")
    name = _text(el, "name")
    if not tid or not name:
        return None
    return Tag(
        id=tid,
        name=name,
        parent_tag_id=_idref(el, "context"),
        rank=_int(el, "rank", 0) or 0,
        added=_parse_dt_utc(_text(el, "added")),
        modified=_parse_dt_utc(_text(el, "modified")),
        note=_note(el),
        hidden=_parse_dt_utc(_text(el, "hidden")),
        on_hold=_bool(el, "prohibits-next-action"),
    )


# ---------------------------------------------------------------------------
# Property-list values (perspectives and settings)
# ---------------------------------------------------------------------------


def _plist_value(el: ET.Element) -> Any:
    """Decode one Apple property-list value element (namespace-agnostic).

    Perspectives and settings embed ``<plist>`` payloads inside the OmniFocus
    XML, so the elements inherit the OmniFocus namespace; only local names
    matter here.
    """
    local = el.tag.rsplit("}", 1)[-1]
    if local == "dict":
        children = list(el)
        result: dict[str, Any] = {}
        for key_el, value_el in zip(children[::2], children[1::2], strict=False):
            result[(key_el.text or "").strip()] = _plist_value(value_el)
        return result
    if local == "array":
        return [_plist_value(child) for child in el]
    if local in ("true", "false"):
        return local == "true"
    text = el.text or ""
    if local == "integer":
        try:
            return int(text.strip())
        except ValueError:
            return None
    if local == "real":
        try:
            return float(text.strip())
        except ValueError:
            return None
    if local == "data":
        try:
            return base64.b64decode("".join(text.split()), validate=True)
        except ValueError:
            return None
    return text


def _plist_of(el: ET.Element) -> Any:
    """Return the decoded value of *el*'s ``<plist>`` child, or ``None``."""
    plist = el.find(_tag("plist"))
    if plist is None:
        return None
    values = list(plist)
    return _plist_value(values[0]) if values else None


def _build_perspective(el: ET.Element) -> Perspective | None:
    """Convert a raw ``<perspective>`` element into a :class:`Perspective`.

    Returns ``None`` for perspectives without a name (e.g. layout-only stubs).
    """
    pid = el.get("id")
    payload = _plist_of(el)
    if not pid or not isinstance(payload, dict):
        return None
    name = payload.get("name")
    if not isinstance(name, str) or not name.strip():
        return None

    rules: list[Any] = []
    rules_error: str | None = None
    raw_rules = payload.get("filterRules")
    if isinstance(raw_rules, str) and raw_rules.strip():
        try:
            decoded = json.loads(raw_rules)
        except ValueError as exc:
            rules_error = f"filterRules is not valid JSON: {exc}"
        else:
            if isinstance(decoded, list):
                rules = decoded
            else:
                rules_error = "filterRules is not a JSON list"
    elif isinstance(raw_rules, list):
        rules = raw_rules

    aggregation = payload.get("topLevelFilterAggregation")
    view_state = payload.get("viewState")
    view_mode: str | None = None
    sort: str | None = None
    if isinstance(view_state, dict):
        mode = view_state.get("viewMode")
        view_mode = mode if isinstance(mode, str) else None
        mode_states = view_state.get("viewModeState")
        if isinstance(mode_states, dict) and view_mode is not None:
            mode_state = mode_states.get(view_mode)
            if isinstance(mode_state, dict) and isinstance(mode_state.get("sort"), str):
                sort = mode_state["sort"]

    return Perspective(
        id=pid,
        name=name.strip(),
        filter_rules=rules,
        aggregation=aggregation if aggregation in ("all", "any", "none") else "all",
        view_mode=view_mode,
        sort=sort,
        rules_error=rules_error,
    )


def _task_tag_links(index: _RawIndex) -> dict[str, list[str]]:
    """Return ``task id -> ordered tag ids`` from ``<task-to-tag>`` link elements.

    OmniFocus 4 keeps only the *primary* tag as an inline ``<context idref>`` on
    the task; every assigned tag is recorded as a separate ``<task-to-tag>``
    element (id ``"<task>.<tag>"``) ordered by ``<rank-in-task>``.
    """
    ranked: dict[str, list[tuple[str, str]]] = {}
    for el in index.get("task-to-tag", {}).values():
        task_id = _idref(el, "task")
        tag_id = _idref(el, "context")
        if not task_id or not tag_id:
            continue
        ranked.setdefault(task_id, []).append((_text(el, "rank-in-task"), tag_id))
    return {
        task_id: [tag_id for _rank, tag_id in sorted(pairs)] for task_id, pairs in ranked.items()
    }


def _merge_tag_ids(inline: tuple[str, ...], linked: list[str] | None) -> tuple[str, ...]:
    """Combine the inline primary tag with the full linked tag list (deduplicated)."""
    if not linked:
        return inline
    merged = list(linked)
    merged.extend(tag_id for tag_id in inline if tag_id not in merged)
    return tuple(merged)


# ---------------------------------------------------------------------------
# Project / task builder helpers
# ---------------------------------------------------------------------------


def _is_project(task_el: ET.Element) -> bool:
    """Return True if the ``<task>`` element represents a project container.

    A task element is a *project* when its ``<project>`` child element exists
    **and** has at least one child element (e.g. ``<folder>``, ``<status>``,
    ``<singleton>``).  A plain task has a self-closing ``<project/>`` with no
    children.
    """
    proj_child = task_el.find(_tag("project"))
    return proj_child is not None and len(list(proj_child)) > 0


def _build_project(el: ET.Element) -> Project | None:
    """Convert a project ``<task>`` element into a :class:`Project`."""
    pid = el.get("id")
    name = _text(el, "name")
    if not pid or not name:
        return None

    proj = el.find(_tag("project"))
    folder_id: str | None = None
    status = "active"
    singleton = False
    if proj is not None:
        folder_id = _idref(proj, "folder")
        raw_status = _text(proj, "status")
        if raw_status:
            status = raw_status
        singleton = _bool(proj, "singleton")
    if folder_id is None:
        folder_id = _idref(el, "folder")

    tag_ids = tuple(c.get("idref", "") for c in el.findall(_tag("context")) if c.get("idref"))

    return Project(
        id=pid,
        name=name,
        folder_id=folder_id,
        status=status,
        singleton=singleton,
        rank=_int(el, "rank", 0) or 0,
        added=_parse_dt_utc(_text(el, "added")) or _EPOCH,
        modified=_parse_dt_utc(_text(el, "modified")) or _EPOCH,
        flagged=_bool(el, "flagged"),
        due=_parse_dt_local(_text(el, "due")),
        start=_parse_dt_local(_text(el, "start")),
        note=_note(el),
        completed=_parse_dt_utc(_text(el, "completed")),
        last_review=(_parse_dt_utc(_text(proj, "last-review")) if proj is not None else None),
        next_review=(_parse_dt_utc(_text(proj, "next-review")) if proj is not None else None),
        review_interval=((_text(proj, "review-interval") or None) if proj is not None else None),
        tag_ids=tag_ids,
        repetition_rule=_text(el, "repetition-rule") or None,
        repetition_method=_text(el, "repetition-method") or None,
        repetition_schedule_type=_text(el, "repetition-schedule-type") or None,
        repetition_anchor_date=_text(el, "repetition-anchor-date") or None,
        catch_up_automatically=_bool(el, "catch-up-automatically"),
        next_clone_identifier=_int(el, "next-clone-identifier", 0) or 0,
        due_date_alarm_policy=_text(el, "due-date-alarm-policy") or None,
        defer_date_alarm_policy=_text(el, "defer-date-alarm-policy") or None,
        latest_time_to_start_alarm_policy=_text(el, "latest-time-to-start-alarm-policy") or None,
        planned_date_alarm_policy=_text(el, "planned-date-alarm-policy") or None,
        order=_text(el, "order") or "parallel",
    )


def _build_task(el: ET.Element, project_id: str | None) -> Task | None:
    """Convert a leaf/intermediate ``<task>`` XML element into a :class:`Task`.

    Args:
        el: The ``<task>`` XML element.
        project_id: Pre-resolved containing project id, or ``None`` for inbox tasks.
    """
    tid = el.get("id")
    name = _text(el, "name")
    if not tid or not name:
        return None

    # Hidden: an empty <hidden/> means not hidden; a timestamp means dropped.
    hidden_raw = _text(el, "hidden")
    hidden = _parse_dt_utc(hidden_raw) if hidden_raw else None

    tag_ids = tuple(c.get("idref", "") for c in el.findall(_tag("context")) if c.get("idref"))

    return Task(
        id=tid,
        name=name,
        parent_task_id=_idref(el, "task"),
        project_id=project_id,
        inbox=_bool(el, "inbox"),
        completed=_parse_dt_utc(_text(el, "completed")),
        flagged=_bool(el, "flagged"),
        due=_parse_dt_local(_text(el, "due")),
        start=_parse_dt_local(_text(el, "start")),
        hidden=hidden,
        note=_note(el),
        rank=_int(el, "rank", 0) or 0,
        repetition_rule=_text(el, "repetition-rule") or None,
        estimated_minutes=_int(el, "estimated-minutes"),
        tag_ids=tag_ids,
        added=_parse_dt_utc(_text(el, "added")) or _EPOCH,
        modified=_parse_dt_utc(_text(el, "modified")) or _EPOCH,
        order=_text(el, "order") or "parallel",
        repetition_method=_text(el, "repetition-method") or None,
        repetition_schedule_type=_text(el, "repetition-schedule-type") or None,
        repetition_anchor_date=_text(el, "repetition-anchor-date") or None,
        catch_up_automatically=_bool(el, "catch-up-automatically"),
        next_clone_identifier=_int(el, "next-clone-identifier", 0) or 0,
        due_date_alarm_policy=_text(el, "due-date-alarm-policy") or None,
        defer_date_alarm_policy=_text(el, "defer-date-alarm-policy") or None,
        latest_time_to_start_alarm_policy=_text(el, "latest-time-to-start-alarm-policy") or None,
        planned_date_alarm_policy=_text(el, "planned-date-alarm-policy") or None,
        planned=_parse_dt_local(_text(el, "planned")),
    )


# ---------------------------------------------------------------------------
# Parent chain resolution
# ---------------------------------------------------------------------------


def _resolve_project_ids(
    raw_tasks: dict[str, ET.Element],
    project_ids: set[str],
) -> dict[str, str | None]:
    """Build a mapping from every task id to its containing project id.

    Uses memoised traversal of the parent chain (``<task idref="..."/>``) to
    avoid O(n²) behaviour on deeply-nested tasks.

    Args:
        raw_tasks: The raw XML element dict for all ``<task>`` elements.
        project_ids: Set of task ids that are projects.

    Returns:
        Dict mapping each task id → project id (or ``None`` for inbox tasks
        that have no parent project).
    """
    memo: dict[str, str | None] = {}

    def resolve(tid: str, visited: set[str]) -> str | None:
        if tid in memo:
            return memo[tid]
        if tid in visited:
            # Cycle guard — should never happen in a valid OF database
            memo[tid] = None
            return None
        if tid in project_ids:
            memo[tid] = tid
            return tid
        visited.add(tid)
        el = raw_tasks.get(tid)
        if el is None:
            memo[tid] = None
            return None
        parent_idref = _idref(el, "task")
        if parent_idref is None:
            memo[tid] = None
            return None
        result = resolve(parent_idref, visited)
        memo[tid] = result
        return result

    for tid in raw_tasks:
        if tid not in memo:
            resolve(tid, set())

    return memo


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_model(
    baseline_bytes: bytes,
    transaction_bytes_list: list[bytes] | None = None,
) -> OFModel:
    """Parse a ``.ofocus`` bundle into an :class:`OFModel`.

    Applies the baseline first, then each transaction in the provided order
    (callers are responsible for sorting transactions chronologically by
    filename before passing them here).

    Args:
        baseline_bytes: Raw bytes of the baseline ``00000000000000=*.zip``.
        transaction_bytes_list: Optional list of raw ZIP bytes for incremental
            transactions, in chronological order.

    Returns:
        A fully populated :class:`OFModel`.

    Raises:
        OFParseError: If any ZIP or XML is malformed.
    """
    if transaction_bytes_list is None:
        transaction_bytes_list = []

    # Deep parent chains and nested ElementTree trees recurse past the default
    # limit during deepcopy/resolve; raise it before parsing.
    ensure_recursion_limit()

    # Step 1: build raw element index from baseline
    index: _RawIndex = {}
    baseline_root = load_xml_from_zip(baseline_bytes)
    _index_elements(baseline_root, index)

    # Step 2: apply transactions (chronological order maintained by caller)
    for tx_bytes in transaction_bytes_list:
        tx_root = load_xml_from_zip(tx_bytes)
        _index_elements(tx_root, index)

    # Step 3: build folders
    folders: dict[str, Folder] = {}
    for el in index.get("folder", {}).values():
        folder = _build_folder(el)
        if folder is not None:
            folders[folder.id] = folder

    # Step 4: build tags from <context> elements
    tags: dict[str, Tag] = {}
    for el in index.get("context", {}).values():
        tag = _build_tag(el)
        if tag is not None:
            tags[tag.id] = tag

    # Step 5: separate project elements from task elements
    raw_task_els: dict[str, ET.Element] = index.get("task", {})
    project_els: dict[str, ET.Element] = {}
    leaf_els: dict[str, ET.Element] = {}

    for tid, el in raw_task_els.items():
        if _is_project(el):
            project_els[tid] = el
        else:
            leaf_els[tid] = el

    tag_links = _task_tag_links(index)

    # Step 6: build projects
    projects: dict[str, Project] = {}
    for el in project_els.values():
        project = _build_project(el)
        if project is not None:
            merged_tags = _merge_tag_ids(project.tag_ids, tag_links.get(project.id))
            if merged_tags != project.tag_ids:
                project = replace(project, tag_ids=merged_tags)
            projects[project.id] = project

    # Step 7: resolve parent project ids for all leaf tasks
    project_id_map = _resolve_project_ids(raw_task_els, set(project_els.keys()))

    # Step 8: build tasks
    tasks: dict[str, Task] = {}
    for tid, el in leaf_els.items():
        project_id = project_id_map.get(tid)
        task = _build_task(el, project_id)
        if task is not None:
            merged_tags = _merge_tag_ids(task.tag_ids, tag_links.get(task.id))
            if merged_tags != task.tag_ids:
                task = replace(task, tag_ids=merged_tags)
            tasks[task.id] = task

    # Step 9: perspectives and synced settings
    perspectives: dict[str, Perspective] = {}
    for el in index.get("perspective", {}).values():
        perspective = _build_perspective(el)
        if perspective is not None:
            perspectives[perspective.id] = perspective
    settings: dict[str, Any] = {sid: _plist_of(el) for sid, el in index.get("setting", {}).items()}

    return OFModel(
        folders=folders,
        projects=projects,
        tasks=tasks,
        tags=tags,
        perspectives=perspectives,
        settings=settings,
        parser_version=PARSER_VERSION,
    )
