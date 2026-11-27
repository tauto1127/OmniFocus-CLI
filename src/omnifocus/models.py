"""Data model dataclasses for OmniFocus entities.

These are plain, immutable-friendly dataclasses produced by :mod:`omnifocus.parser`
and consumed by all other modules.  No business logic lives here — just structure.

Date conventions
----------------
- ``added`` / ``modified`` / ``completed`` are :class:`datetime.datetime` in UTC
  (they have a ``Z`` suffix in the XML).
- ``due`` / ``start`` are :class:`datetime.datetime` in *local* time without a
  timezone (OmniFocus stores them without a ``Z`` suffix).  Consumers should
  treat them as wall-clock times in the user's local timezone.
"""

from __future__ import annotations

__author__ = "Maciej Szymczak <maciej@szymczak.at>"

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

# Bumped whenever the parser starts reading data that cached models lack, so stale
# pickle caches are rebuilt instead of reused.
PARSER_VERSION = 2


class RichNote(str):
    """A note's plain text that remembers the app's original rich-text XML.

    OmniFocus stores notes as ``<note><text><p><run><lit>…`` markup. The plain text is
    what every reader sees; ``xml`` is the untouched ``<note>`` element so writers can
    send it back unchanged instead of flattening (or erasing) the formatting. Any string
    operation returns a plain :class:`str`, so an edited note is written as plain text.
    """

    xml: str

    def __new__(cls, text: str, xml: str) -> RichNote:
        obj = super().__new__(cls, text)
        obj.xml = xml
        return obj

    def __reduce__(self) -> tuple[Any, ...]:
        return (RichNote, (str(self), self.xml))


@dataclass(frozen=True)
class Folder:
    """A top-level or nested folder that groups projects.

    Attributes:
        id: Opaque OmniFocus identifier string.
        name: Display name (may contain Unicode and emoji).
        parent_folder_id: ``id`` of the parent :class:`Folder`, or ``None``
            for top-level folders.
        rank: Integer sort key used to order siblings.
        added: UTC datetime when the folder was created.
        modified: UTC datetime of the last modification.
        hidden: UTC datetime when the folder was dropped, or ``None`` if active.
            Projects inside a dropped folder (at any depth) are dropped too.
    """

    id: str
    name: str
    parent_folder_id: str | None
    rank: int
    added: datetime
    modified: datetime
    hidden: datetime | None = None


@dataclass(frozen=True)
class Tag:
    """An OmniFocus context / tag.

    Attributes:
        id: Opaque identifier.
        name: Display name.
        parent_tag_id: ``id`` of the parent tag, or ``None`` for root tags.
        rank: Integer sort key.
        added: UTC datetime when the tag was created, or ``None`` when unknown.
        modified: UTC datetime when the tag was last modified, or ``None`` when unknown.
        note: Plain-text note content.
        hidden: UTC datetime when the tag was dropped/hidden, or ``None`` if visible.
        on_hold: ``True`` when the tag is On Hold (``<prohibits-next-action>``);
            actions carrying an on-hold tag are not available.
    """

    id: str
    name: str
    parent_tag_id: str | None
    rank: int
    added: datetime | None = None
    modified: datetime | None = None
    note: str = ""
    hidden: datetime | None = None
    on_hold: bool = False


@dataclass(frozen=True)
class Project:
    """A project container (a task element with a populated ``<project>`` child).

    Attributes:
        id: Opaque identifier (same as the underlying ``<task>`` element's id).
        name: Display name.
        folder_id: ``id`` of the containing :class:`Folder`, or ``None`` for
            projects not assigned to any folder.
        status: One of ``"active"``, ``"inactive"``, ``"done"``, ``"dropped"``.
        singleton: ``True`` for single-action lists.
        rank: Integer sort key within the parent folder.
        added: UTC datetime of creation.
        modified: UTC datetime of last modification.
        flagged: Whether the project is flagged.
        due: Wall-clock due datetime (local time), or ``None``.
        start: Wall-clock defer datetime (local time), or ``None``.
        note: Plain-text note content (may be empty).
        completed: UTC datetime when the project was completed, or ``None``.
        last_review: UTC datetime when the project was last reviewed, or ``None``.
        next_review: UTC datetime when the project should next be reviewed, or ``None``.
        review_interval: Raw OmniFocus review interval token, or ``None``.
        tag_ids: Ordered list of :class:`Tag` identifiers assigned to this project.
    """

    id: str
    name: str
    folder_id: str | None
    status: str
    singleton: bool
    rank: int
    added: datetime
    modified: datetime
    flagged: bool
    due: datetime | None
    start: datetime | None
    note: str
    completed: datetime | None
    last_review: datetime | None = None
    next_review: datetime | None = None
    review_interval: str | None = None
    tag_ids: tuple[str, ...] = field(default_factory=tuple)
    repetition_rule: str | None = None
    repetition_method: str | None = None
    repetition_schedule_type: str | None = None
    repetition_anchor_date: str | None = None
    catch_up_automatically: bool = False
    next_clone_identifier: int = 0
    due_date_alarm_policy: str | None = None
    defer_date_alarm_policy: str | None = None
    latest_time_to_start_alarm_policy: str | None = None
    planned_date_alarm_policy: str | None = None
    order: str = "parallel"


@dataclass(frozen=True)
class Task:
    """A leaf or intermediate task inside a project.

    Attributes:
        id: Opaque identifier.
        name: Display name.
        parent_task_id: ``id`` of the immediate parent task or project, or
            ``None`` for inbox tasks with no explicit parent.
        project_id: ``id`` of the containing :class:`Project`, resolved by
            walking up the parent chain.  ``None`` for inbox tasks.
        inbox: ``True`` when the task lives in the OmniFocus inbox.
        completed: UTC datetime of completion, or ``None`` if still active.
        flagged: Whether the task is flagged.
        due: Wall-clock due datetime (local time), or ``None``.
        start: Wall-clock defer datetime (local time), or ``None``.
        hidden: UTC datetime when the task was dropped/hidden, or ``None`` if
            visible.  OmniFocus 4 serializes a dropped task as a ``<hidden>``
            timestamp.
        note: Plain-text note (may be empty).
        rank: Integer sort key within the parent.
        repetition_rule: RFC 5545 RRULE string, or ``None`` for non-repeating tasks.
        estimated_minutes: Estimated duration in minutes, or ``None``.
        tag_ids: Ordered list of :class:`Tag` identifiers.
        added: UTC datetime of creation.
        modified: UTC datetime of last modification.
        order: Subtask ordering: ``"sequential"`` or ``"parallel"``.
    """

    id: str
    name: str
    parent_task_id: str | None
    project_id: str | None
    inbox: bool
    completed: datetime | None
    flagged: bool
    due: datetime | None
    start: datetime | None
    hidden: datetime | None
    note: str
    rank: int
    repetition_rule: str | None
    estimated_minutes: int | None
    tag_ids: tuple[str, ...] = field(default_factory=tuple)
    added: datetime = field(default_factory=lambda: datetime.now(UTC))
    modified: datetime = field(default_factory=lambda: datetime.now(UTC))
    order: str = "parallel"
    repetition_method: str | None = None
    repetition_schedule_type: str | None = None
    repetition_anchor_date: str | None = None
    catch_up_automatically: bool = False
    next_clone_identifier: int = 0
    due_date_alarm_policy: str | None = None
    defer_date_alarm_policy: str | None = None
    latest_time_to_start_alarm_policy: str | None = None
    planned_date_alarm_policy: str | None = None
    planned: datetime | None = None


@dataclass(frozen=True)
class Perspective:
    """A custom (or synced built-in) OmniFocus perspective definition.

    Attributes:
        id: Opaque identifier (built-ins synced from OmniFocus use ids such as
            ``ProcessCompleted``).
        name: Display name shown in the OmniFocus sidebar.
        filter_rules: Decoded ``filterRules`` JSON: a list of single-key rule
            objects such as ``{"actionAvailability": "available"}``.
        aggregation: How top-level rules combine: ``"all"``, ``"any"`` or ``"none"``.
        view_mode: ``"project"`` or ``"context"`` (tag) outline, when known.
        sort: Saved sort key from the view state (e.g. ``"modified"``), when known.
        rules_error: Set when the stored rules could not be decoded.
    """

    id: str
    name: str
    filter_rules: list[Any] = field(default_factory=list)
    aggregation: str = "all"
    view_mode: str | None = None
    sort: str | None = None
    rules_error: str | None = None


@dataclass
class OFModel:
    """The fully-parsed, in-memory representation of an OmniFocus database.

    All lookup dictionaries are keyed by the entity's ``id`` field.

    Attributes:
        folders: All folders, keyed by id.
        projects: All projects, keyed by id.
        tasks: All non-project tasks, keyed by id.
        tags: All tags/contexts, keyed by id.
        perspectives: Custom perspectives synced in the database, keyed by id.
        settings: Decoded synced ``<setting>`` values keyed by setting id.
        parsed_at: UTC datetime when this model was built from the bundle.
    """

    folders: dict[str, Folder] = field(default_factory=dict)
    projects: dict[str, Project] = field(default_factory=dict)
    tasks: dict[str, Task] = field(default_factory=dict)
    tags: dict[str, Tag] = field(default_factory=dict)
    perspectives: dict[str, Perspective] = field(default_factory=dict)
    settings: dict[str, Any] = field(default_factory=dict)
    parsed_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    parser_version: int = 0

    @property
    def active_tasks(self) -> list[Task]:
        """Return all tasks that are not completed and not hidden."""
        return [t for t in self.tasks.values() if t.completed is None and t.hidden is None]

    @property
    def inbox_tasks(self) -> list[Task]:
        """Return active tasks that are in the inbox."""
        return [t for t in self.active_tasks if t.inbox]

    @property
    def active_projects(self) -> list[Project]:
        """Return projects whose effective status is 'active'."""
        return [p for p in self.projects.values() if self.effective_project_status(p) == "active"]

    def folder_dropped_at(self, folder_id: str | None) -> datetime | None:
        """Return when *folder_id* or one of its ancestors was dropped, else ``None``."""
        seen: set[str] = set()
        while folder_id and folder_id not in seen:
            seen.add(folder_id)
            folder = self.folders.get(folder_id)
            if folder is None:
                return None
            if folder.hidden is not None:
                return folder.hidden
            folder_id = folder.parent_folder_id
        return None

    def effective_project_status(self, project: Project) -> str:
        """Return the status the apps show for *project*.

        A project inside a dropped folder (at any depth) keeps its own stored status,
        but OmniFocus treats it as dropped, so active/on-hold projects there report
        ``"dropped"``.
        """
        if project.status in {"active", "inactive"} and self.folder_dropped_at(project.folder_id):
            return "dropped"
        return project.status
