"""Incremental attendance reads over the ZKTeco SDK.

pyzk's get_attendance() reads the whole punch log on every call. On a
terminal holding ~96k punches that is a 3.9 MB buffer: ~236 chunk reads over
UDP (2-5 minutes) or ~60 over TCP on slow firmware (10 minutes), to find the
handful of punches made since the last pull.

The terminal's log is append-only and the buffered-read protocol takes a byte
offset (CMD_PREPARE_BUFFER, then CMD_READ_BUFFER with start/size), so only the
tail needs to cross the wire. What makes that safe is the cursor: the record
count and the last record seen at the previous pull.

* count unchanged, log not at capacity, and the anchor was the log's final
  record -> nothing new; no buffer is prepared.
* otherwise -> read from the anchor (the last real record seen, and its
  position) onwards, and require that record to still be where it was. If it is not (log cleared, rolled over at
  capacity, or rewritten), fall back to a full read. The caller dedupes
  against the database either way, so a fallback costs time, never
  correctness.

Uses pyzk internals (name-mangled private methods) because pyzk offers no
ranged read. They are the same calls read_with_buffer() makes.
"""

import logging
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
    position ``first_position`` and run to the end of a ``total``-record log.

    The anchor is the last real record read and where it sits. When blank
    slots follow it, "count unchanged" no longer proves "nothing new" (a
    punch may land in a slot the count already included), so `anchor_is_last`
    tells the next read whether it may skip.
    """
    for offset in range(len(records) - 1, -1, -1):
        if not _is_blank(records[offset]):
            position = first_position + offset
            return {
                "records": total,
                "anchor": position,
                "last": _key(records[offset]),
                "anchor_is_last": position == total,
            }
    if previous and previous.get("last") and previous.get("anchor", 0) <= total:
        # Nothing real read (only blanks): keep the old anchor.
        return {**previous, "records": total, "anchor_is_last": False}
    return {"records": total, "anchor": None, "last": None, "anchor_is_last": False}


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


def read_attendance(conn, cursor: dict = None):
    """Read the punches added since ``cursor``.

    Returns ``(records, new_cursor, mode)``. ``records`` is everything read:
    the new punches, plus on a tail read the anchor record and any blank
    slots, which the caller's PIN filter and dedup drop. ``new_cursor`` is to
    be stored once those records are committed. ``mode`` is "unchanged",
    "tail" or "full"; a full read is pyzk's own get_attendance().
    """
    conn.read_sizes()
    total = conn.records
    cursor = cursor or {}
    if total == 0:
        return [], _cursor(0, [], 1, None), "full"

    anchor = cursor.get("anchor")
    last = cursor.get("last")
    at_capacity = bool(conn.rec_cap) and total >= conn.rec_cap

    # A full log can drop its oldest punch for each new one without the count
    # moving, so "same count" only means "nothing new" below capacity, and
    # only when nothing (not even a blank slot) followed the anchor.
    if (
        last
        and cursor.get("records") == total
        and cursor.get("anchor_is_last")
        and not at_capacity
    ):
        return [], cursor, "unchanged"

    if last and anchor and 0 < anchor <= total:
        records = _read_tail(conn, total, anchor)
        if records and _key(records[0]) == last:
            return records, _cursor(total, records, anchor, cursor), "tail"
        log.info(
            "attendance log moved under the cursor (anchor %s not at record %d); reading it all",
            last, anchor,
        )

    records = conn.get_attendance()
    return records, _cursor(total, records, 1, None), "full"


def _read_tail(conn, total: int, anchor: int):
    """Records ``anchor``..``total`` (1-based, anchor included), or None when
    the buffer is not laid out as expected."""
    resp = conn._ZK__send_command(
        _PREPARE_BUFFER, pack("<bhii", 1, const.CMD_ATTLOG_RRQ, 0, 0), 1024
    )
    if not resp.get("status"):
        raise ZKErrorResponse("attendance buffer not supported")
    if resp["code"] == const.CMD_DATA:
        # A small log answered inline rather than buffered: nothing to save.
        return None
    try:
        size = unpack("I", conn._ZK__data[1:5])[0]
        record_size = (size - 4) // total
        if record_size not in (8, 16, 40) or 4 + record_size * total != size:
            log.warning(
                "attendance buffer of %d bytes does not divide into %d records",
                size, total,
            )
            return None
        start = 4 + (anchor - 1) * record_size
        return _parse(conn, _read_range(conn, start, size), record_size, [])
    finally:
        try:
            conn.free_data()
        except Exception:
            pass
