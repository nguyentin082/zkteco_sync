"""Incremental attendance reads over the ZKTeco SDK.

pyzk's get_attendance() reads the whole punch log on every call. On a
terminal holding ~96k punches that is a 3.9 MB buffer: ~236 chunk reads over
UDP (2-5 minutes) or ~60 over TCP on slow firmware (10 minutes), to find the
handful of punches made since the last pull.

The terminal's log is append-only and the buffered-read protocol takes a byte
offset (CMD_PREPARE_BUFFER, then CMD_READ_BUFFER with start/size), so only the
tail needs to cross the wire. What makes that safe is the cursor: the last
real record seen at the previous pull and where it sat.

* read from a little before the anchor's old position to the end, and find
  the anchor record in what came back; everything after it is new.
* no usable anchor (first pull, or the anchor was not found) -> read the last
  few hundred records and anchor there if the database already holds the
  earliest of them, widening the window before giving up and reading all.

The caller dedupes against the database either way, so a fallback costs
time, never correctness.

Uses pyzk internals (name-mangled private methods) because pyzk offers no
ranged read. They are the same calls read_with_buffer() makes.
"""

import logging
from contextlib import contextmanager
from struct import pack, unpack

from zk import const
from zk.attendance import Attendance
from zk.exception import ZKErrorResponse

log = logging.getLogger(__name__)

_PREPARE_BUFFER = 1503  # CMD_PREPARE_BUFFER, as read_with_buffer() sends it


def _key(att):
    return {"user_id": str(att.user_id), "timestamp": att.timestamp.isoformat()}


def _is_blank(att) -> bool:
    # Some firmware (ZLM60_TFT 6.60) counts an all-zero slot at the end of
    # the log as a record. It has no PIN, so it cannot anchor anything.
    return not str(att.user_id).strip()


def _cursor(total: int, records: list, first_position: int, previous: dict) -> dict:
    """The cursor after reading ``records``, which start at 1-based log
    position ``first_position`` and run to the end of a ``total``-record log:
    the last real record read and where it sat."""
    for offset in range(len(records) - 1, -1, -1):
        if not _is_blank(records[offset]):
            return {
                "records": total,
                "anchor": first_position + offset,
                "last": _key(records[offset]),
            }
    if previous and previous.get("last"):
        # Nothing real read (only blanks): keep the old anchor.
        return {**previous, "records": total}
    return {"records": total, "anchor": None, "last": None}


def _parse(conn, data: bytes, record_size: int, users_cache: list) -> list:
    """Records in pyzk's formats (8, 16 or 40 bytes), exactly as get_attendance
    parses them."""
    decode_time = conn._ZK__decode_time
    out = []

    def users():
        if not users_cache:
            users_cache.extend(conn.get_users())
        return users_cache

    if record_size == 8:
        by_uid = {u.uid: u.user_id for u in users()}
        for i in range(0, len(data) - 7, 8):
            uid, status, ts, punch = unpack("HB4sB", data[i:i + 8])
            out.append(Attendance(by_uid.get(uid, str(uid)), decode_time(ts), status, punch, uid))
    elif record_size == 16:
        by_user_id = {u.user_id: u for u in users()}
        by_uid = {u.uid: u for u in users()}
        for i in range(0, len(data) - 15, 16):
            user_id, ts, status, punch, _reserved, _workcode = unpack("<I4sBB2sI", data[i:i + 16])
            user_id = str(user_id)
            uid = user_id
            if user_id in by_user_id:
                uid = by_user_id[user_id].uid
            elif int(user_id) in by_uid:
                uid = by_uid[int(user_id)].uid
                user_id = by_uid[int(user_id)].user_id
            out.append(Attendance(user_id, decode_time(ts), status, punch, uid))
    else:
        for i in range(0, len(data) - 39, 40):
            uid, user_id, status, ts, punch, _space = unpack("<H24sB4sB8s", data[i:i + 40])
            user_id = user_id.split(b"\x00")[0].decode(errors="ignore")
            out.append(Attendance(user_id, decode_time(ts), status, punch, uid))
    return out


def _read_range(conn, start: int, end: int) -> bytes:
    chunk = 0xFFC0 if conn.tcp else 16 * 1024
    parts = []
    while start < end:
        size = min(chunk, end - start)
        parts.append(conn._ZK__read_chunk(start, size))
        start += size
    return b"".join(parts)


# How far before its previous position the anchor record is looked for.
ANCHOR_SLACK = 64

# How far back from the end a first pull looks for a punch the database
# already holds, widening each time it finds none.
SEED_WINDOWS = (500, 4000, 32000)


def read_attendance(conn, cursor: dict = None, is_stored=None, full_read=None):
    """Read the punches added since ``cursor``.

    Returns ``(records, new_cursor, mode)``. ``records`` is everything read:
    the new punches, plus on a tail read the anchor record and any blank
    slots, which the caller's PIN filter and dedup drop. ``new_cursor`` is to
    be stored once those records are committed. ``mode`` is "tail", "seed"
    or "full".

    ``is_stored(att)`` answers whether the database already holds a punch
    (None for a record that is not a person's punch). With it, a pull with no
    usable cursor does not read the whole log: it reads the last few hundred
    records and, if the earliest person's punch among them is already stored,
    everything before it was stored by an earlier full pull. That is "seed".

    ``full_read()`` replaces ``conn.get_attendance()`` for the full read, for
    a caller that reads large buffers better over another transport.
    """
    conn.read_sizes()
    total = conn.records
    cursor = cursor or {}
    if total == 0:
        return [], _cursor(0, [], 1, None), "full"

    anchor = cursor.get("anchor")
    last = cursor.get("last")

    # No "count unchanged, so nothing new" shortcut: on the ZLM60_TFT a
    # record was seen one position lower than where the previous pull read
    # it, with the count including a blank slot some of the time. A count
    # that can move independently of the punches cannot prove there are none.
    with _prepared_log(conn, total) as read_from:
        if read_from is not None:
            if last and anchor and 0 < anchor:
                # Positions can drift a little, so look for the anchor in a
                # window before where it was rather than exactly there.
                start = max(1, min(anchor, total) - ANCHOR_SLACK)
                records = read_from(start)
                found = _find_last(records, last)
                if found is not None:
                    records = records[found:]
                    return records, _cursor(total, records, start + found, cursor), "tail"
                log.info(
                    "attendance anchor %s not found near record %d",
                    last, anchor,
                )
            if is_stored is not None:
                for window in SEED_WINDOWS:
                    if window >= total:
                        break
                    first = total - window + 1
                    records = read_from(first)
                    if _already_covered(records, is_stored):
                        return records, _cursor(total, records, first, None), "seed"
                log.info("no stored punch in the last %d records; reading it all", window)

    records = full_read() if full_read else conn.get_attendance()
    return records, _cursor(total, records, 1, None), "full"


def _find_last(records, key):
    for i in range(len(records) - 1, -1, -1):
        if _key(records[i]) == key:
            return i
    return None


def _already_covered(records, is_stored) -> bool:
    """True when the earliest person's punch in ``records`` is stored."""
    for att in records:
        stored = is_stored(att)
        if stored is not None:
            return stored
    return False


@contextmanager
def _prepared_log(conn, total: int):
    """Prepare the punch log for buffered reads once, and yield
    ``read_from(position)`` returning records ``position``..``total``
    (1-based), or None when the buffer is not laid out as expected. The
    buffer is freed on exit, before any other read may run."""
    resp = conn._ZK__send_command(
        _PREPARE_BUFFER, pack("<bhii", 1, const.CMD_ATTLOG_RRQ, 0, 0), 1024
    )
    if not resp.get("status"):
        raise ZKErrorResponse("attendance buffer not supported")
    if resp["code"] == const.CMD_DATA:
        # A small log answered inline rather than buffered: nothing to save.
        yield None
        return
    try:
        size = unpack("I", conn._ZK__data[1:5])[0]
        record_size = (size - 4) // total
        if record_size not in (8, 16, 40) or 4 + record_size * total != size:
            log.warning(
                "attendance buffer of %d bytes does not divide into %d records",
                size, total,
            )
            yield None
            return
        users_cache = []

        def read_from(position):
            start = 4 + (position - 1) * record_size
            return _parse(conn, _read_range(conn, start, size), record_size, users_cache)

        yield read_from
    finally:
        try:
            conn.free_data()
        except Exception:
            pass
