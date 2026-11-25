# Tool reference

Every answer is one compact JSON object (also delivered as `structuredContent`). Anything that carries event text
has `"untrusted": true, "source": "caldav"`. Failures are real tool errors (`isError`), with a message that says how
to correct the call.

## Conventions

**Dates in requests (reads).** `start` and `end` accept:

| Form | Meaning |
|---|---|
| `2026-11-28` | that whole day. As an `end`, **inclusive**: `start=2026-11-28, end=2026-11-29` covers both days. |
| `2026-11-28T15:00` | an instant in the server's zone (offsets such as `+01:00` or `Z` are converted). As an `end`, exclusive. |
| `now`, `today`, `tomorrow`, `yesterday` | |
| `this_week`, `next_week`, `last_week` | The week starts on `CALDAV_WEEK_START` (default Monday). |
| `this_weekend`, `next_weekend` | Saturday and Sunday. On a weekend, `this_weekend` is the current one. |
| `this_month`, `next_month`, `last_month` | Calendar months. |
| `next_N_days`, `last_N_days` | From today (or ending today), N up to 366. |
| `monday` … `sunday`, `next_monday` … | The next such day, today included; `next_` is strictly after today. |

With no `end`, a date or keyword covers just its own day or period. A date-time start covers a day (agenda), a year
(search) or an hour (free/busy). Reversed, empty or over-long ranges are errors. Day boundaries follow the local
calendar, so a day with a daylight-saving change is 23 or 25 hours long.

**Dates in requests (writes)** are exact ISO values only. `tomorrow` is refused, naming today, because a vague
value that became an all-day event would be a silent wrong answer.

**Events in answers.**

| Field | |
|---|---|
| `uid`, `summary`, `calendar` | |
| `start`, `end`, `day` | Timed: local time with offset (`2026-11-26T10:00+01:00`). `day` is the weekday of `start`. |
| `all_day: true` | `start`/`end` are dates and `end` is the **last day, inclusive**. |
| `floating: true` | The event has no zone of its own; it was read as server local time. |
| `event_tz` | Only when the event's own zone differs from the server's. |
| `location` | When present. |
| `status` | Only when not CONFIRMED (`TENTATIVE`; cancelled events are hidden and counted in `notes`). |
| `free: true` | Marked as not blocking time (TRANSP:TRANSPARENT). |
| `recurring`, `recurrence_id` | Occurrences of a repeating event. **`recurrence_id` is what `instance` takes.** |
| `flags` | Text that looks like an attempt to instruct you (`instruction-override`, `calendar-command`, ...). |

More, on request, via `include`: `description`, `attendees`, `organizer`, `categories`, `reminders`, `rrule`,
`etag`, `url` (or `all`). Titles, locations and descriptions are cleaned and capped (400 and 2,000 characters).

**Answers say what they did.** Every list has `range` (resolved start, end, zone and a labelled sentence), `now`,
`calendars` searched, `total`, `count`, and `notes` for: truncation, hidden cancelled events, unreadable events,
calendars that failed (`INCOMPLETE`, with `errors`), and why nothing matched. `search.path` states where matching
happened. Answers are capped at 60,000 bytes by dropping whole events, with a note.

---

## caldav_list_calendars

No parameters. Returns `now`, `today`, `tz`, `week_starts_on`, `default_calendar` and `calendars: [{id, name}]`.
Call it first when the calendar or the date is unclear.

## caldav_get_events

`start="today"`, `end`, `calendars` (names or ids; default **all**), `limit`, `include`, `order="asc"|"desc"`.

```
caldav_get_events(start="tomorrow", calendars=["Personal"])
→ {"untrusted":true,"source":"caldav","now":"2026-11-25T15:30+01:00",
   "range":{"start":"2026-11-26T00:00:00+01:00","end":"2026-11-27T00:00:00+01:00","tz":"Europe/Berlin",
            "label":"Thu 2026-11-26 00:00 to Fri 2026-11-27 00:00 (Europe/Berlin), end exclusive"},
   "calendars":["Personal"],"total":1,"count":1,
   "events":[{"uid":"dentist","summary":"Dentist","start":"2026-11-26T10:00+01:00","end":"2026-11-26T11:00+01:00",
              "day":"Thu","calendar":"Personal","location":"Praxis Dr. Roth"}],
   "search":{"path":"server (time range only)"}}
```

An empty list is explained: `"notes":["No events in this range in 'Personal'."]`.

## caldav_search_events

`query`, `fields`, `where`, `start="now"`, `end`, `calendars`, `limit`, `include`, `order`.
At least one of `query` / `where` is required (to list a period, use `caldav_get_events`).

* `query`: words that must **all** appear, each in any of `summary, location, description, categories, attendee`
  (attendee = name or address). Case-insensitive, Unicode-folded. `fields` restricts which.
* `where`: `{"all": [...], "any": [...], "none": [...]}` of `{field, op, value}`; fields `summary, location,
  description, categories, attendee, organizer, status`; ops `contains, equals, starts_with, ends_with, exists,
  missing` (`exists`/`missing` take no value). `none` means *no* value matches.
* Default window: now to one year ahead (so results are upcoming). Past events need an explicit range.
  "Next dentist appointment" is `query="dentist", limit=1`; "when did I last see Anna" is `query="anna",
  start="2026-01-01", end="yesterday", order="desc", limit=1`.

**Where matching happens.** The time range is always a server query. Text conditions are pushed to the server
only when it will provably agree: `contains` on summary, location, description or categories at the top level of an
AND, with an ASCII term and no `\ , ;` (RFC 4791 folds ASCII case only, so `Zahnärztin` is never trusted to the
server). Everything is then re-checked here, so the answer is exact either way. `search.path` is one of:

* `server (time range and text), re-checked locally`
* `server (time range and part of the text), finished locally`
* `server (time range), text matched locally`

and `search.examined` is how many events were looked at. Zero results with a filter says whether the range held
events that did not match, or held none.

## caldav_get_event_by_uid

`uid`, `calendar`. The event in full (`description`, `attendees`, `organizer`, `categories`, `reminders`, `rrule`,
`etag`, `url`). For a repeating event also `series: {rrule, excluded: [...], overrides: [{recurrence_id, start, end,
summary}]}`. The text block is fenced and datamarked; a warning precedes it if the text looks hostile.

## caldav_get_freebusy

`start="today"`, `end`, `calendars`, `min_free_minutes=15`. A date-time `start` alone checks one hour.

```
caldav_get_freebusy(start="2026-11-26T09:00", end="2026-11-26T12:00")
→ {"available":false,
   "busy":[{"start":"2026-11-26T10:00+01:00","end":"2026-11-26T11:00+01:00","events":[{"uid":"dentist","summary":"Dentist",...}]}],
   "free":[{"start":"2026-11-26T09:00+01:00","end":"2026-11-26T10:00+01:00"},{"start":"2026-11-26T11:00+01:00","end":"2026-11-26T12:00+01:00"}],
   "search":{"path":"computed from the events in the range (time-range query on the server)"}, ...}
```

Overlapping and touching events merge into one block that lists what causes it. Cancelled and `free`-marked
events never block. All-day events are listed in `all_day_events` and do not block. Tentative events block and say so.
Computed from the expanded events (so it can name the conflict) rather than the server's `free-busy-query`, which
returns periods without titles; `caldav-mcp-probe` reports whether the server offers it.

## caldav_create_event

`summary`, `start` (required), `end` | `duration_minutes`, `calendar`, `location`, `description`, `categories`,
`reminders_minutes_before`, `rrule`, `check_conflicts=true`.

* Date-time `start`: a timed event of one hour unless `end`/`duration_minutes`. Date-only `start`: an all-day event;
  a date-only `end` is its last day.
* `rrule` is validated (`FREQ` DAILY/WEEKLY/MONTHLY/YEARLY, `INTERVAL, COUNT, UNTIL, BYDAY, BYMONTHDAY, BYMONTH,
  BYSETPOS, BYYEARDAY, BYWEEKNO, WKST`; `COUNT` up to 1000).
* `calendar` defaults to `CALDAV_DEFAULT_CALENDAR`, else the only calendar, else the call is refused with the list.
* Returns `{"created": true, "event": {... "etag": ...}, "conflicts": [...], "notes": [...]}`; the event is read back
  from the server. `conflicts` are timed events in any calendar that overlap (never blocking).
* A second identical create (same title, start and end) returns `{"created": false, "reason": ..., "event": ...}`
  and writes nothing, so a retry after a timeout cannot duplicate.

## caldav_update_event

`uid`, `scope`, `instance`, `etag`, `calendar`, then the changes: `summary`, `start`, `end`, `duration_minutes`,
`location`, `description`, `categories`, `reminders_minutes_before`, `rrule`, `clear`.

* **Only the fields given change.** The whole stored event is read, edited and written back; overrides, alarms,
  attendees and properties this server does not know survive.
* Moving `start` keeps the length; `end` alone keeps the start; `duration_minutes` resizes.
  A date-only `start` turns the event all-day, a date-time `start` turns it back.
* `clear: ["location", "description", "categories", "reminders", "rrule"]` removes fields. An empty string is
  refused (it would be ambiguous); setting and clearing the same field is refused; an update that changes nothing
  is an error.
* **`scope` is required for a repeating event** (and an error for a one-off):

  | `scope` | Changes | `instance` |
  |---|---|---|
  | `series` | every occurrence. Exceptions already made keep pointing at their occurrences if `start` moves. | not needed |
  | `instance` | one occurrence (creates or edits its exception). | required |
  | `this_and_future` | that occurrence and all later ones: the old series ends just before it and a **new series** (new uid, returned) starts there; later exceptions travel with it. | required |

  `instance` is an occurrence's `recurrence_id` as `caldav_get_events` shows it; a plain date works when exactly one
  occurrence starts that day. A wrong one is refused with the nearest real ones listed. `this_and_future` from the
  first occurrence is just `series`, and says so.
* **Conditional write.** The ETag read in this very call is sent as `If-Match`; if the event changed meanwhile the
  server refuses and nothing is written. `etag` (from `caldav_get_event_by_uid`) additionally refuses a write based
  on a stale read. A server that returns no ETags gets an unconditional write and the answer's `notes` say so.

```
caldav_update_event(uid="standup", summary="Daily sync")
→ ERROR: This event repeats (FREQ=WEEKLY;COUNT=10), so say what to change: scope='series' (every occurrence),
  scope='instance' (one occurrence; pass `instance`, its recurrence_id from caldav_get_events), or
  scope='this_and_future' (that occurrence and every later one; pass `instance`).
```

## caldav_delete_event

`uid`, `scope`, `instance`, `etag`, `calendar`. Same scopes and rules as update. `instance` cancels one occurrence
(an EXDATE; its exception is removed too); `this_and_future` ends the series before it (deleting the whole series
if it is the first occurrence). The answer describes exactly the occurrence removed. Not undoable.

```
caldav_delete_event(uid="standup", scope="instance", instance="2026-11-30")
→ {"deleted":true,"scope":"instance","event":{"uid":"standup","summary":"Standup","start":"2026-11-30T10:00+01:00",
   "end":"2026-11-30T10:30+01:00","recurrence_id":"2026-11-30T10:00+01:00",...},"notes":[]}
```

## caldav_move_event

`uid`, `target_calendar`, `source_calendar`. Moves the event, with its whole series and exceptions, **keeping its
uid**. Uses WebDAV `MOVE`; if the server lacks it, copies then deletes, and if the delete fails removes the copy
again (and says if it could not). Refused when the uid already exists in the target.

## caldav_add_attendee, caldav_remove_attendee

`uid`, `email`, plus for add `name`, `role`; `scope="series"` for a repeating event (attendees apply to all
occurrences), `etag`, `calendar`. Addresses are validated; adding someone already present changes nothing; removing
someone absent lists who is there. **A server with scheduling may email an invitation when an attendee is added**
(`caldav-mcp-probe` reports `scheduling_advertised`); leave these tools off unless you want that.
