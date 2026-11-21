"""Evaluate OmniFocus perspectives headlessly from the synced database.

OmniFocus stores every custom perspective in the ``.ofocus`` bundle as a
property list whose ``filterRules`` value is a JSON list of single-key rule
objects (the same vocabulary Omni Automation exposes as
``Perspective.Custom.archivedFilterRules``), for example::

    [{"actionAvailability": "remaining"}, {"actionHasAnyOfTags": ["icwIpjU7eks"]}]

combined by ``topLevelFilterAggregation`` (``all`` / ``any`` / ``none``).  This
module re-implements those rules over :class:`~omnifocus.models.OFModel` so a
perspective's contents can be listed without a Mac.

Built-in perspectives (Inbox, Flagged, Forecast, Review) are emulated from the
same task data.  Two synced built-ins (Completed, Changed) are stored as custom
perspectives by OmniFocus itself and are evaluated like any other.

Known approximations (documented for callers):

* Availability follows the OmniFocus rules for defer dates, on-hold projects and
  tags, and sequential projects/action groups.  Ordering and grouping are
  simplified to outline order (or date order for date-sorted views).
* ``actionStatus: "due"`` uses a 48 hour "due soon" window plus overdue items.
* Date specs of the ``{"dynamic": ...}`` form accept ISO dates and the words
  ``today`` / ``yesterday`` / ``tomorrow``; anything else is reported as an
  unsupported rule and ignored.
"""

from __future__ import annotations

import os
import unicodedata
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, tzinfo
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from omnifocus.errors import OFPerspectiveNotFound
from omnifocus.models import OFModel, Perspective, Project, Task
from omnifocus.review import compute_project_review_state

Item = Task | Project

DUE_SOON = timedelta(hours=48)
DEFAULT_LIMIT = 50
MAX_LIMIT = 500
NOTE_PREVIEW_CHARS = 160

# ---------------------------------------------------------------------------
# Built-in perspectives
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BuiltinPerspective:
    """A built-in OmniFocus perspective emulated by this module."""

    key: str
    name: str
    sidebar_id: str
    description: str
    aliases: tuple[str, ...] = ()


BUILTINS: tuple[BuiltinPerspective, ...] = (
    BuiltinPerspective(
        "inbox",
        "Inbox",
        "ProcessInbox.v2",
        "Remaining actions in the Inbox (not yet assigned to a project).",
        ("インボックス", "受信箱"),
    ),
    BuiltinPerspective(
        "forecast",
        "Forecast",
        "ProcessForecast.v2",
        (
            "Today's agenda: overdue actions, actions due today and actions planned "
            "for today or earlier."
        ),
        ("予測", "フォーキャスト", "today", "今日"),
    ),
    BuiltinPerspective(
        "flagged",
        "Flagged",
        "ProcessFlaggedItems.v2",
        "Flagged actions (including actions inside flagged projects or groups).",
        ("フラグ付き", "フラグ"),
    ),
    BuiltinPerspective(
        "review",
        "Review",
        "ProcessReview.v2",
        "Projects whose next review date has arrived.",
        ("レビュー",),
    ),
)

# Synced built-ins that OmniFocus stores as perspective elements.
_SYNCED_BUILTIN_ALIASES: dict[str, tuple[str, ...]] = {
    "ProcessCompleted": ("完了", "completed"),
    "ProcessRecentChanges": ("変更", "changed", "recent changes"),
}


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------


def user_timezone() -> tzinfo:
    """Return the timezone used for "today" calculations.

    ``OF_TIMEZONE`` (an IANA name such as ``Asia/Tokyo``) wins, then ``TZ``,
    then the host's local timezone.  Containers often run in UTC, so setting
    ``OF_TIMEZONE`` matters for the Forecast and date rules.
    """
    for var in ("OF_TIMEZONE", "TZ"):
        name = os.environ.get(var, "").strip()
        if name:
            try:
                return ZoneInfo(name)
            except ZoneInfoNotFoundError, ValueError:
                continue
    local = datetime.now().astimezone().tzinfo
    return local if local is not None else UTC


def _to_local_naive(value: datetime | None, tz: tzinfo) -> datetime | None:
    """Return *value* as a naive wall-clock datetime in *tz*.

    OmniFocus stores due/defer/planned dates as floating local times (naive) and
    bookkeeping timestamps (added, modified, completed) in UTC (aware).
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone(tz).replace(tzinfo=None)


def _add_months(moment: datetime, months: int) -> datetime:
    """Shift *moment* by a whole number of calendar months, clamping the day."""
    month_index = moment.month - 1 + months
    year = moment.year + month_index // 12
    month = month_index % 12 + 1
    for day in (moment.day, 30, 29, 28):
        try:
            return moment.replace(year=year, month=month, day=day)
        except ValueError:
            continue
    return moment  # pragma: no cover - day 28 always exists


def _shift(moment: datetime, amount: float, component: str) -> datetime | None:
    """Shift *moment* by ``amount`` units of *component* (may be negative)."""
    if component == "year":
        return _add_months(moment, round(amount * 12))
    if component == "month":
        return _add_months(moment, round(amount))
    unit = {"week": timedelta(weeks=1), "day": timedelta(days=1), "hour": timedelta(hours=1)}
    if component not in unit:
        return None
    return moment + unit[component] * amount


# ---------------------------------------------------------------------------
# Evaluation context
# ---------------------------------------------------------------------------


@dataclass
class _Context:
    """Precomputed relationships shared by every rule evaluation."""

    model: OFModel
    now: datetime  # naive wall clock in the user's timezone
    tz: tzinfo
    children: dict[str, list[Task]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    _available: dict[str, bool] = field(default_factory=dict)
    _blocked: dict[str, bool] = field(default_factory=dict)

    @classmethod
    def build(cls, model: OFModel, now: datetime | None = None) -> _Context:
        tz = user_timezone()
        current = _to_local_naive(now, tz) if now is not None else datetime.now(tz)
        assert current is not None
        ctx = cls(model=model, now=current.replace(tzinfo=None), tz=tz)
        for task in model.tasks.values():
            parent = task.parent_task_id or ""
            ctx.children.setdefault(parent, []).append(task)
        for siblings in ctx.children.values():
            siblings.sort(key=lambda item: (item.rank, item.name))
        return ctx

    def warn(self, message: str) -> None:
        if message not in self.warnings:
            self.warnings.append(message)

    # -- structure -----------------------------------------------------------

    def parent_task(self, task: Task) -> Task | None:
        return self.model.tasks.get(task.parent_task_id or "")

    def project_of(self, item: Item) -> Project | None:
        if isinstance(item, Project):
            return item
        return self.model.projects.get(item.project_id or "")

    def ancestors(self, task: Task) -> list[Task]:
        chain: list[Task] = []
        seen: set[str] = {task.id}
        parent = self.parent_task(task)
        while parent is not None and parent.id not in seen:
            chain.append(parent)
            seen.add(parent.id)
            parent = self.parent_task(parent)
        return chain

    def remaining_children(self, parent_id: str) -> list[Task]:
        return [child for child in self.children.get(parent_id, []) if self.is_remaining(child)]

    def folder_chain(self, folder_id: str | None) -> list[str]:
        chain: list[str] = []
        while folder_id and folder_id not in chain:
            chain.append(folder_id)
            folder = self.model.folders.get(folder_id)
            folder_id = folder.parent_folder_id if folder else None
        return chain

    # -- dates -----------------------------------------------------------------

    def effective_due(self, item: Item) -> datetime | None:
        dates = [item.due]
        if isinstance(item, Task):
            dates += [ancestor.due for ancestor in self.ancestors(item)]
            project = self.project_of(item)
            dates.append(project.due if project else None)
        present = [d for d in (_to_local_naive(d, self.tz) for d in dates) if d is not None]
        return min(present) if present else None

    def effective_defer(self, item: Item) -> datetime | None:
        dates = [item.start]
        if isinstance(item, Task):
            dates += [ancestor.start for ancestor in self.ancestors(item)]
            project = self.project_of(item)
            dates.append(project.start if project else None)
        present = [d for d in (_to_local_naive(d, self.tz) for d in dates) if d is not None]
        return max(present) if present else None

    def effective_planned(self, item: Item) -> datetime | None:
        dates = [getattr(item, "planned", None)]
        if isinstance(item, Task):
            dates += [ancestor.planned for ancestor in self.ancestors(item)]
        present = [d for d in (_to_local_naive(d, self.tz) for d in dates) if d is not None]
        return min(present) if present else None

    def effective_flagged(self, item: Item) -> bool:
        if item.flagged:
            return True
        if isinstance(item, Project):
            return False
        if any(ancestor.flagged for ancestor in self.ancestors(item)):
            return True
        project = self.project_of(item)
        return bool(project and project.flagged)

    def date_field(self, item: Item, field_name: str) -> datetime | None:
        if field_name == "due":
            return self.effective_due(item)
        if field_name == "defer":
            return self.effective_defer(item)
        if field_name == "planned":
            return self.effective_planned(item)
        if field_name == "completed":
            return _to_local_naive(item.completed, self.tz)
        if field_name == "dropped":
            return _to_local_naive(self.dropped_at(item), self.tz)
        if field_name == "added":
            return _to_local_naive(item.added, self.tz)
        if field_name == "changed":
            return _to_local_naive(item.modified, self.tz)
        self.warn(f"Unsupported date field {field_name!r} was ignored.")
        return None

    # -- status ----------------------------------------------------------------

    def folder_dropped_at(self, project: Project) -> datetime | None:
        for folder_id in self.folder_chain(project.folder_id):
            folder = self.model.folders.get(folder_id)
            if folder is not None and folder.hidden is not None:
                return folder.hidden
        return None

    def dropped_at(self, item: Item) -> datetime | None:
        if isinstance(item, Project):
            if item.status == "dropped":
                return item.modified
            return self.folder_dropped_at(item)
        if item.hidden is not None:
            return item.hidden
        for ancestor in self.ancestors(item):
            if ancestor.hidden is not None:
                return ancestor.hidden
        project = self.project_of(item)
        if project is not None:
            return self.dropped_at(project)
        return None

    def is_completed(self, item: Item) -> bool:
        if isinstance(item, Project):
            return item.status == "done" or item.completed is not None
        if item.completed is not None:
            return True
        if any(ancestor.completed is not None for ancestor in self.ancestors(item)):
            return True
        project = self.project_of(item)
        return bool(project and (project.status == "done" or project.completed is not None))

    def is_dropped(self, item: Item) -> bool:
        return self.dropped_at(item) is not None

    def is_remaining(self, item: Item) -> bool:
        return not self.is_completed(item) and not self.is_dropped(item)

    def has_on_hold_tag(self, item: Item) -> bool:
        tags = self.model.tags
        return any(tags[tag_id].on_hold for tag_id in item.tag_ids if tag_id in tags)

    def _blocked_by_sequence(self, task: Task) -> bool:
        """Return ``True`` when an earlier sibling in a sequential container comes first."""
        node: Task = task
        while True:
            parent = self.parent_task(node)
            if parent is not None:
                container_order = parent.order
                siblings = self.remaining_children(parent.id)
            else:
                project = self.project_of(node)
                if project is None or project.singleton:
                    return False
                container_order = project.order
                siblings = self.remaining_children(project.id)
            if container_order == "sequential" and siblings and siblings[0].id != node.id:
                return True
            if parent is None:
                return False
            node = parent

    def is_blocked(self, item: Item) -> bool:
        """Return ``True`` when a remaining item cannot be worked on right now."""
        if item.id in self._blocked:
            return self._blocked[item.id]
        defer = self.effective_defer(item)
        blocked = bool(defer and defer > self.now) or self.has_on_hold_tag(item)
        project = self.project_of(item)
        if project is not None and project.status != "active":
            blocked = True
        if not blocked and isinstance(item, Task):
            blocked = any(self.has_on_hold_tag(ancestor) for ancestor in self.ancestors(item))
            blocked = blocked or self._blocked_by_sequence(item)
        self._blocked[item.id] = blocked
        return blocked

    def is_available(self, item: Item) -> bool:
        if item.id in self._available:
            return self._available[item.id]
        self._available[item.id] = False  # recursion guard
        available = self.is_remaining(item) and not self.is_blocked(item)
        if available and isinstance(item, Task):
            kids = self.remaining_children(item.id)
            if kids:
                available = any(self.is_available(child) for child in kids)
        self._available[item.id] = available
        return available

    def is_first_available(self, item: Item) -> bool:
        if not isinstance(item, Task) or not self.is_available(item):
            return False
        if self.children.get(item.id) and self.remaining_children(item.id):
            return False
        if item.project_id is None:
            return True
        for candidate in self.outline(self.children.get(item.project_id, [])):
            if not self.remaining_children(candidate.id) and self.is_available(candidate):
                return candidate.id == item.id
        return False  # pragma: no cover - item itself is always reached first or later

    def outline(self, roots: Iterable[Task]) -> list[Task]:
        ordered: list[Task] = []
        stack = list(reversed(list(roots)))
        seen: set[str] = set()
        while stack:
            node = stack.pop()
            if node.id in seen:
                continue
            seen.add(node.id)
            ordered.append(node)
            stack.extend(reversed(self.children.get(node.id, [])))
        return ordered

    def is_group(self, item: Item) -> bool:
        return isinstance(item, Task) and bool(self.children.get(item.id))

    def tag_with_descendants(self, tag_ids: Iterable[str]) -> set[str]:
        wanted = set(tag_ids)
        by_parent: dict[str, list[str]] = {}
        for tag in self.model.tags.values():
            if tag.parent_tag_id:
                by_parent.setdefault(tag.parent_tag_id, []).append(tag.id)
        stack = list(wanted)
        while stack:
            for child in by_parent.get(stack.pop(), []):
                if child not in wanted:
                    wanted.add(child)
                    stack.append(child)
        return wanted

    def within_focus(self, item: Item, focus_ids: set[str]) -> bool:
        if item.id in focus_ids:
            return True
        if isinstance(item, Task):
            if any(ancestor.id in focus_ids for ancestor in self.ancestors(item)):
                return True
        project = self.project_of(item)
        if project is None:
            return False
        if project.id in focus_ids:
            return True
        return any(folder_id in focus_ids for folder_id in self.folder_chain(project.folder_id))

    def project_status_matches(self, item: Item, wanted: str) -> bool | None:
        project = self.project_of(item)
        if project is None:
            return False
        status = project.status
        if wanted == "active":
            return status == "active"
        if wanted == "onHold":
            return status == "inactive"
        if wanted == "completed":
            return status == "done"
        if wanted == "dropped":
            return status == "dropped"
        if wanted == "remaining":
            return status in ("active", "inactive")
        if wanted == "pending":
            start = _to_local_naive(project.start, self.tz)
            return status == "active" and start is not None and start > self.now
        if wanted == "stalled":
            if status != "active":
                return False
            return not any(
                self.is_available(task)
                for task in self.model.tasks.values()
                if task.project_id == project.id
            )
        self.warn(f"Unsupported project status {wanted!r} in actionHasProjectWithStatus.")
        return None


# ---------------------------------------------------------------------------
# Rule evaluation
# ---------------------------------------------------------------------------

RuleResult = bool | None  # None = rule not understood (ignored by aggregation)


def _aggregate(results: list[RuleResult], how: str) -> bool:
    known = [result for result in results if result is not None]
    if how == "any":
        return any(known) if known else True
    if how == "none":
        return not any(known)
    return all(known)


def _parse_date_spec(spec: Any, ctx: _Context) -> datetime | None:
    if not isinstance(spec, dict):
        return None
    raw = spec.get("dynamic") or spec.get("date")
    if not isinstance(raw, str):
        return None
    text = raw.strip().lower()
    today = ctx.now.replace(hour=0, minute=0, second=0, microsecond=0)
    words = {"today": 0, "yesterday": -1, "tomorrow": 1}
    if text in words:
        return today + timedelta(days=words[text])
    try:
        parsed = datetime.fromisoformat(raw.strip())
    except ValueError:
        return None
    return _to_local_naive(parsed, ctx.tz)


def _date_rule(rule: dict[str, Any], item: Item, ctx: _Context) -> RuleResult:
    field_name = str(rule.get("actionDateField", ""))
    value = ctx.date_field(item, field_name)
    today = ctx.now.date()
    if "actionDateIsToday" in rule:
        return value is not None and value.date() == today
    if "actionDateIsYesterday" in rule:
        return value is not None and value.date() == today - timedelta(days=1)
    if "actionDateIsTomorrow" in rule:
        return value is not None and value.date() == today + timedelta(days=1)
    for key, amount_key, sign in (
        ("actionDateIsInThePast", "relativeBeforeAmount", -1),
        ("actionDateIsInTheNext", "relativeAfterAmount", 1),
    ):
        if key in rule:
            spec = rule[key] if isinstance(rule[key], dict) else {}
            amount = spec.get(amount_key)
            component = str(spec.get("relativeComponent", "day"))
            if not isinstance(amount, int | float):
                ctx.warn(f"Could not read {key} amount; rule ignored.")
                return None
            bound = _shift(ctx.now, sign * amount, component)
            if bound is None:
                ctx.warn(f"Unsupported relativeComponent {component!r}; rule ignored.")
                return None
            if value is None:
                return False
            low, high = (bound, ctx.now) if sign < 0 else (ctx.now, bound)
            return low <= value <= high
    bounds: list[tuple[str, Callable[[datetime, datetime], bool]]] = [
        ("actionDateIsOnDateSpec", lambda v, d: v.date() == d.date()),
        ("actionDateIsAfterDateSpec", lambda v, d: v >= d),
        ("actionDateIsBeforeDateSpec", lambda v, d: v < d),
    ]
    matched: list[RuleResult] = []
    for key, compare in bounds:
        if key in rule:
            target = _parse_date_spec(rule[key], ctx)
            if target is None:
                ctx.warn(f"Unsupported date specification in {key}; rule ignored.")
                matched.append(None)
                continue
            matched.append(value is not None and compare(value, target))
    if matched:
        known = [m for m in matched if m is not None]
        return all(known) if known else None
    ctx.warn(f"Unsupported date rule {sorted(rule)}; rule ignored.")
    return None


def _evaluate_rule(rule: Any, item: Item, ctx: _Context) -> RuleResult:  # noqa: PLR0911, PLR0912
    """Evaluate one archived filter rule against *item*."""
    if not isinstance(rule, dict) or not rule:
        ctx.warn("Malformed perspective rule ignored.")
        return None
    if "disabledRule" in rule:
        return None
    if "aggregateRules" in rule:
        nested = rule.get("aggregateRules")
        how = str(rule.get("aggregateType", "all"))
        if not isinstance(nested, list):
            ctx.warn("Malformed aggregateRules ignored.")
            return None
        return _aggregate([_evaluate_rule(sub, item, ctx) for sub in nested], how)
    if "actionDateField" in rule:
        return _date_rule(rule, item, ctx)

    key, value = next(iter(rule.items()))
    if key == "actionAvailability":
        checks: dict[str, Callable[[Item], bool]] = {
            "available": ctx.is_available,
            "remaining": ctx.is_remaining,
            "completed": ctx.is_completed,
            "dropped": ctx.is_dropped,
            "firstAvailable": ctx.is_first_available,
            "all": lambda _item: True,
        }
        check = checks.get(str(value))
        if check is None:
            ctx.warn(f"Unsupported availability {value!r}; rule ignored.")
            return None
        return check(item)
    if key == "actionStatus":
        if value == "flagged":
            return ctx.effective_flagged(item)
        if value == "due":
            due = ctx.effective_due(item)
            return due is not None and due <= ctx.now + DUE_SOON
        ctx.warn(f"Unsupported actionStatus {value!r}; rule ignored.")
        return None
    if key == "actionHasAnyOfTags":
        wanted = ctx.tag_with_descendants(value if isinstance(value, list) else [])
        return any(tag_id in wanted for tag_id in item.tag_ids)
    if key == "actionHasAllOfTags":
        required = value if isinstance(value, list) else []
        return all(
            any(tag_id in ctx.tag_with_descendants([needed]) for tag_id in item.tag_ids)
            for needed in required
        )
    if key == "actionIsUntagged":
        return not item.tag_ids
    if key == "actionHasTagWithStatus":
        tags = [ctx.model.tags[t] for t in item.tag_ids if t in ctx.model.tags]
        status_checks: dict[str, Callable[[Any], bool]] = {
            "onHold": lambda tag: tag.on_hold and tag.hidden is None,
            "dropped": lambda tag: tag.hidden is not None,
            "active": lambda tag: not tag.on_hold and tag.hidden is None,
            "remaining": lambda tag: tag.hidden is None,
        }
        status_check = status_checks.get(str(value))
        if status_check is None:
            ctx.warn(f"Unsupported tag status {value!r}; rule ignored.")
            return None
        return any(status_check(tag) for tag in tags)
    if key == "actionWithinFocus":
        return ctx.within_focus(item, set(value if isinstance(value, list) else []))
    if key == "actionHasNoProject":
        return isinstance(item, Task) and item.project_id is None
    if key == "actionIsProject":
        return isinstance(item, Project)
    if key == "actionIsGroup":
        return ctx.is_group(item)
    if key == "actionIsProjectOrGroup":
        return isinstance(item, Project) or ctx.is_group(item)
    if key == "actionIsLeaf":
        return isinstance(item, Task) and not ctx.is_group(item)
    if key == "actionIsInSingleActionsList":
        project = ctx.project_of(item)
        return bool(project and project.singleton and isinstance(item, Task))
    if key == "actionHasProjectWithStatus":
        return ctx.project_status_matches(item, str(value))
    if key == "actionHasDueDate":
        return ctx.effective_due(item) is not None
    if key == "actionHasDeferDate":
        return ctx.effective_defer(item) is not None
    if key == "actionHasDuration":
        return getattr(item, "estimated_minutes", None) is not None
    if key == "actionWithinDuration":
        minutes = getattr(item, "estimated_minutes", None)
        return isinstance(value, int | float) and minutes is not None and minutes <= value
    if key == "actionRepeats":
        return item.repetition_rule is not None
    if key == "actionMatchingSearch":
        phrases = [str(p).casefold() for p in (value if isinstance(value, list) else [value])]
        haystack = f"{item.name}\n{item.note}".casefold()
        return all(phrase in haystack for phrase in phrases if phrase)
    ctx.warn(f"Unsupported perspective rule {key!r} ignored; results may include extra items.")
    return None


def _uses_projects(rules: Iterable[Any]) -> bool:
    """Return ``True`` when rules can match project rows (not only actions)."""
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        if "actionIsProject" in rule or "actionIsProjectOrGroup" in rule:
            return True
        nested = rule.get("aggregateRules")
        if isinstance(nested, list) and _uses_projects(nested):
            return True
    return False


# ---------------------------------------------------------------------------
# Rule descriptions (human readable, for LLM callers)
# ---------------------------------------------------------------------------


def _names(ids: Any, ctx: _Context) -> str:
    labels: list[str] = []
    for obj_id in ids if isinstance(ids, list) else []:
        obj = (
            ctx.model.tags.get(obj_id)
            or ctx.model.projects.get(obj_id)
            or ctx.model.folders.get(obj_id)
            or ctx.model.tasks.get(obj_id)
        )
        labels.append(f'"{obj.name}"' if obj is not None else f"<{obj_id}>")
    return ", ".join(labels) if labels else "(none)"


def _join(parts: list[str], how: str) -> str:
    """Join rule descriptions according to an aggregation type."""
    if how == "any":
        return "(" + " OR ".join(parts) + ")"
    if how == "none":
        return "NONE OF (" + "; ".join(parts) + ")"
    return "(" + " AND ".join(parts) + ")"


def describe_rule(rule: Any, ctx: _Context) -> str:  # noqa: PLR0911
    """Return a short English description of one rule."""
    if not isinstance(rule, dict) or not rule:
        return "malformed rule"
    if "disabledRule" in rule:
        return f"(disabled) {describe_rule(rule['disabledRule'], ctx)}"
    if "aggregateRules" in rule:
        how = str(rule.get("aggregateType", "all"))
        nested = rule.get("aggregateRules")
        parts = [describe_rule(r, ctx) for r in nested] if isinstance(nested, list) else []
        return _join(parts, how)
    if "actionDateField" in rule:
        extra = [k for k in rule if k != "actionDateField"]
        return f"{rule['actionDateField']} date {', '.join(extra) or 'rule'}"
    key, value = next(iter(rule.items()))
    if key == "actionAvailability":
        return f"availability is {value}"
    if key == "actionHasAnyOfTags":
        return f"tagged with any of {_names(value, ctx)}"
    if key == "actionHasAllOfTags":
        return f"tagged with all of {_names(value, ctx)}"
    if key == "actionWithinFocus":
        return f"inside {_names(value, ctx)}"
    if key == "actionHasNoProject":
        return "in the Inbox (no project)"
    if key == "actionStatus":
        return f"status is {value}"
    return f"{key} = {value}"


def describe_perspective(perspective: Perspective, ctx: _Context) -> str:
    """Return a one-line English summary of a custom perspective's rules."""
    if perspective.rules_error:
        return f"Rules could not be read: {perspective.rules_error}"
    if not perspective.filter_rules:
        return "No filter rules (shows everything)."
    parts = [describe_rule(rule, ctx) for rule in perspective.filter_rules]
    if perspective.aggregation == "all":
        return " AND ".join(parts)
    return _join(parts, perspective.aggregation)


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


def _iso(value: datetime | None) -> str | None:
    return value.isoformat(timespec="minutes") if value is not None else None


def item_row(item: Item, ctx: _Context) -> dict[str, Any]:
    """Return a compact, LLM-friendly row for one task or project."""
    model = ctx.model
    project = ctx.project_of(item)
    note = (item.note or "").strip()
    if len(note) > NOTE_PREVIEW_CHARS:
        note = note[:NOTE_PREVIEW_CHARS].rstrip() + "…"
    row: dict[str, Any] = {
        "id": item.id,
        "kind": "project" if isinstance(item, Project) else "task",
        "name": item.name,
        "project": project.name if project is not None and project is not item else None,
        "parent_task": None,
        "tags": [model.tags[t].name for t in item.tag_ids if t in model.tags],
        "due": _iso(ctx.effective_due(item)),
        "defer": _iso(ctx.effective_defer(item)),
        "planned": _iso(ctx.effective_planned(item)),
        "flagged": ctx.effective_flagged(item),
        "available": ctx.is_available(item),
        "completed": _iso(_to_local_naive(item.completed, ctx.tz)),
        "note": note or None,
    }
    if isinstance(item, Task):
        parent = ctx.parent_task(item)
        row["parent_task"] = parent.name if parent is not None else None
        if item.estimated_minutes is not None:
            row["estimated_minutes"] = item.estimated_minutes
    due = ctx.effective_due(item)
    row["overdue"] = bool(due and due < ctx.now and ctx.is_remaining(item))
    # Drop empty fields to keep responses short; always keep the identifying keys.
    keep = {"id", "kind", "name", "available"}
    return {k: v for k, v in row.items() if k in keep or v not in (None, [], False)}


def _outline_key_factory(ctx: _Context) -> Callable[[Item], tuple[Any, ...]]:
    model = ctx.model

    def folder_ranks(folder_id: str | None) -> list[tuple[int, str]]:
        return [
            (model.folders[f].rank, model.folders[f].name)
            for f in reversed(ctx.folder_chain(folder_id))
            if f in model.folders
        ]

    def key(item: Item) -> tuple[Any, ...]:
        project = ctx.project_of(item)
        if isinstance(item, Task) and project is None:
            inbox_path = [(a.rank, a.name) for a in reversed(ctx.ancestors(item))]
            return (0, inbox_path + [(item.rank, item.name)])
        assert project is not None
        path = folder_ranks(project.folder_id) + [(project.rank, project.name)]
        if isinstance(item, Task):
            path += [(a.rank, a.name) for a in reversed(ctx.ancestors(item))]
            path.append((item.rank, item.name))
        return (1, path)

    return key


@dataclass
class PerspectiveResult:
    """Items matched by a perspective plus metadata for the caller."""

    perspective: str
    kind: str
    description: str
    items: list[Item]
    warnings: list[str]
    sort: str = "outline"


def _sorted(items: list[Item], ctx: _Context, sort: str | None) -> tuple[list[Item], str]:
    if sort in ("completed", "modified", "added"):
        epoch = datetime.min.replace(tzinfo=UTC)

        def by_date(item: Item) -> datetime:
            value: datetime | None = getattr(item, sort, None)
            if value is None:
                return epoch
            return value if value.tzinfo else value.replace(tzinfo=UTC)

        return sorted(items, key=by_date, reverse=True), f"{sort} (newest first)"
    if sort == "due":
        far = datetime.max
        outline_key = _outline_key_factory(ctx)

        def by_due(item: Item) -> tuple[datetime, tuple[Any, ...]]:
            return (ctx.effective_due(item) or far, outline_key(item))

        return sorted(items, key=by_due), "due date"
    return sorted(items, key=_outline_key_factory(ctx)), "outline"


def evaluate_custom(perspective: Perspective, ctx: _Context) -> PerspectiveResult:
    """Return the items matched by a custom perspective."""
    if perspective.rules_error:
        ctx.warn(perspective.rules_error)
    candidates: list[Item] = list(ctx.model.tasks.values())
    if _uses_projects(perspective.filter_rules):
        candidates += list(ctx.model.projects.values())
    matched = [
        item
        for item in candidates
        if _aggregate(
            [_evaluate_rule(rule, item, ctx) for rule in perspective.filter_rules],
            perspective.aggregation,
        )
    ]
    items, sort_label = _sorted(matched, ctx, perspective.sort)
    return PerspectiveResult(
        perspective=perspective.name,
        kind="custom",
        description=describe_perspective(perspective, ctx),
        items=items,
        warnings=ctx.warnings,
        sort=sort_label,
    )


def _flagged_availability(model: OFModel) -> str:
    setting = model.settings.get("ProcessFlaggedItemsv2")
    raw = setting.get("filterIdentifier") if isinstance(setting, dict) else None
    return {"incomplete": "remaining", "remaining": "remaining", "all": "all"}.get(
        str(raw), "available"
    )


def evaluate_builtin(builtin: BuiltinPerspective, ctx: _Context) -> PerspectiveResult:
    """Return the items shown by an emulated built-in perspective."""
    model = ctx.model
    tasks = list(model.tasks.values())
    sort: str | None = None
    description = builtin.description
    if builtin.key == "inbox":
        items: list[Item] = [
            t
            for t in tasks
            if t.project_id is None
            and ctx.is_remaining(t)
            and (t.inbox or any(a.inbox for a in ctx.ancestors(t)))
        ]
    elif builtin.key == "flagged":
        availability = _flagged_availability(model)
        checks: dict[str, Callable[[Item], bool]] = {
            "remaining": ctx.is_remaining,
            "all": lambda _item: True,
        }
        check = checks.get(availability, ctx.is_available)
        items = [t for t in tasks if ctx.effective_flagged(t) and check(t)]
        items += [p for p in model.projects.values() if p.flagged and ctx.is_remaining(p)]
        description += f" Showing {availability} items."
    elif builtin.key == "forecast":
        end_of_today = datetime.combine(ctx.now.date(), datetime.max.time())
        include_deferred = model.settings.get("_ForecastIncludeDeferredItems") is True
        items = []
        for task in tasks:
            if not ctx.is_remaining(task):
                continue
            due = ctx.effective_due(task)
            planned = ctx.effective_planned(task)
            defer = _to_local_naive(task.start, ctx.tz)
            if (
                (due is not None and due <= end_of_today)
                or (planned is not None and planned <= end_of_today)
                or (include_deferred and defer is not None and defer.date() == ctx.now.date())
            ):
                items.append(task)
        sort = "due"
        description += f" Today is {ctx.now.date().isoformat()}."
    else:  # review
        items = []
        for project in model.projects.values():
            if project.status not in ("active", "inactive") or ctx.is_dropped(project):
                continue
            aware_now = ctx.now.replace(tzinfo=ctx.tz).astimezone(UTC)
            state = compute_project_review_state(project, now=aware_now)
            if state.due:
                items.append(project)
    ordered, sort_label = _sorted(items, ctx, sort)
    return PerspectiveResult(
        perspective=builtin.name,
        kind="builtin",
        description=description,
        items=ordered,
        warnings=ctx.warnings,
        sort=sort_label,
    )


# ---------------------------------------------------------------------------
# Lookup and public API
# ---------------------------------------------------------------------------


def _norm(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold().strip()


@dataclass(frozen=True)
class PerspectiveRef:
    """A resolvable perspective: either a built-in or a custom one."""

    id: str
    name: str
    kind: str  # "builtin" or "custom"
    builtin: BuiltinPerspective | None = None
    custom: Perspective | None = None

    def aliases(self) -> tuple[str, ...]:
        extra: tuple[str, ...] = ()
        if self.builtin is not None:
            extra = (self.builtin.key, *self.builtin.aliases)
        elif self.id in _SYNCED_BUILTIN_ALIASES:
            extra = _SYNCED_BUILTIN_ALIASES[self.id]
        return (self.name, self.id, *extra)


def all_perspectives(model: OFModel) -> list[PerspectiveRef]:
    """Return built-in and custom perspectives in OmniFocus sidebar order."""
    refs = [PerspectiveRef(b.sidebar_id, b.name, "builtin", builtin=b) for b in BUILTINS]
    refs += [
        PerspectiveRef(p.id, p.name, "custom", custom=p)
        for p in model.perspectives.values()
        if p.id not in {b.sidebar_id for b in BUILTINS}
    ]
    order = model.settings.get("PerspectiveOrder_v3")
    position = {pid: i for i, pid in enumerate(order)} if isinstance(order, list) else {}
    fallback = len(position)
    natural = {ref.id: i for i, ref in enumerate(refs)}
    return sorted(refs, key=lambda ref: (position.get(ref.id, fallback), natural[ref.id]))


def find_perspective(model: OFModel, query: str) -> PerspectiveRef:
    """Resolve *query* (name, id or alias; case/width-insensitive) to a perspective.

    Raises:
        OFPerspectiveNotFound: With a message listing the valid names when nothing (or
            more than one perspective) matches.
    """
    refs = all_perspectives(model)
    needle = _norm(query)
    names = ", ".join(ref.name for ref in refs)
    if not needle:
        raise OFPerspectiveNotFound(f"Perspective name is empty. Available perspectives: {names}.")
    exact = [ref for ref in refs if needle in {_norm(alias) for alias in ref.aliases()}]
    if len(exact) == 1:
        return exact[0]
    partial = [ref for ref in refs if any(needle in _norm(alias) for alias in ref.aliases())]
    if len(exact) > 1 or (not exact and len(partial) > 1):
        choices = ", ".join(ref.name for ref in (exact or partial))
        raise OFPerspectiveNotFound(
            f"Perspective name {query!r} is ambiguous. Use one of: {choices}."
        )
    if not exact and len(partial) == 1:
        return partial[0]
    raise OFPerspectiveNotFound(f"No perspective named {query!r}. Available perspectives: {names}.")


def list_perspectives(model: OFModel, *, now: datetime | None = None) -> list[dict[str, Any]]:
    """Return a summary row for every perspective (built-in and custom)."""
    ctx = _Context.build(model, now)
    rows: list[dict[str, Any]] = []
    for ref in all_perspectives(model):
        if ref.builtin is not None:
            description = ref.builtin.description
        else:
            assert ref.custom is not None
            description = describe_perspective(ref.custom, ctx)
        rows.append({"name": ref.name, "id": ref.id, "kind": ref.kind, "shows": description})
    return rows


def evaluate(model: OFModel, query: str, *, now: datetime | None = None) -> PerspectiveResult:
    """Resolve and evaluate a perspective by name, id or alias."""
    ref = find_perspective(model, query)
    ctx = _Context.build(model, now)
    if ref.builtin is not None:
        return evaluate_builtin(ref.builtin, ctx)
    assert ref.custom is not None
    return evaluate_custom(ref.custom, ctx)


def perspective_payload(
    model: OFModel,
    query: str,
    *,
    limit: int = DEFAULT_LIMIT,
    only_available: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Evaluate a perspective and return a JSON-ready payload for MCP/CLI/HTTP."""
    limit = max(1, min(int(limit), MAX_LIMIT))
    result = evaluate(model, query, now=now)
    ctx = _Context.build(model, now)
    items = result.items
    if only_available:
        items = [item for item in items if ctx.is_available(item)]
    rows = [item_row(item, ctx) for item in items[:limit]]
    payload: dict[str, Any] = {
        "perspective": result.perspective,
        "kind": result.kind,
        "shows": result.description,
        "total": len(items),
        "returned": len(rows),
        "sorted_by": result.sort,
        "items": rows,
    }
    if len(items) > limit:
        payload["truncated"] = (
            f"Only the first {limit} of {len(items)} items are shown. "
            "Call again with a larger limit to see more."
        )
    if result.warnings:
        payload["warnings"] = result.warnings
    return payload
