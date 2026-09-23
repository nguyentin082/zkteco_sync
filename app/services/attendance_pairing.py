"""Which punch of the day is the arrival, and which is the departure.

The terminal's own `status` column cannot answer that. It is written by the
mode key on the device, nobody presses the mode key, and so on the live
installation 96,267 of 96,340 stored records carry status=1 — "check-out" —
while not a single one carries status=0. Reading that column literally is what
made every row on the Attendance screen say "Check Out".

The clock is the only trustworthy signal, so it is the one used, by the rule
the payroll export has always used: within one calendar day of one person, the
earliest punch is the arrival and the latest is the departure. That rule lives
here, in one place, so the screen and the timesheet can never drift apart on
what "check-in" means.

The device's own `status` is not overwritten anywhere — it stays on the record
and is still served, because a field that is wrong on this installation may be
right on the next one, and provenance is not ours to delete.
"""

from typing import Optional

from app import config


# The labels the API emits and the UI keys its badges off. Strings, not
# numbers: the device's `status` is already a number, and a second set of
# integer codes meaning something else is exactly the confusion this module
# exists to end.
CHECK_IN = "check_in"
CHECK_OUT = "check_out"
INTERIM = "interim"
# A day whose record is half-missing. Named for what is *present* rather than
# for what is absent, because that is the part we actually know.
IN_ONLY = "in_only"
OUT_ONLY = "out_only"


def wall_clock(row):
    """The punch as the device's clock showed it, with no offset attached.

    The column type hands back a UTC-aware datetime, but these digits are the
    device's own wall-clock and the `timezone` column is what says so. tzinfo
    is dropped rather than converted: converting would move a 14:48 punch to
    some other hour, and with it the calendar day the punch is grouped under.
    """
    return row.timestamp.replace(tzinfo=None) if row.timestamp else None


def minute_of_day(value) -> int:
    """A time of day as minutes since midnight."""
    return value.hour * 60 + value.minute


def is_paired(first, last, punches: int) -> bool:
    """Whether a day holds a real span of work rather than half a record.

    Two punches in the same minute are the same broken record as one:
    somebody touched the terminal twice on their way in. There is no span
    there, so the day is not paired. This is the predicate the export scores
    by, which is why it is shared rather than restated.
    """
    return (
        punches >= 2
        and first is not None
        and last is not None
        and minute_of_day(last) > minute_of_day(first)
    )


def split_point() -> int:
    """The minute separating a lone punch's morning from its evening.

    A day with one punch says nothing about which half of the day it belongs
    to, and the midpoint of the configured shift is the least-wrong guess
    available: 08:30–17:30 puts it at 13:00, so the 08:12 punch of somebody
    who forgot to badge out reads as an arrival, and the 17:41 punch the
    terminal missed on the way in reads as a departure.

    Taken from the shift rather than from the lunch break so that a site
    working 06:00–14:00 follows along without anybody remembering to move a
    second setting.
    """
    return (
        minute_of_day(config.WORK_SHIFT_START)
        + minute_of_day(config.WORK_SHIFT_END)
    ) // 2


def day_edges(records) -> dict:
    """One entry per (person, day): which record is the arrival, which the
    departure, and how many punches there were.

    A "day" is the calendar date on the device's own clock — the digits that
    were stored, never re-zoned, exactly as the export reads them. A night
    shift crossing midnight is therefore a late arrival on one day and an
    early departure on the next, which is what the terminal itself recorded.

    Ties break on `id`, so two punches stamped on the same second still yield
    exactly one arrival and one departure rather than two rows wearing the
    same badge.

    Memory is one small entry per person per day, not per punch.
    """
    edges: dict = {}
    for record in records:
        when = wall_clock(record)
        if when is None:
            continue

        key = (record.user_id, when.date())
        rank = (when, record.id)
        edge = edges.get(key)

        if edge is None:
            edges[key] = {"first": rank, "last": rank, "punches": 1}
            continue

        edge["punches"] += 1
        if rank < edge["first"]:
            edge["first"] = rank
        if rank > edge["last"]:
            edge["last"] = rank

    return edges


def label(row, edges: dict) -> Optional[str]:
    """What to call one punch, given the edges of the day it belongs to.

    None when the row cannot be placed — no timestamp, or a day that was not
    among the ones `edges` was built from. The caller shows the device's raw
    status in that case rather than inventing a label.
    """
    when = wall_clock(row)
    if when is None:
        return None

    edge = edges.get((row.user_id, when.date()))
    if edge is None:
        return None

    first_at, _ = edge["first"]
    last_at, _ = edge["last"]

    if not is_paired(first_at, last_at, edge["punches"]):
        # One punch, or several inside one minute: the same single event
        # either way, so every row of it carries the same label.
        return IN_ONLY if minute_of_day(when) < split_point() else OUT_ONLY

    rank = (when, row.id)
    if rank == edge["first"]:
        return CHECK_IN
    if rank == edge["last"]:
        return CHECK_OUT
    return INTERIM
