# Block ① — Sync Engine Adjustments Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make calsync a true one-way mirror: full sync by default, configurable handling of attendees, conference links and private content, and edits made on the target reverted to the source on the next cycle.

**Architecture:** Three independent changes to the existing engine. (1) `rules[].window` becomes optional; without it the planner uses an all-time window. (2) Google attendees and conference links enter the unified model, and a per-rule `FieldPolicy` (like the existing alarm policy) shapes each series before hashing and rendering. Attendees are never rendered as `ATTENDEE`. (3) `render.content_digest()` reduces an iCalendar body to what a person sees. The engine compares it, for every body fetched during detection, with the digest of what it would write; a mismatch marks the copy as foreign-edited, so the planner rewrites it.

**Tech Stack:** Python 3.12, uv, httpx, icalendar 7, pytest + anyio, Radicale (e2e), ruff, pyright.

**Design source:** `docs/superpowers/specs/2026-10-24-calsync-v2-product-design.md` §3 (decisions D1, D3, D4). Base engine spec: `docs/superpowers/specs/2026-10-23-calsync-design.md` (v3).

## Global Constraints

- Branch: `feat/block1-engine`, created from `main`. Never commit to `main` directly.
- After every task: `uv run pytest -W error -q`, `uv run ruff check src tests`, `uv run ruff format --check src tests` and `uv run pyright` are all clean.
- TDD: write the failing test first, run it, see it fail for the expected reason, then implement.
- Test data is synthetic: `example.com` addresses and neutral titles. No real names, emails, calendar IDs or medical terms anywhere in code, tests or docs.
- Privacy invariant: never log event titles, descriptions, locations, attendees or URLs. Logs carry counts and IDs only.
- Invariants in `AGENTS.md` §4 stay true: one-way, ownership by resource name, local state is a rebuildable cache, deletes need evidence, protected/degraded series are never updated or deleted, request budget (`tests/e2e/test_request_budget.py` must pass unchanged in its existing assertions), isolation, error taxonomy.
- Exact values (copy verbatim):
  - `fields` defaults: `attendees = "notes"`, `conference = "copy"`, `content = "full"`.
  - Allowed values: `attendees` ∈ {`"none"`, `"notes"`}; `conference` ∈ {`"copy"`, `"none"`}; `content` ∈ {`"full"`, `"busy"`}.
  - `on_target_edit` default and only allowed value: `"revert"`.
  - Notes labels: `"Attendees: "` and `"Conference: "`. Blocks are joined with one blank line (`"\n\n"`).
  - Attendee label: `"Name <email>"` when both are present, otherwise whichever one is present.
  - Busy title: `"Busy"`.
  - `RENDER_VERSION = 2`.
  - Google params state key: `"events_params"`.
- Commits: one per task (more are fine), Conventional Commits subject, message ends with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## File Structure

| File | Change |
| --- | --- |
| `src/calsync/model.py` | `Window.everything()`; `Attendee`; `attendees` and `conference_url` on `Series` and `Occurrence`; `FieldPolicy`; `apply_field_policy()`; `RENDER_VERSION = 2` |
| `src/calsync/config.py` | `Rule.window` optional; `Rule.fields`; `Rule.on_target_edit`; parsing and validation |
| `src/calsync/planner.py` | `Plan.out_of_window` count |
| `src/calsync/sources/google_map.py` | read attendees and the conference link |
| `src/calsync/sources/google.py` | request the new fields; full resync when the request parameters change |
| `src/calsync/render.py` | `URL` property; `content_digest()` |
| `src/calsync/engine.py` | all-time window; field policy in `_desired`; revert foreign edits; `RuleResult.out_of_window` and `reverted` |
| `src/calsync/cli.py` | print `out_of_window=` and `reverted=` when non-zero |
| tests | new and updated tests listed per task |
| `README.md`, `AGENTS.md`, specs | Task 6 |

---

### Task 1: Optional window (full sync by default)

**Files:**
- Modify: `src/calsync/model.py` (class `Window`)
- Modify: `src/calsync/config.py` (`Rule`, `_rule`)
- Modify: `src/calsync/planner.py` (`Plan`, `plan`)
- Modify: `src/calsync/engine.py` (`RuleResult`, `_run_rule`)
- Modify: `src/calsync/cli.py` (`_print_cycle`)
- Test: `tests/test_model.py`, `tests/test_config.py`, `tests/test_planner.py`, `tests/test_engine.py`, `tests/test_cli_run.py`

**Interfaces:**
- Produces: `Window.everything() -> Window`; `Rule.window: WindowSpec | None` (None = all time); `Plan.out_of_window: int`; `RuleResult.out_of_window: int`.

- [ ] **Step 1: Write failing tests**

`tests/test_model.py`:

```python
def test_window_everything_intersects_any_span() -> None:
    w = Window.everything()
    assert w.intersects((datetime(1970, 1, 1, tzinfo=UTC), datetime(1970, 1, 2, tzinfo=UTC)))
    assert w.intersects((datetime(2026, 10, 1, tzinfo=UTC), MAX_TIME))
```

`tests/test_config.py` (`VALID` has a `window`; build variants with `copy.deepcopy(VALID)`):

```python
def test_window_is_optional(tmp_path: Path) -> None:
    data = copy.deepcopy(VALID)
    del data["rules"][0]["window"]
    cfg = parse_config(data, tmp_path)
    assert cfg.rules[0].window is None


def test_window_given_is_kept(tmp_path: Path) -> None:
    cfg = parse_config(copy.deepcopy(VALID), tmp_path)
    assert cfg.rules[0].window == WindowSpec(timedelta(days=7), timedelta(days=180))
```

(import `WindowSpec` from `calsync.config`.)

`tests/test_planner.py` (use the existing helpers in that file for `Desired`):

```python
def test_out_of_window_desired_are_counted() -> None:
    window = Window(datetime(2026, 10, 1, tzinfo=UTC), datetime(2026, 11, 1, tzinfo=UTC))
    inside = desired("in", span=(datetime(2026, 10, 5, tzinfo=UTC), datetime(2026, 10, 6, tzinfo=UTC)))
    outside = desired("old", span=(datetime(2026, 1, 5, tzinfo=UTC), datetime(2026, 1, 6, tzinfo=UTC)))
    p = plan({d.object_id: d for d in (inside, outside)}, {}, window)
    assert [d.object_id for d in p.create] == ["in"]
    assert p.out_of_window == 1
```

If `tests/test_planner.py` has no `desired(...)` helper with a `span` keyword, add one that builds `Desired(object_id, factories.series(object_id), "h-" + object_id, span)`.

`tests/test_engine.py` (FakeTarget harness; harness `NOW` is 2026-10-24 08:00 UTC):

```python
def far_past() -> FakeGoogle:
    fake = FakeGoogle()
    fake.add_calendar(GCAL, "Family")
    fake.put_event(GCAL, google_event("old", "Old", "2026-01-05T09:00:00+08:00", "2026-01-05T09:30:00+08:00"))
    fake.put_event(GCAL, google_event("new", "New", "2026-10-25T09:00:00+08:00", "2026-10-25T09:30:00+08:00"))
    return fake


async def test_no_window_syncs_everything(tmp_path: Path) -> None:
    h = build_fake(tmp_path, far_past(), config_for([rule()]))
    res = (await h.engine.cycle()).rules["family"]
    assert res.created == 2 and res.out_of_window == 0


async def test_window_skips_and_counts(tmp_path: Path) -> None:
    cfg = config_for([rule(window={"past": "7d", "future": "180d"})])
    h = build_fake(tmp_path, far_past(), cfg)
    res = (await h.engine.cycle()).rules["family"]
    assert res.created == 1 and res.out_of_window == 1
```

`tests/test_cli_run.py`: a run whose rule result has `out_of_window=1` prints ` out_of_window=1` on the rule line; `out_of_window=0` prints nothing extra (follow the existing pattern used for `protected=` in that file).

- [ ] **Step 2: Run tests, confirm they fail**

Run: `uv run pytest tests/test_model.py tests/test_config.py tests/test_planner.py tests/test_engine.py tests/test_cli_run.py -q`
Expected: failures for the missing `Window.everything`, `out_of_window`, and `window is None`.

- [ ] **Step 3: Implement**

`model.py`, in `class Window`:

```python
    @classmethod
    def everything(cls) -> "Window":
        """All of time: every span intersects it (full sync)."""
        return cls(datetime(1, 1, 1, tzinfo=UTC), MAX_TIME)
```

`config.py`: `Rule.window: WindowSpec | None`. In `_rule`, parse the window only when the key is present:

```python
    window: WindowSpec | None = None
    if "window" in raw:
        window_raw = _table(raw.get("window"), f"{where}.window", problems)
        past = _duration(window_raw.get("past", "7d"), f"{where}.window.past", problems)
        future = _duration(window_raw.get("future", "180d"), f"{where}.window.future", problems)
        for label, value in (("past", past), ("future", future)):
            if value is not None and value < timedelta(0):
                problems.append(f"{where}.window.{label}: must not be negative")
        if past is None or future is None:
            return None
        window = WindowSpec(past, future)
```

and construct `Rule(name, source, target, window, alarms, guard)`; drop `past`/`future` from the final `None` check.

`planner.py`: add `out_of_window: int = 0` to `Plan`; in `plan()`, replace the `continue` for non-intersecting desired with:

```python
        if not window.intersects(want.span):
            result.out_of_window += 1
            continue
```

`engine.py`: add `out_of_window: int = 0` to `RuleResult` (comment: "desired series skipped by the rule's time window"). In `_run_rule`:

```python
        window = (
            Window.everything()
            if rule.window is None
            else Window.around(self._clock(), rule.window.past, rule.window.future)
        )
        ...
        result.plan = plan(desired, actual, window, frozenset(protected))
        result.out_of_window = result.plan.out_of_window
```

`cli.py` `_print_cycle`, after the `degraded` clause:

```python
        if r.out_of_window:
            line += f" out_of_window={r.out_of_window}"
```

- [ ] **Step 4: Fix tests that relied on the old default window**

Run the whole suite: `uv run pytest -W error -q`. Any test that assumed the 7d/180d default (for example events far in the past or future that should not sync, or out-of-window deletes) now needs an explicit `window={"past": "7d", "future": "180d"}` in its `rule(...)`/config. Change the test config, never the assertion's meaning. List each changed test in the report.

- [ ] **Step 5: Run everything, then commit**

```bash
uv run pytest -W error -q && uv run ruff check src tests && uv run ruff format --check src tests && uv run pyright
git add -A && git commit -m "feat(engine): full sync by default; the time window becomes optional"
```

---

### Task 2: Read attendees and conference links from Google

**Files:**
- Modify: `src/calsync/model.py` (`Attendee`, `Series`, `Occurrence`, normalization)
- Modify: `src/calsync/sources/google_map.py`
- Modify: `src/calsync/sources/google.py`
- Test: `tests/test_model.py`, `tests/test_google_map.py`, `tests/test_google_source.py`

**Interfaces:**
- Produces: `Attendee(name: str, email: str)`; `Series.attendees: tuple[Attendee, ...] = ()`, `Series.conference_url: str = ""`; the same two fields on `Occurrence` (with defaults, appended after `alarms`); `google_map.event_attendees(event) -> tuple[Attendee, ...]`; `google_map.event_conference(event) -> str`; `google.EVENTS_PARAMS_ID: str`.

- [ ] **Step 1: Write failing tests**

`tests/test_google_map.py`:

```python
def test_attendees_are_read_and_rooms_skipped() -> None:
    event = google_event(
        "e1", "Meeting", "2026-10-25T09:00:00+08:00", "2026-10-25T10:00:00+08:00",
        attendees=[
            {"email": "alice@example.com", "displayName": "Alice"},
            {"email": "bob@example.com"},
            {"email": "room-1@resource.example.com", "displayName": "Room 1", "resource": True},
        ],
    )
    assert event_attendees(event) == (
        Attendee("Alice", "alice@example.com"),
        Attendee("", "bob@example.com"),
    )


def test_conference_prefers_video_entry_point() -> None:
    event = google_event(
        "e1", "Call", "2026-10-25T09:00:00+08:00", "2026-10-25T10:00:00+08:00",
        hangoutLink="https://meet.example.com/old",
        conferenceData={"entryPoints": [
            {"entryPointType": "phone", "uri": "tel:+1-555-0100"},
            {"entryPointType": "video", "uri": "https://meet.example.com/abc"},
        ]},
    )
    assert event_conference(event) == "https://meet.example.com/abc"


def test_conference_falls_back_to_hangout_link() -> None:
    event = google_event(
        "e1", "Call", "2026-10-25T09:00:00+08:00", "2026-10-25T10:00:00+08:00",
        hangoutLink="https://meet.example.com/xyz",
    )
    assert event_conference(event) == "https://meet.example.com/xyz"
    assert event_conference(google_event("e2", "No call", "2026-10-25T09:00:00+08:00", "2026-10-25T10:00:00+08:00")) == ""


def test_series_and_overrides_carry_attendees_and_conference() -> None:
    master = google_event(
        "s1", "Weekly", "2026-10-26T09:00:00+08:00", "2026-10-26T10:00:00+08:00",
        recurrence=["RRULE:FREQ=WEEKLY;COUNT=3"],
        attendees=[{"email": "alice@example.com", "displayName": "Alice"}],
        hangoutLink="https://meet.example.com/w",
    )
    moved = google_event(
        "s1_20261102T010000Z", "Weekly", "2026-11-02T10:00:00+08:00", "2026-11-02T11:00:00+08:00",
        recurringEventId="s1",
        originalStartTime={"dateTime": "2026-11-02T09:00:00+08:00", "timeZone": "Asia/Shanghai"},
        attendees=[{"email": "carol@example.com"}],
    )
    [s] = build_series([master, moved], CalendarMeta("Asia/Shanghai"))
    assert s.attendees == (Attendee("Alice", "alice@example.com"),)
    assert s.conference_url == "https://meet.example.com/w"
    assert s.overrides[0].attendees == (Attendee("", "carol@example.com"),)
    assert s.overrides[0].conference_url == ""
```

`tests/test_model.py`:

```python
def test_attendees_are_normalized_and_hashed() -> None:
    a = replace(series(), attendees=(Attendee("Bob", "bob@example.com"), Attendee("Alice", "alice@example.com")))
    b = replace(series(), attendees=(Attendee("Alice", "alice@example.com"), Attendee("Bob", "bob@example.com")))
    assert series_hash(a) == series_hash(b)
    assert series_hash(a) != series_hash(series())
    assert series_hash(replace(series(), conference_url="https://meet.example.com/a")) != series_hash(series())
```

`tests/test_google_source.py` (reuses `seeded()` and `make_source()` from that file; add `import copy` and `from calsync.sources.google import EVENTS_PARAMS_ID`):

```python
async def test_state_from_older_params_forces_a_full_pull() -> None:
    fake, log = seeded(), RequestLog()
    source = make_source(fake, log)
    first = await source.pull(["Family"], {})
    assert first.state["events_params"] == EVENTS_PARAMS_ID
    old = copy.deepcopy(first.state)
    del old["events_params"]  # as saved by a version that requested fewer fields
    again = await source.pull(["Family"], old)
    pull = again.calendars["Family"]
    assert pull.full and sorted(e["id"] for e in pull.upserts) == ["e1", "e2"]
    assert again.state["events_params"] == EVENTS_PARAMS_ID


async def test_matching_params_pull_incrementally() -> None:
    fake, log = seeded(), RequestLog()
    source = make_source(fake, log)
    first = await source.pull(["Family"], {})
    again = await source.pull(["Family"], first.state)
    assert not again.calendars["Family"].full
```

- [ ] **Step 2: Run, confirm failures**

Run: `uv run pytest tests/test_google_map.py tests/test_model.py tests/test_google_source.py -q`
Expected: ImportError / AttributeError for `Attendee`, `event_attendees`, `event_conference`, `EVENTS_PARAMS_ID`.

- [ ] **Step 3: Implement the model**

`model.py`:

```python
@dataclass(frozen=True, slots=True)
class Attendee:
    name: str
    email: str
```

Append to `Occurrence` (after `alarms`) and add to `Series` (after `alarms`, before `transparent`):

```python
    attendees: tuple[Attendee, ...] = ()
    conference_url: str = ""
```

In `_normalize_occurrence` and `normalize_series` add:

```python
        attendees=_normalize_attendees(<obj>.attendees),
        conference_url=<obj>.conference_url.strip(),
```

with

```python
def _normalize_attendees(attendees: tuple["Attendee", ...]) -> tuple["Attendee", ...]:
    cleaned = {Attendee(a.name.strip(), a.email.strip()) for a in attendees}
    return tuple(sorted(cleaned, key=lambda a: (a.email.lower(), a.name)))
```

`_canon` already serializes every dataclass field, so the hash covers both fields with no further change.

- [ ] **Step 4: Implement the Google mapping**

`google_map.py`:

```python
def event_attendees(event: RawEvent) -> tuple[Attendee, ...]:
    """People invited to the event; rooms and other resources are left out."""
    result: list[Attendee] = []
    for entry in event.get("attendees") or ():
        if not isinstance(entry, Mapping) or entry.get("resource"):
            continue
        name = str(entry.get("displayName") or "")
        email = str(entry.get("email") or "")
        if name or email:
            result.append(Attendee(name, email))
    return tuple(result)


def event_conference(event: RawEvent) -> str:
    """The video meeting link: the conference video entry point, else ``hangoutLink``."""
    data = event.get("conferenceData") or {}
    for entry in data.get("entryPoints") or ():
        if isinstance(entry, Mapping) and entry.get("entryPointType") == "video" and entry.get("uri"):
            return str(entry["uri"])
    return str(event.get("hangoutLink") or "")
```

Pass `attendees=event_attendees(ex), conference_url=event_conference(ex)` to each `Occurrence(...)` and `attendees=event_attendees(master), conference_url=event_conference(master)` to `Series(...)` in `_series`. Replace the `build_series` docstring sentence about attendees with: "Attendees and conference links are read into the model; the renderer never writes them as ATTENDEE or ORGANIZER, so a target server cannot send invitations on our behalf."

- [ ] **Step 5: Implement the request change and the forced resync**

`google.py`:

```python
EVENT_FIELDS = (
    "id,status,updated,summary,description,location,start,end,"
    "originalStartTime,recurringEventId,recurrence,reminders,transparency,"
    "attendees(email,displayName,resource),hangoutLink,conferenceData(entryPoints(entryPointType,uri))"
)
```

After `EVENTS_LIST_PARAMS`:

```python
# Sync tokens are only valid for the parameters they were issued with, and a mirror
# filled with fewer fields lacks data. When the parameters change, pull everything again.
EVENTS_PARAMS_ID = hashlib.sha256(json.dumps(dict(EVENTS_LIST_PARAMS), sort_keys=True).encode()).hexdigest()[:16]
```

In `pull`, right after `new_state = copy.deepcopy(dict(state))`:

```python
        if new_state.get("events_params") != EVENTS_PARAMS_ID:
            new_state["events_tokens"] = {}
            new_state["events_params"] = EVENTS_PARAMS_ID
```

(`import hashlib, json` at the top.)

- [ ] **Step 6: Run everything, then commit**

```bash
uv run pytest -W error -q && uv run ruff check src tests && uv run ruff format --check src tests && uv run pyright
git add -A && git commit -m "feat(google): read attendees and conference links; resync when request params change"
```

---

### Task 3: Field policy and the URL property

**Files:**
- Modify: `src/calsync/model.py` (`FieldPolicy`, `apply_field_policy`, `RENDER_VERSION`)
- Modify: `src/calsync/config.py` (`Rule.fields`, `_fields`)
- Modify: `src/calsync/render.py` (`URL`)
- Modify: `src/calsync/engine.py` (`_desired`)
- Test: `tests/test_model.py`, `tests/test_config.py`, `tests/test_render.py`, `tests/test_engine.py`

**Interfaces:**
- Consumes: `Attendee`, `Series.attendees`, `Series.conference_url` (Task 2).
- Produces: `FieldPolicy(attendees: Literal["none","notes"]="notes", conference: Literal["copy","none"]="copy", content: Literal["full","busy"]="full")` in `model.py`; `apply_field_policy(series: Series, policy: FieldPolicy) -> Series`; `Rule.fields: FieldPolicy` (default `FieldPolicy()`, last field of `Rule`).

- [ ] **Step 1: Write failing tests**

`tests/test_model.py`:

```python
ALICE = Attendee("Alice", "alice@example.com")
BOB = Attendee("", "bob@example.com")


def rich() -> Series:
    occ = Occurrence(
        original_start=at(2026, 10, 30, 9, 30), start=at(2026, 10, 30, 10, 0), end=at(2026, 10, 30, 10, 5),
        title="Standup moved", description="", location="Room B", alarms=(),
        attendees=(BOB,), conference_url="",
    )
    return replace(
        series(recurrence=("RRULE:FREQ=DAILY;COUNT=10",), overrides=(occ,)),
        description="Agenda", location="Room A", attendees=(ALICE, BOB),
        conference_url="https://meet.example.com/abc",
    )


def test_notes_and_copy_by_default() -> None:
    s = apply_field_policy(rich(), FieldPolicy())
    assert s.description == (
        "Agenda\n\nAttendees: Alice <alice@example.com>, bob@example.com\n\n"
        "Conference: https://meet.example.com/abc"
    )
    assert s.attendees == () and s.conference_url == "https://meet.example.com/abc"
    assert s.overrides[0].description == "Attendees: bob@example.com"
    assert s.overrides[0].attendees == ()


def test_none_drops_attendees_and_conference() -> None:
    s = apply_field_policy(rich(), FieldPolicy(attendees="none", conference="none"))
    assert s.description == "Agenda"
    assert s.attendees == () and s.conference_url == ""


def test_busy_hides_content_but_keeps_time_and_alarms() -> None:
    original = rich()
    s = apply_field_policy(original, FieldPolicy(content="busy"))
    assert (s.title, s.description, s.location, s.conference_url) == ("Busy", "", "", "")
    assert s.start == original.start and s.alarms == original.alarms
    occ = s.overrides[0]
    assert (occ.title, occ.description, occ.location) == ("Busy", "", "")
    assert occ.start == original.overrides[0].start


def test_policy_changes_the_hash() -> None:
    assert series_hash(apply_field_policy(rich(), FieldPolicy())) != series_hash(
        apply_field_policy(rich(), FieldPolicy(attendees="none"))
    )
```

`tests/test_config.py`:

```python
def test_fields_default(tmp_path: Path) -> None:
    assert parse_config(copy.deepcopy(VALID), tmp_path).rules[0].fields == FieldPolicy()


def test_fields_parsed(tmp_path: Path) -> None:
    data = copy.deepcopy(VALID)
    data["rules"][0]["fields"] = {"attendees": "none", "conference": "none", "content": "busy"}
    assert parse_config(data, tmp_path).rules[0].fields == FieldPolicy("none", "none", "busy")


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({"attendees": "invite"}, 'rules[family].fields.attendees: expected one of "none", "notes"'),
        ({"conference": True}, 'rules[family].fields.conference: expected one of "copy", "none"'),
        ({"content": "hidden"}, 'rules[family].fields.content: expected one of "full", "busy"'),
        ({"colour": "red"}, "rules[family].fields.colour: unknown setting"),
    ],
)
def test_fields_rejected(tmp_path: Path, fields: dict[str, object], expected: str) -> None:
    data = copy.deepcopy(VALID)
    data["rules"][0]["fields"] = fields
    with pytest.raises(ConfigError) as err:
        parse_config(data, tmp_path)
    assert expected in err.value.problems
```

(Check how `ConfigError` exposes problems in `tests/test_config.py` and match it; import `FieldPolicy` from `calsync.model`.)

`tests/test_render.py`:

```python
def test_url_is_rendered_and_attendees_never_are() -> None:
    s = replace(series(), conference_url="https://meet.example.com/abc",
                attendees=(Attendee("Alice", "alice@example.com"),))
    data = render(s, uid="u@calsync", rule_name="r", content_hash="h", now=NOW)
    assert b"URL:https://meet.example.com/abc" in data
    assert b"ATTENDEE" not in data and b"ORGANIZER" not in data
```

`tests/test_engine.py`:

```python
def with_attendees() -> FakeGoogle:
    fake = FakeGoogle()
    fake.add_calendar(GCAL, "Family")
    fake.put_event(GCAL, google_event(
        "m1", "Visit", "2026-10-28T10:00:00+08:00", "2026-10-28T10:30:00+08:00",
        attendees=[{"email": "alice@example.com", "displayName": "Alice"}],
        hangoutLink="https://meet.example.com/abc",
    ))
    return fake


async def test_default_fields_write_notes_not_invitations(tmp_path: Path) -> None:
    h = build_fake(tmp_path, with_attendees(), config_for([rule()]))
    await h.engine.cycle()
    [body] = h.target.managed(h.target.calendars["Family"]).values()
    text = body.decode()
    assert "Attendees: Alice <alice@example.com>" in text.replace("\r\n ", "")
    assert "URL:https://meet.example.com/abc" in text
    assert "ATTENDEE" not in text


async def test_busy_rule_hides_content(tmp_path: Path) -> None:
    h = build_fake(tmp_path, with_attendees(), config_for([rule(fields={"content": "busy"})]))
    await h.engine.cycle()
    [body] = h.target.managed(h.target.calendars["Family"]).values()
    assert b"SUMMARY:Busy" in body and b"DESCRIPTION" not in body and b"URL" not in body
```

- [ ] **Step 2: Run, confirm failures**

Run: `uv run pytest tests/test_model.py tests/test_config.py tests/test_render.py tests/test_engine.py -q`

- [ ] **Step 3: Implement**

`model.py`:

```python
RENDER_VERSION = 2

AttendeeMode = Literal["none", "notes"]
ConferenceMode = Literal["copy", "none"]
ContentMode = Literal["full", "busy"]
BUSY_TITLE = "Busy"


@dataclass(frozen=True, slots=True)
class FieldPolicy:
    attendees: AttendeeMode = "notes"
    conference: ConferenceMode = "copy"
    content: ContentMode = "full"


def _attendee_label(a: Attendee) -> str:
    return f"{a.name} <{a.email}>" if a.name and a.email else a.name or a.email


def _notes(description: str, attendees: tuple[Attendee, ...], url: str, policy: FieldPolicy) -> str:
    blocks = [description.strip()] if description.strip() else []
    if policy.attendees == "notes" and attendees:
        blocks.append("Attendees: " + ", ".join(_attendee_label(a) for a in attendees))
    if policy.conference == "copy" and url:
        blocks.append("Conference: " + url)
    return "\n\n".join(blocks)


def apply_field_policy(series: Series, policy: FieldPolicy) -> Series:
    """Shape what reaches the target. Attendees never stay structured: they are
    either written into the notes or dropped, so no server can invite anyone."""
    if policy.content == "busy":
        return replace(
            series, title=BUSY_TITLE, description="", location="", attendees=(), conference_url="",
            overrides=tuple(
                replace(o, title=BUSY_TITLE, description="", location="", attendees=(), conference_url="")
                for o in series.overrides
            ),
        )
    keep_url = policy.conference == "copy"
    return replace(
        series,
        description=_notes(series.description, series.attendees, series.conference_url, policy),
        attendees=(),
        conference_url=series.conference_url if keep_url else "",
        overrides=tuple(
            replace(
                o,
                description=_notes(o.description, o.attendees, o.conference_url, policy),
                attendees=(),
                conference_url=o.conference_url if keep_url else "",
            )
            for o in series.overrides
        ),
    )
```

`config.py`: import `FieldPolicy` from `calsync.model`; add `fields: FieldPolicy = FieldPolicy()` as the last field of `Rule`; add

```python
_FIELD_CHOICES: dict[str, tuple[str, ...]] = {
    "attendees": ("none", "notes"),
    "conference": ("copy", "none"),
    "content": ("full", "busy"),
}


def _fields(raw: Any, where: str, problems: list[str]) -> FieldPolicy:
    table = _table(raw, where, problems)
    values: dict[str, str] = {}
    for key, value in table.items():
        choices = _FIELD_CHOICES.get(key)
        if choices is None:
            problems.append(f"{where}.{key}: unknown setting")
        elif value not in choices:
            allowed = ", ".join(f'"{c}"' for c in choices)
            problems.append(f"{where}.{key}: expected one of {allowed}")
        else:
            values[key] = value
    return FieldPolicy(**values)
```

and in `_rule`: `fields = _fields(raw.get("fields"), f"{where}.fields", problems)` → `Rule(..., guard, fields)`. (`_table(None, ...)` already returns an empty mapping; verify and keep that behaviour.)

`render.py`: add a `url: str` parameter to `_event`, after `location`, with `if url: event.add("url", url)` next to the location line; pass `series.conference_url` and `occ.conference_url`.

`engine.py` `_desired`: `shaped = apply_field_policy(apply_alarm_policy(item, rule.alarms), rule.fields)`.

- [ ] **Step 4: Run everything, then commit**

```bash
uv run pytest -W error -q && uv run ruff check src tests && uv run ruff format --check src tests && uv run pyright
git add -A && git commit -m "feat(engine): configurable field policy (attendees, conference link, busy-only)"
```

---

### Task 4: `content_digest` — what a person sees in an event

**Files:**
- Modify: `src/calsync/render.py`
- Test: `tests/test_render.py`

**Interfaces:**
- Produces: `content_digest(data: bytes) -> str | None` (32 hex chars; None when the body is not a calendar with at least one VEVENT; never raises).

Background for the implementer: the engine (Task 5) compares the digest of a body fetched from the target with the digest of the body it would render. Equal digests mean "nothing a person sees differs". It must ignore anything servers and phones add on their own, or calsync would rewrite events forever. It must especially ignore alarm bookkeeping: when someone dismisses or snoozes an alert on an iPhone, the phone writes `ACKNOWLEDGED` into the VALARM, or adds a VALARM with `RELATED-TO` and an absolute `TRIGGER;VALUE=DATE-TIME`. Rewriting those away would break snooze for the person relying on the reminder.

- [ ] **Step 1: Write failing tests**

`tests/test_render.py` (use `factories.series`, `at`, `Occurrence`; `NOW` as in the file):

```python
def body(s: Series, *, now: datetime = NOW, content_hash: str = "h") -> bytes:
    return render(s, uid="u@calsync", rule_name="r", content_hash=content_hash, now=now)


def daily() -> Series:
    occ = Occurrence(
        original_start=at(2026, 10, 25, 9, 30), start=at(2026, 10, 25, 8, 0), end=at(2026, 10, 25, 8, 5),
        title="Reminder 1/4", description="", location="", alarms=(Alarm(timedelta(0)),),
    )
    return replace(series(recurrence=("RRULE:FREQ=DAILY;COUNT=21",), overrides=(occ,)), description="Note")


def test_digest_ignores_stamp_marker_and_property_order() -> None:
    a = body(daily())
    b = body(daily(), now=NOW + timedelta(days=3), content_hash="other")
    assert content_digest(a) == content_digest(b) is not None


def test_digest_ignores_server_and_phone_additions() -> None:
    original = body(daily())
    noisy = original.replace(
        b"BEGIN:VALARM\r\n",
        b"BEGIN:VALARM\r\nACKNOWLEDGED:20261025T000500Z\r\nX-WR-ALARMUID:abc\r\nUID:abc\r\n",
    ).replace(
        b"END:VEVENT\r\n",
        b"SEQUENCE:3\r\nCREATED:20261024T000000Z\r\nLAST-MODIFIED:20261024T000000Z\r\n"
        b"X-APPLE-TRAVEL-ADVISORY-BEHAVIOR:AUTOMATIC\r\n"
        b"BEGIN:VALARM\r\nACTION:DISPLAY\r\nDESCRIPTION:Reminder\r\n"
        b"TRIGGER;VALUE=DATE-TIME:20261025T001500Z\r\nRELATED-TO:abc\r\nEND:VALARM\r\n"
        b"BEGIN:VALARM\r\nACTION:NONE\r\nTRIGGER;VALUE=DATE-TIME:19760401T005545Z\r\n"
        b"X-APPLE-DEFAULT-ALARM:TRUE\r\nEND:VALARM\r\n"
        b"END:VEVENT\r\n",
    )
    assert noisy != original
    assert content_digest(noisy) == content_digest(original)


@pytest.mark.parametrize(
    "change",
    [
        lambda s: replace(s, title="Other"),
        lambda s: replace(s, start=at(2026, 10, 23, 10, 0), end=at(2026, 10, 23, 10, 5)),
        lambda s: replace(s, alarms=(Alarm(timedelta(minutes=-5)),)),
        lambda s: replace(s, alarms=()),
        lambda s: replace(s, exdates=(at(2026, 10, 27, 9, 30),)),
        lambda s: replace(s, overrides=(replace(s.overrides[0], start=at(2026, 10, 25, 7, 0)),)),
        lambda s: replace(s, conference_url="https://meet.example.com/x"),
        lambda s: replace(s, description="Changed"),
    ],
)
def test_digest_sees_what_people_see(change: Callable[[Series], Series]) -> None:
    assert content_digest(body(change(daily()))) != content_digest(body(daily()))


def test_digest_of_garbage_is_none() -> None:
    assert content_digest(b"not a calendar") is None
    assert content_digest(b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nEND:VCALENDAR\r\n") is None
```

- [ ] **Step 2: Run, confirm failures**

Run: `uv run pytest tests/test_render.py -q` → ImportError for `content_digest`.

- [ ] **Step 3: Implement**

`render.py` (imports: `hashlib`, `json`, `date` from datetime, `Any` from typing):

```python
def _when_key(value: Any) -> list[Any]:
    when = when_from_ical(value)
    return [when.utc().isoformat(), when.tzid, when.all_day]


def _dates(component: Component, name: str) -> list[list[Any]]:
    prop = component.get(name)
    if prop is None:
        return []
    props = prop if isinstance(prop, list) else [prop]
    return sorted(_when_key(item.dt) for p in props for item in p.dts)


def _alarm_keys(event: Component) -> list[list[Any]]:
    """Relative alarms only. Snoozes (RELATED-TO, absolute trigger) and client
    default alarms are a person's own bookkeeping, not content."""
    keys: list[list[Any]] = []
    for alarm in event.subcomponents:
        if alarm.name != "VALARM" or "related-to" in alarm:
            continue
        if str(alarm.get("x-apple-default-alarm", "")).upper() == "TRUE":
            continue
        trigger = alarm.get("trigger")
        if trigger is None:
            continue
        offset = alarm.decoded("trigger")
        if not isinstance(offset, timedelta):
            continue
        related = str(trigger.params.get("RELATED", "START")).upper()
        keys.append([int(offset.total_seconds()), related])
    return sorted(keys)


def _event_key(event: Component) -> dict[str, Any]:
    start = event.decoded("dtstart")
    if "dtend" in event:
        end: Any = event.decoded("dtend")
    elif "duration" in event:
        end = start + event.decoded("duration")
    else:
        end = start
    rid = event.decoded("recurrence-id") if "recurrence-id" in event else None
    return {
        "rid": None if rid is None else _when_key(rid),
        "start": _when_key(start),
        "end": _when_key(end),
        "summary": str(event.get("summary", "")).strip(),
        "description": str(event.get("description", "")).strip(),
        "location": str(event.get("location", "")).strip(),
        "url": str(event.get("url", "")).strip(),
        "transp": str(event.get("transp", "OPAQUE")).upper(),
        "rrule": sorted(_rrule_lines(event)),
        "rdate": _dates(event, "rdate"),
        "exdate": _dates(event, "exdate"),
        "alarms": _alarm_keys(event),
    }


def content_digest(data: bytes) -> str | None:
    """Digest of what a person sees in the events of a resource body.

    Ignores what servers and phones add on their own: DTSTAMP, SEQUENCE,
    CREATED/LAST-MODIFIED, X- properties, property order, VTIMEZONE text and
    alarm bookkeeping. None if the body holds no readable event. Never raises:
    the body comes from the target server and may be anything."""
    try:
        events = list(Calendar.from_ical(data).walk("VEVENT"))
        if not events:
            return None
        keys = sorted((_event_key(e) for e in events), key=lambda k: json.dumps(k["rid"]))
    except Exception:  # untrusted bytes: any parser failure means "unreadable"
        return None
    payload = json.dumps(keys, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.blake2b(payload.encode(), digest_size=16).hexdigest()
```

If a test shows that icalendar decodes a property differently than assumed (for example `rdate` structures), adjust the helper, not the test's intent, and say so in the report.

- [ ] **Step 4: Run everything, then commit**

```bash
uv run pytest -W error -q && uv run ruff check src tests && uv run ruff format --check src tests && uv run pyright
git add -A && git commit -m "feat(render): content_digest for spotting edits made on the target"
```

---

### Task 5: Revert edits made on the target

**Files:**
- Modify: `src/calsync/config.py` (`Rule.on_target_edit`)
- Modify: `src/calsync/engine.py` (`cycle`, `_detect`, `_run_rule`, `_push`, `RuleResult`)
- Modify: `src/calsync/cli.py` (`_print_cycle`)
- Test: `tests/test_config.py`, `tests/test_engine_edges.py`, `tests/test_cli_run.py`, `tests/e2e/test_request_budget.py`, a new e2e test `tests/e2e/test_target_edits.py`

**Interfaces:**
- Consumes: `content_digest` (Task 4); `apply_field_policy` in `_desired` (Task 3).
- Produces: `Rule.on_target_edit: Literal["revert"] = "revert"`; `RuleResult.reverted: int` (a subset of `updated` plus re-created deletes are counted in `created`).

Design (spec v2 §3.3, adjusted to the code): `apply_target_delta` already adopts the etag reported after a write that returned no ETag, so no extra etag fetch is needed. Every body fetched during detection is either new to the cache (cold start, lost state) or changed by someone else. For each such body whose marker hash still equals the desired hash, compare `content_digest(body)` with `content_digest(render(desired))`. If they differ, treat the copy as foreign-edited by setting its cached `content_hash` to `None`; the planner then rewrites it (PUT with `If-Match`). A managed resource deleted on the target is dropped from the cache by detection and re-created by the planner; this already works and gets a test.

- [ ] **Step 1: Write failing tests**

`tests/test_config.py`:

```python
def test_on_target_edit_default_and_rejects_others(tmp_path: Path) -> None:
    assert parse_config(copy.deepcopy(VALID), tmp_path).rules[0].on_target_edit == "revert"
    data = copy.deepcopy(VALID)
    data["rules"][0]["on_target_edit"] = "keep"
    with pytest.raises(ConfigError) as err:
        parse_config(data, tmp_path)
    assert 'rules[family].on_target_edit: only "revert" is supported' in err.value.problems
```

`tests/test_engine_edges.py`: remove the `@pytest.mark.xfail(...)` decorator and the "known limitation" header from `test_manual_target_edit_is_repaired`, update its docstring to "Someone moves an event on the phone; the client keeps X-CALSYNC-HASH. The next cycle writes the source version back.", and add after the existing asserts:

```python
    assert res.reverted == 1
    assert (await h.engine.cycle()).rules["family"].updated == 0  # converged
```

Add next to it:

```python
async def test_copy_deleted_on_target_is_recreated(tmp_path: Path) -> None:
    target = FakeTarget(mode="sync")
    h = build_fake(tmp_path, simple(), config_for([rule()]), target=target)
    await h.engine.cycle()
    cal = target.calendars["Family"]
    href = next(iter(target.managed(cal)))
    target.remove(href)
    res = (await h.engine.cycle()).rules["family"]
    assert res.created == 1 and res.deleted == 0
    assert href in target.managed(cal)


async def test_alarm_dismissed_on_phone_is_not_rewritten(tmp_path: Path) -> None:
    target = FakeTarget(mode="sync")
    h = build_fake(tmp_path, simple(), config_for([rule()]), target=target)
    await h.engine.cycle()
    cal = target.calendars["Family"]
    href, data = next(iter(target.managed(cal).items()))
    target.put(href, data.replace(b"BEGIN:VALARM\r\n", b"BEGIN:VALARM\r\nACKNOWLEDGED:20261025T000500Z\r\n"))
    res = (await h.engine.cycle()).rules["family"]
    assert res.updated == 0 and res.reverted == 0


async def test_lost_state_does_not_rewrite_unchanged_copies(tmp_path: Path) -> None:
    target = FakeTarget(mode="sync")
    h = build_fake(tmp_path, simple(), config_for([rule()]), target=target)
    await h.engine.cycle()
    (tmp_path / "state.db").unlink()
    h2 = build_fake(tmp_path, simple(), config_for([rule()]), target=target)
    res = (await h2.engine.cycle()).rules["family"]
    assert (res.created, res.updated, res.reverted) == (0, 0, 0)
```

(If the store keeps WAL side files, delete `state.db-wal`/`state.db-shm` too; check how other tests simulate lost state and reuse that.)

`tests/test_cli_run.py`: `reverted=1` is printed on the rule line when non-zero, like `out_of_window`.

New `tests/e2e/test_target_edits.py` against Radicale (mirror the setup of `tests/e2e/test_request_budget.py`):

```python
"""Edits made directly on a CalDAV server are reverted to the source."""

from collections import Counter
from pathlib import Path

import pytest

from tests.factories import google_event
from tests.fakes.google import FakeGoogle
from tests.harness import GCAL, build, config_for, rule

pytestmark = [pytest.mark.anyio, pytest.mark.e2e]


def one_event() -> FakeGoogle:
    fake = FakeGoogle()
    fake.add_calendar(GCAL, "Family")
    fake.put_event(GCAL, google_event("p1", "Reminder", "2026-10-25T08:00:00+08:00", "2026-10-25T08:05:00+08:00"))
    return fake


ONE_IDLE = Counter({"google-calendarlist": 1, "google-events": 1, "sync-collection": 1})


async def edit_on_server(h: Harness, old: bytes, new: bytes) -> str:
    """Change the one managed copy directly on Radicale, keeping our marker (like a phone)."""
    cal = await h.target.ensure_calendar("Family", create=False)
    hrefs = list(h.store.load_target_cache("dav", cal.href).objects)
    [obj] = await h.target.fetch(cal, hrefs)
    edited = obj.data.replace(old, new)
    assert edited != obj.data and b"X-CALSYNC-HASH" in edited
    await h.target.update(obj.href, obj.etag, edited)
    return obj.href


async def test_phone_style_edit_is_reverted_then_idle(tmp_path: Path, radicale_url: str, dav_user: str) -> None:
    h = build(tmp_path, radicale_url, dav_user, one_event(), config_for([rule()]))
    await h.engine.cycle()
    href = await edit_on_server(h, b"T080000", b"T083000")  # move the start by 30 minutes

    res = (await h.engine.cycle()).rules["family"]
    assert res.updated == 1 and res.reverted == 1
    cal = await h.target.ensure_calendar("Family", create=False)
    [back] = await h.target.fetch(cal, [href])
    assert b"T080000" in back.data and b"T083000" not in back.data

    mark = h.log.mark()
    res = (await h.engine.cycle()).rules["family"]
    assert res.updated == 0
    assert Counter(e.kind for e in h.log.since(mark)) == ONE_IDLE
```

(`from tests.harness import Harness` as well. `T080000` appears only in DTSTART and `T080500` in DTEND for this event, so the replace moves the start only; if `render` emits UTC `Z` times for this zone, check one rendered body and adjust the two byte patterns, keeping the intent.)

`tests/e2e/test_request_budget.py`: add

```python
async def test_cycle_after_revert_is_idle(tmp_path: Path, radicale_url: str, dav_user: str) -> None:
    h = build(tmp_path, radicale_url, dav_user, fake_with_two_calendars(), config_for([rule("family")]))
    await h.engine.cycle()
    cal = await h.target.ensure_calendar("Family", create=False)
    hrefs = sorted(h.store.load_target_cache("dav", cal.href).objects)
    [obj] = await h.target.fetch(cal, hrefs[:1])
    await h.target.update(obj.href, obj.etag, obj.data.replace(b"SUMMARY:F", b"SUMMARY:Edited F"))
    revert = (await h.engine.cycle()).rules["family"]
    assert revert.reverted == 1
    mark = h.log.mark()
    await h.engine.cycle()
    assert Counter(e.kind for e in h.log.since(mark)) == Counter(
        {"google-calendarlist": 1, "google-events": 1, "sync-collection": 1}
    )
```

- [ ] **Step 2: Run, confirm failures**

Run: `uv run pytest tests/test_config.py tests/test_engine_edges.py tests/test_cli_run.py tests/e2e -q`
Expected: the revert tests fail (`updated == 0`), `reverted` attribute missing, config error missing.

- [ ] **Step 3: Implement**

`config.py`: `Rule.on_target_edit: Literal["revert"] = "revert"` (after `fields`). In `_rule`:

```python
    on_edit = raw.get("on_target_edit", "revert")
    if on_edit != "revert":
        problems.append(f'{where}.on_target_edit: only "revert" is supported')
```

`engine.py`:

- `RuleResult`: add `reverted: int = 0  # copies edited on the target and written back (counted in updated)`.
- `Engine.__init__`: `self._fresh: dict[tuple[str, str], str | None] = {}  # (account, href) -> digest of a body read this cycle`.
- `cycle()`: `self._fresh.clear()` at the start, next to `target.reset()`.
- `_detect`, inside the `for obj in fetched:` loop, after `name` is known to be managed: `self._fresh[(account, obj.href)] = content_digest(obj.data)`.
- `_run_rule`, after `protected` is computed and before `plan(...)`:

```python
        foreign = self._foreign_edits(rule, actual, desired, protected)
        for oid in foreign:
            actual[oid] = replace(actual[oid], content_hash=None)
```

with

```python
    def _foreign_edits(
        self,
        rule: Rule,
        actual: Mapping[str, Actual],
        desired: Mapping[str, Desired],
        protected: set[str] | frozenset[str],
    ) -> set[str]:
        """Copies read this cycle whose visible content differs from what we would write
        although their marker says they are current: someone edited them on the target."""
        found: set[str] = set()
        for oid, have in actual.items():
            key = (rule.target.account, have.href)
            want = desired.get(oid)
            if key not in self._fresh or want is None or oid in protected:
                continue
            if have.content_hash != want.content_hash:
                continue  # the source changed too: a normal update follows anyway
            expected = content_digest(
                render(want.series, uid=uid_for(oid), rule_name=rule.name,
                       content_hash=want.content_hash, now=self._clock())
            )
            if self._fresh[key] != expected:
                found.add(oid)
        if found:
            log.info("rule %s: %d copy(ies) edited on the target, writing the source version back",
                     rule.name, len(found))
        return found
```

  Keep `foreign` on the instance for `_push` (for example pass it as a parameter `reverted_ids: set[str]`), and in `_push.update()` after a successful write: `if want.object_id in reverted_ids: result.reverted += 1`.
  In dry-run, still compute `foreign` (so `run --dry-run` shows the planned update) but do not write.
- `cli.py` `_print_cycle`: `if r.reverted: line += f" reverted={r.reverted}"`.

Imports: `content_digest` from `calsync.render`.

- [ ] **Step 4: Run everything, then commit**

```bash
uv run pytest -W error -q && uv run ruff check src tests && uv run ruff format --check src tests && uv run pyright
git add -A && git commit -m "feat(engine): revert edits made on the target to the source version"
```

---

### Task 6: Documentation

**Files:**
- Modify: `README.md`, `AGENTS.md`, `docs/superpowers/specs/2026-10-23-calsync-design.md` (§13), `docs/superpowers/specs/2026-10-24-calsync-v2-product-design.md` (§3.3 note)

No code; keep every statement true to the code after Tasks 1–5.

- [ ] **Step 1: README**
  - Features table: full sync by default; edits made on the target are reverted; attendees and conference links handled per `fields`.
  - New section "What calsync writes into your calendars": resource names `calsync-<rule>-<id>.ics`, `UID …@calsync`, hidden `X-CALSYNC-RULE` and `X-CALSYNC-HASH` properties (what each is for), and that calsync only ever touches resources it named.
  - Configuration reference: `window` optional ("omitted: every event, past and future"); `fields` (table of the three settings with allowed values and defaults, exactly as in Global Constraints); `on_target_edit` (only `"revert"`).
  - Quick start sample config: drop `window` from the sample rule.
  - Known limitations: remove the "edits made on the target are kept" item; add "Only pop-up reminders are synced; Google e-mail reminders are not." and "Attendees are never added as invitees; with `attendees = "notes"` they are listed in the notes."
  - CLI output: mention `out_of_window=` and `reverted=`.
- [ ] **Step 2: AGENTS.md**
  - Module map: `render.py` gains `content_digest()`; `model.py` gains `FieldPolicy` / `apply_field_policy`.
  - Section 8 "Known limitations and debt": remove the target-side edit item.
  - Gotchas: "`content_digest` must ignore phone bookkeeping (ACKNOWLEDGED, snoozed VALARMs with RELATED-TO, X-APPLE-DEFAULT-ALARM); otherwise calsync would undo snoozes."
- [ ] **Step 3: Specs**
  - v3 §13 item 6 (ETag on PUT): add "观察（2026-10-24 真实运行）：写入后的下一轮没有读取正文，说明 iCloud 的 PUT 响应带 ETag（推断，未抓包确认）。"
  - v3 §13 item 10: add "块①改为按可见内容摘要比对（`content_digest`），不依赖 `X-CALSYNC-HASH` 是否保留；真机验证见第二阶段设计 E1。"
  - v2 §3.3 step 1: add a note "实现说明：`apply_target_delta` 已在写入未返回 ETag 时采用下一次检测报告的 etag，因此没有额外的 etag 请求。"
- [ ] **Step 4: Commit**

```bash
git add -A && git commit -m "docs: full sync, field policy and target-edit revert"
```

---

### Manual acceptance (controller with the user, after the branch is merged)

Not for a subagent: it touches the user's real calendars and phone.

1. `uv tool install --reinstall --editable ~/Code/calsync` (the launchd job picks it up on its next run).
2. Before touching the real state, a throwaway-state dry run: `calsync --state-dir "$(mktemp -d)" run --dry-run` (same config and credentials, a fresh state directory, so it is a cold start that reads every managed body; the real state stays untouched). Expect no `~` (update) lines for events that were not changed in Google and have no attendees or conference link: their copies show the source version already and are adopted. A `~` line for such an event means the digest sees a difference that a person does not — stop and investigate before step 3.
3. With the user's approval, remove `window` from the local rule in `~/.config/calsync/config.toml`; `calsync config check`; `calsync run --dry-run` and review: expect a full Google resync, the state rebuilt (SCHEMA_VERSION 3, run history cleared), updates only for series whose visible content changed (attendee or conference lines, `URL`), and the previously out-of-window events created.
4. `calsync run`; then confirm the next cycle is idle (`requests` back to the idle count, `updated=0`).
5. E1 on the iPhone: move one future event's time in the synced iCloud calendar; within one cycle `reverted=1` appears in `~/Library/Logs/calsync/calsync.log` and the phone shows the source time again. Expect `reverted=1` whether or not iOS keeps `X-CALSYNC-HASH` (classification uses the hash calsync recorded, not the body's marker); the info line "read back with no calsync marker" appears only if iOS dropped `X-CALSYNC-HASH` from the body — record whether it does. A copy that still carries the marker of an older version (adopted on a cold start, not rewritten) is not reported.
6. Snooze check, twice: once on a single event and once on one instance of a recurring daily event. When the alert fires on the phone, snooze it; the following cycles must report `updated=0` and `reverted=0` for that rule (no write at all, not only no revert), and the snoozed alert must fire again. Note whether the log shows "read back with no calsync marker" (iOS dropped `X-CALSYNC-HASH`; a marker kept from an older version is not reported) — either is safe now, but record which.
7. Record E1 and the snooze result in v3 spec §13 item 10 and in `docs/ROADMAP.md`.
