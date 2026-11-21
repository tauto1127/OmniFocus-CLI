# Perspectives (headless)

OmniFocus perspectives can be listed and evaluated **without a Mac**. Custom
perspectives are synced inside the `.ofocus` database as property lists whose
`filterRules` JSON uses the same vocabulary Omni Automation exposes as
`Perspective.Custom.archivedFilterRules`. This project reads those rules and
re-implements them over the synced tasks.

## Usage

```bash
of perspectives                      # list built-in + custom perspectives
of perspective Forecast              # today + overdue (also: 予測)
of perspective "返信待ち" --format json
of perspective NextWork --available --limit 20
```

MCP tools: `list_perspectives` and `get_perspective_tasks` (`name`, `limit`,
`only_available`). Names are matched case- and width-insensitively, by id, or by
alias (`inbox`/`インボックス`, `forecast`/`予測`/`today`, `flagged`/`フラグ付き`,
`review`/`レビュー`, `completed`/`完了`). A unique partial name also works. An
unknown name returns an error that lists every valid name.

## Built-in perspectives

| Name | Emulation |
| --- | --- |
| Inbox | Remaining inbox actions and their subtasks |
| Forecast | Remaining actions due today or earlier, or planned for today or earlier (plus actions deferred until today when the synced Forecast setting asks for it) |
| Flagged | Flagged actions, including ones inside flagged projects or groups, plus flagged projects. Uses the synced Flagged filter setting (default *available*) |
| Review | Active and on-hold projects whose review date has arrived, excluding projects in dropped folders |
| Completed / Changed | Synced by OmniFocus as perspectives and evaluated like custom ones |

Projects, Tags and Nearby are navigation views, not task lists, and are not emulated.

## Supported rules

`actionAvailability` (`available`, `remaining`, `completed`, `dropped`,
`firstAvailable`), `actionStatus` (`due`, `flagged`), `actionHasAnyOfTags`,
`actionHasAllOfTags` (both include sub-tags), `actionIsUntagged`,
`actionHasTagWithStatus`, `actionWithinFocus` (folders, projects, tasks),
`actionHasNoProject`, `actionIsProject`, `actionIsGroup`,
`actionIsProjectOrGroup`, `actionIsLeaf`, `actionIsInSingleActionsList`,
`actionHasProjectWithStatus`, `actionHasDueDate`, `actionHasDeferDate`,
`actionHasDuration`, `actionWithinDuration`, `actionRepeats`,
`actionMatchingSearch`, date rules (`actionDateField` with `IsToday`,
`IsYesterday`, `IsTomorrow`, `IsInThePast`, `IsInTheNext`, `IsOnDateSpec`,
`IsAfterDateSpec`, `IsBeforeDateSpec`), nested `aggregateRules` /
`aggregateType`, `disabledRule`, and top-level `all` / `any` / `none`.

Unknown rules are ignored and reported in a `warnings` array, so results may then
include extra items.

## Availability model

An action is **remaining** when neither it, its parent action groups, its project
nor any containing folder is completed or dropped. It is **available** when it is
remaining and also:

- its effective defer date (its own or inherited) has passed,
- its project is active (not on hold),
- none of its tags or its parents' tags is on hold,
- it is first in every sequential project and action group above it,
- for an action group, at least one child is available.

Due, defer, planned and flagged values are inherited from parent groups and
projects, as in OmniFocus.

## Time zone

Due, defer and planned dates are floating local times. "Today" is computed in
`OF_TIMEZONE` (IANA name, e.g. `Asia/Tokyo`), then `TZ`, then the host zone.
**Containers usually run in UTC, so set `OF_TIMEZONE`.**

## Limitations

- Ordering is outline order (folder → project → action ranks), due-date order for
  Forecast, and newest-first for date-sorted views. Custom grouping is not reproduced.
- "Due soon" is fixed at 48 hours.
- `{"dynamic": ...}` date specs accept ISO dates and `today` / `yesterday` / `tomorrow` only.
- A perspective's saved *focus* (`useSavedFocus`) is not stored in the synced plist
  and is therefore not applied.
