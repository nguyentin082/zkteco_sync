"""Reading a ZKTime .NET backup file, and putting what it holds back into this app.

ZKTime's own Backup button writes a whole SQLite database — 82 tables, schema
straight out of the .NET app's Hibernate mappings. This module reads one and
restores the three things this app has its own home for: people, punches and
fingerprint templates. Everything else in the file (payroll, report layouts,
ZKTime's own menus, users and privileges) belongs to ZKTime and is left alone,
not silently half-imported.

Why this exists at all
----------------------
An `acc` terminal's transaction table cannot be queried by the server — see
``provisioning.NO_ATTENDANCE_QUERY`` and the 501 on
``POST /devices/{sn}/pull/attendance``. Punches arrive by push, as they
happen, and there is no "fetch me the last three years". So for history that
predates this server, a ZKTime backup is not a convenience: it is the only
copy that exists. That is the whole justification for reading a foreign
database format here.

The shape of the file, as confirmed against the operator's own
-------------------------------------------------------------
Read off ``bak_zktime_20261806.db`` (19 MB, 2026-06-18) rather than assumed:

* ``hr_employee`` — one row per person. ``emp_pin`` is the PIN the terminal
  and this app both key on. The name lives entirely in ``emp_firstname``;
  ``emp_lastname`` is ``''`` for all 52 rows, so the two are joined with a
  space only when the second is actually non-empty.
* ``att_punches`` — the raw punches. **``employee_id`` is a foreign key to
  ``hr_employee.id``, not a PIN.** Reading it as a PIN is the one mistake that
  would import 64,132 rows attributed to the wrong people, so the PIN is
  always reached through the join and never inferred.
* ``att_terminal`` — the terminals. The serial is ``terminal_sns`` (TEXT);
  ``terminal_sn`` (BIGINT) is 0 and is not the serial.
* ``hr_biotemplate`` — fingerprint templates, ``bio_type=1``, ``version``
  '10.0', ``template_data`` base64. ``size`` is 0 on every row in the file and
  is therefore never trusted or validated against.

Deliberately not imported: ``att_day_summary`` (167,420 rows) and
``att_day_details``. Both are ZKTime's *derived* attendance — its shift engine's
opinion about the punches, computed under rules this app does not share and
cannot reproduce. Importing them would present ZKTime's arithmetic as if this
app had done it. The punches they were derived from are imported instead, and
this app derives its own.

What a restore is allowed to do
-------------------------------
Add rows and fill in blanks. Never delete, never overwrite a value an operator
typed. People go through ``employee_sync.upsert_employee``, the single writer,
under exactly the rule every other source obeys: a source may fill a field in
but never empty one out. Punches are inserted only where the
``(device_sn, user_id, timestamp)`` unique key says there is nothing there
already, which is what makes running a restore twice a no-op rather than a
duplicate.
"""

import base64
import binascii
import logging
import os
import sqlite3
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import config
from app.errors import fragment
from app.models import AttendanceLog, BiometricTemplate, Device, Employee
from app.services import employee_sync
from app.services.punch_filter import is_person_pin

log = logging.getLogger(__name__)

# Every SQLite file starts with this. Checked before handing the path to
# sqlite3 so that "you uploaded something that is not a database" is an
# answerable error rather than a DatabaseError from a driver.
_SQLITE_MAGIC = b"SQLite format 3\x00"

# Without these there is nothing to restore and the file is not a ZKTime
# backup, whatever its extension says.
_REQUIRED_TABLES = ("hr_employee", "att_punches", "att_terminal")

# The parts an operator can ask for, in the order they must run. People first:
# `roster_only` on the attendance screen hides punches whose PIN is not on the
# roster, so attendance restored without its people would land in the database
# and then be invisible on the page that was supposed to show it.
PARTS = ("employees", "attendance", "templates")

# How ZKTime spells a timestamp. The first is what the operator's file uses
# throughout; the others are the spellings other builds of ZKTime and other
# SQLite writers produce for the same column.
_TIME_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M:%S.%f",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M:%S.%f",
    "%Y-%m-%d %H:%M",
)

# Punches are inserted in batches of this many. 64k rows in one flush is a
# large transaction and a large amount of held memory; one row at a time is
# 64k round trips. This is neither.
_BATCH = 1000


class ZKTimeBackupError(Exception):
    """The file cannot be read as a ZKTime backup, and why.

    ``code`` and ``params`` are what the UI translates (see app/errors.py);
    the message stays the English sentence for logs and API callers.
    """

    def __init__(self, message: str, code: str = "", **params):
        super().__init__(message)
        self.code = code
        self.params = params


# ---------------------------------------------------------------------------
# Opening and validating
# ---------------------------------------------------------------------------

def open_backup(path: str) -> sqlite3.Connection:
    """Open a backup read-only, or raise ``ZKTimeBackupError`` saying why not.

    Read-only is a URI flag rather than a convention: this module must not be
    able to write to the operator's backup even by mistake, and a restore that
    modified its own source would destroy the one copy of the history it was
    reading.
    """
    if not os.path.isfile(path):
        raise ZKTimeBackupError(
            "The uploaded file is no longer available. Upload it again.",
            code="backup.file_missing",
        )

    with open(path, "rb") as handle:
        if handle.read(len(_SQLITE_MAGIC)) != _SQLITE_MAGIC:
            raise ZKTimeBackupError(
                "This is not a SQLite database. A ZKTime backup is the .db file "
                "ZKTime's own Backup button writes — not a .zip, .bak or .sql export.",
                code="backup.not_sqlite",
            )

    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise ZKTimeBackupError(
            f"The database could not be opened: {exc}",
            code="backup.open_failed", error=str(exc),
        ) from exc

    conn.row_factory = sqlite3.Row

    # sqlite3.connect() is lazy — it does not touch the file, so a database
    # that is corrupt or truncated connects cleanly and fails on the first
    # query instead. That makes this read, not the connect above, the point
    # where "the header says SQLite but the rest does not" surfaces. Without
    # catching it here an operator uploading a half-copied backup gets a 500
    # and a traceback rather than a sentence telling them the file is damaged.
    try:
        present = {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    except sqlite3.DatabaseError as exc:
        conn.close()
        raise ZKTimeBackupError(
            f"The file starts like a SQLite database but cannot be read as one "
            f"({exc}). It is most likely truncated or corrupt — check the copy "
            "completed, and try the backup again.",
            code="backup.corrupt", error=str(exc),
        ) from exc

    missing = [name for name in _REQUIRED_TABLES if name not in present]
    if missing:
        conn.close()
        raise ZKTimeBackupError(
            "This SQLite database is not a ZKTime backup — it has no "
            + ", ".join(missing)
            + " table. Check you uploaded the file ZKTime's Backup button produced.",
            code="backup.not_zktime", tables=", ".join(missing),
        )

    return conn


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


# ---------------------------------------------------------------------------
# Value conversion
# ---------------------------------------------------------------------------

def _parse_time(value):
    """A ZKTime DATETIME as a naive datetime, or None if it cannot be read.

    Naive on purpose, and never converted. The column holds the terminal's own
    wall-clock digits with no offset — the same thing an ATTLOG push and an SDK
    pull hand over — and this app's rule (D10) is to store those digits
    verbatim and record separately what they mean. Converting here would move
    every punch by the offset and there would be no way to tell afterwards.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)

    text = str(value).strip()
    if not text:
        return None
    for fmt in _TIME_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _int(value, default=0):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError, AttributeError):
        return default


def _text(value) -> str:
    return "" if value is None else str(value).strip()


def _full_name(first, last) -> str:
    """``emp_firstname`` plus ``emp_lastname``, without inventing a separator.

    Every row in the operator's file has an empty ``emp_lastname``, so a
    naive join would put a trailing space on all 52 names.
    """
    parts = [_text(first), _text(last)]
    return " ".join(part for part in parts if part)


def _version_parts(value):
    """ZKTime's ``version`` ('10.0') as the (major, minor) a BIODATA command needs.

    Carried through rather than defaulted: the device is told which template
    format it is being handed, and inventing 0.0 here would be this module
    making a claim the file did not.
    """
    text = _text(value)
    if not text:
        return 0, 0
    head, _, tail = text.partition(".")
    return _int(head, 0), _int(tail, 0)


def _looks_like_base64(value: str) -> bool:
    """Is this template payload something a device could be handed back?

    Only a shape check, never a decode-and-re-encode: the bytes are stored
    exactly as the file spelled them, because a template this app rewrote is a
    template this app has taken responsibility for.
    """
    if not value:
        return False
    try:
        base64.b64decode(value, validate=True)
        return True
    except (binascii.Error, ValueError):
        return False


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def list_terminals(conn: sqlite3.Connection) -> list:
    """The terminals the file knows about, with how much of it is theirs.

    ``terminal_sns`` is the serial, not ``terminal_sn`` — see the module
    docstring. ``punches`` is counted from ``att_punches`` rather than read
    from ``att_terminal.terminal_punches``, which is ZKTime's own running
    total (92,029 against 64,132 actual rows in the operator's file) and
    describes the terminal's lifetime, not the file's contents.
    """
    rows = conn.execute(
        "SELECT id, terminal_sns, terminal_sn, terminal_name, terminal_tcpip, "
        "       terminal_port "
        "FROM att_terminal ORDER BY id"
    ).fetchall()

    terminals = []
    for row in rows:
        counted = conn.execute(
            "SELECT COUNT(*), MIN(punch_time), MAX(punch_time) "
            "FROM att_punches WHERE terminal_id = ?", (row["id"],)
        ).fetchone()
        serial = _text(row["terminal_sns"])
        # Fall back to the BIGINT column only when it holds something that
        # could be a serial. It is 0 in the operator's file, and 0 is not one.
        if not serial and _int(row["terminal_sn"], 0):
            serial = str(row["terminal_sn"])
        terminals.append({
            "terminal_id": row["id"],
            "serial": serial,
            "name": _text(row["terminal_name"]),
            "ip_address": _text(row["terminal_tcpip"]),
            # Carried so a device can be registered from this row alone; see
            # adopt_terminal. 0 in a file that never used TCP, hence the
            # fallback there rather than here — this stays what the file says.
            "port": _int(row["terminal_port"], 0),
            "punches": counted[0] or 0,
            "first_punch": _text(counted[1]) or None,
            "last_punch": _text(counted[2]) or None,
        })
    return terminals


def _pin_by_row_id(conn: sqlite3.Connection) -> dict:
    """``hr_employee.id`` → ``emp_pin``. The join every punch depends on."""
    return {
        row["id"]: _text(row["emp_pin"])
        for row in conn.execute("SELECT id, emp_pin FROM hr_employee")
    }


def inspect_backup(conn: sqlite3.Connection, terminal_id: int = None) -> dict:
    """What the file contains, for an operator to read before committing to it.

    Answers the two questions a preview exists to answer — *is this the right
    file* and *what will it do*. It deliberately says nothing about which
    device a terminal should be restored onto: that mapping is a fact about
    this installation's device table, not about the file, so the router adds
    ``registered_here`` to each terminal and the operator still chooses.
    """
    terminals = list_terminals(conn)

    scope = ""
    params = ()
    if terminal_id is not None:
        scope = " WHERE terminal_id = ?"
        params = (terminal_id,)

    punches = conn.execute(
        f"SELECT COUNT(*), MIN(punch_time), MAX(punch_time) FROM att_punches{scope}",
        params,
    ).fetchone()

    templates = []
    if _has_table(conn, "hr_biotemplate"):
        templates = [
            {"bio_type": row[0], "count": row[1]}
            for row in conn.execute(
                "SELECT bio_type, COUNT(*) FROM hr_biotemplate GROUP BY bio_type "
                "ORDER BY bio_type"
            )
        ]

    employees = conn.execute("SELECT COUNT(*) FROM hr_employee").fetchone()[0]

    # Punches whose employee_id resolves to no hr_employee row, or to one with
    # a PIN of 0. Surfaced rather than silently dropped at restore, because a
    # large number here means the file is damaged and the operator should know
    # before importing it, not after.
    pins = _pin_by_row_id(conn)
    unresolved = 0
    for row in conn.execute(
        f"SELECT employee_id FROM att_punches{scope}", params
    ):
        if not is_person_pin(pins.get(row[0])):
            unresolved += 1

    return {
        "terminals": terminals,
        "employees": employees,
        "punches": {
            "total": punches[0] or 0,
            "first": _text(punches[1]) or None,
            "last": _text(punches[2]) or None,
            "unresolved": unresolved,
        },
        "templates": templates,
        "derived_tables_ignored": _derived_row_counts(conn),
    }


def _derived_row_counts(conn: sqlite3.Connection) -> dict:
    """What is in the file and deliberately not imported.

    Reported so the preview can say so out loud. An operator who sees 167,420
    rows in the file and 64,132 restored should be able to find out where the
    difference went without reading this source.
    """
    counts = {}
    for table in ("att_day_summary", "att_day_details"):
        if _has_table(conn, table):
            counts[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    return counts


# ---------------------------------------------------------------------------
# Restoring
# ---------------------------------------------------------------------------

def adopt_terminal(db: Session, conn: sqlite3.Connection, device_sn: str,
                   created_by: str = None) -> Device:
    """Register a device from the file's own ``att_terminal`` row.

    The terminal that recorded these punches is usually the reason the backup
    is being restored at all: it is offline, or was never pointed at this
    server, so it has never announced itself and there is no ``devices`` row
    for it. Requiring the operator to go and type the serial in by hand first
    is asking them to copy four fields out of a file this code is already
    reading — and to get the serial exactly right, from a terminal they cannot
    currently reach.

    So the row is built from the file instead. **Only from the file**: a
    serial that is not one of this backup's terminals is still refused, which
    is the part of "a restore will not invent a device" that was ever load
    bearing. What is created is not an invention, it is what ZKTime recorded
    about the terminal — serial, name, IP and port, carried across verbatim.

    Approved on creation, with the operator's name on it, for the same reason
    ``POST /devices`` is: trust is withheld from serials the *server*
    discovers on its own, because /iclock/* is reachable from the internet and
    a stranger's terminal must not be able to enrol itself. A named admin
    uploading a backup and choosing which terminal in it to restore is not
    that — it is the same deliberate act as typing the serial in, and it is
    audited as one.

    The timezone is seeded from ``DEFAULT_DEVICE_TIMEZONE``, exactly as it is
    for a device created by either other route. It is not read from the file:
    ZKTime stores wall-clock digits with no zone (see ``_parse_time``), so the
    file has no timezone to offer. The caller is told the device was created
    and which zone it got, because that label is what the restored punches
    will be read under and correcting it later is a deliberate act with its
    own endpoint.
    """
    terminal = next(
        (t for t in list_terminals(conn) if t["serial"] == device_sn), None
    )
    if terminal is None:
        known = [t["serial"] for t in list_terminals(conn) if t["serial"]]
        raise ZKTimeBackupError(
            f"No device with serial {device_sn!r} is registered here, and this "
            "backup holds no terminal with that serial either, so there is "
            "nothing to create it from. "
            + (f"The file's terminal(s): {', '.join(known)}."
               if known else "The file names no terminal serial at all."),
            code="backup.unknown_device" if known else "backup.unknown_device_no_terminals",
            sn=device_sn, terminals=", ".join(known),
        )

    device = Device(
        serial_number=device_sn,
        # ZKTime's own record of the terminal. The IP is very likely still
        # right — it is a LAN address the site assigned — and where it is not,
        # it is a starting point the operator can correct, which an empty
        # field is not.
        ip_address=terminal["ip_address"] or "",
        port=_int(terminal.get("port"), 0) or 4370,
        name=terminal["name"] or None,
        status="approved",
        approved_at=datetime.now(timezone.utc),
        approved_by=created_by,
        timezone=config.DEFAULT_DEVICE_TIMEZONE,
    )
    db.add(device)
    db.commit()
    db.refresh(device)
    log.info(
        "zktime restore: registered device %s (%s) from the backup file",
        device_sn, terminal["name"] or "unnamed",
    )
    return device


def restore(db: Session, conn: sqlite3.Connection, *, device_sn: str,
            terminal_id: int = None, parts=("employees", "attendance"),
            created_by: str = None) -> dict:
    """Put the file's people, punches and templates into this app's tables.

    ``parts`` names what to restore. Templates are not in the default: see
    ``_restore_templates`` for why that one is opt-in.

    The device is the operator's own choice. When that serial is not
    registered here, it is created from the backup's own ``att_terminal`` row
    rather than refused — see ``adopt_terminal``, which is also where the
    limits on that are. A serial belonging to neither is still an error: the
    punches would otherwise land under a device nothing in the app knows
    about, attributed to a timezone nobody chose.
    """
    # Before the device is resolved, because resolving it can now *create*
    # one: a typo in `parts` must not leave a registered device behind.
    unknown = [part for part in parts if part not in PARTS]
    if unknown:
        raise ZKTimeBackupError(
            f"Unknown restore part(s): {', '.join(unknown)}",
            code="backup.unknown_parts", parts=", ".join(unknown),
        )

    device_sn = str(device_sn or "").strip()
    device = db.query(Device).filter_by(serial_number=device_sn).first()
    device_created = device is None
    if device_created:
        device = adopt_terminal(db, conn, device_sn, created_by)

    summary = {"device_sn": device_sn, "terminal_id": terminal_id,
               "parts": list(parts), "device_created": device_created,
               "device_timezone": device.timezone}

    # Order matters and is not the caller's to choose: people before punches,
    # so a punch restored in the same run is never hidden by `roster_only` for
    # want of a roster row that arrives seconds later.
    if "employees" in parts:
        summary["employees"] = _restore_employees(db, conn)
    if "attendance" in parts:
        summary["attendance"] = _restore_attendance(
            db, conn, device=device, terminal_id=terminal_id
        )
    if "templates" in parts:
        summary["templates"] = _restore_templates(db, conn, device_sn=device_sn)

    (summary["warnings"], summary["notes"],
     summary["warning_codes"], summary["note_codes"]) = _warnings(
        db, conn, parts, terminal_id,
        created_device=device if device_created else None,
    )
    return summary


def _restore_employees(db: Session, conn: sqlite3.Connection) -> dict:
    """Every person in ``hr_employee``, through the one employee writer.

    Going through ``employee_sync.upsert_employee`` rather than writing rows
    here is what keeps a restore from being a fourth source with its own
    opinions: a backup fills in a name this app never had, and cannot blank out
    one an operator typed last week.

    No ``device_employees`` link is created. That table means "this person is
    enrolled on this terminal *now*", and a backup is evidence about the day it
    was taken — possibly years ago — not about what the terminal holds today.
    Writing links from it would make the Users drawer claim an enrolment
    nothing has confirmed. The device's own Sync All answers that question
    truthfully, and this restore leaves it to do so.
    """
    created = updated = skipped = 0

    for row in conn.execute(
        "SELECT emp_pin, emp_firstname, emp_lastname, emp_privilege, "
        "       emp_cardNumber FROM hr_employee ORDER BY id"
    ):
        pin = _text(row["emp_pin"])
        if not is_person_pin(pin):
            skipped += 1
            continue

        existed = db.query(Employee).filter_by(user_id=pin[:24]).first() is not None
        emp = employee_sync.upsert_employee(
            db, pin,
            name=_full_name(row["emp_firstname"], row["emp_lastname"]),
            privilege=row["emp_privilege"],
            card=row["emp_cardNumber"],
        )
        if emp is None:
            skipped += 1
        elif existed:
            updated += 1
        else:
            created += 1

    db.commit()
    log.info(
        "zktime restore: employees — %d created, %d updated, %d skipped",
        created, updated, skipped,
    )
    return {"created": created, "updated": updated, "skipped": skipped}


def _restore_attendance(db: Session, conn: sqlite3.Connection, *, device,
                        terminal_id=None) -> dict:
    """Punches from ``att_punches``, joined to their PIN, onto one device.

    The join is the whole job. ``att_punches.employee_id`` references
    ``hr_employee.id``; the app keys attendance on the PIN. The mapping is
    built once up front and every punch goes through it — a punch whose
    employee row is missing is counted and dropped, never stored under its raw
    ``employee_id``, which would silently attribute it to whichever person
    happens to have that number as a PIN.
    """
    pins = _pin_by_row_id(conn)
    zone = device.timezone

    scope = ""
    params = ()
    if terminal_id is not None:
        scope = " WHERE terminal_id = ?"
        params = (terminal_id,)

    # The window the file covers, so the existing-key lookup reads only the
    # slice of attendance_logs that could possibly collide instead of every
    # punch this device has ever recorded.
    bounds = conn.execute(
        f"SELECT MIN(punch_time), MAX(punch_time) FROM att_punches{scope}", params
    ).fetchone()
    first, last = _parse_time(bounds[0]), _parse_time(bounds[1])

    existing_q = select(AttendanceLog.user_id, AttendanceLog.timestamp).where(
        AttendanceLog.device_sn == device.serial_number
    )
    if first and last:
        existing_q = existing_q.where(
            AttendanceLog.timestamp >= first, AttendanceLog.timestamp <= last
        )
    # `AttendanceLog.timestamp` is mapped through `UTCDateTime`, which stamps
    # tzinfo=UTC onto every value it reads back (app/database.py). The column
    # stores a naive wall-clock, `_parse_time` produces a naive wall-clock, and
    # an aware datetime never compares equal to a naive one — so without
    # stripping it here every key would miss, every punch would look new, and
    # a second restore would collide with the unique constraint instead of
    # being the no-op it is meant to be. The tzinfo carries no information:
    # what these digits mean is in the `timezone` column, not on the value.
    existing = {
        (row[0], row[1].replace(tzinfo=None) if row[1] is not None else None)
        for row in db.execute(existing_q)
    }

    inserted = duplicate = unresolved = unreadable = 0
    batch = []
    # Two punches in the file can carry the same (PIN, time) — the same person
    # badging twice in one second, or ZKTime's own import having doubled a row.
    # `existing` is updated as we go so the second one is counted as a
    # duplicate here rather than violating the unique key on flush.
    for row in conn.execute(
        "SELECT employee_id, punch_time, workstate, verifycode "
        f"FROM att_punches{scope} ORDER BY id", params
    ):
        pin = pins.get(row["employee_id"])
        if not is_person_pin(pin):
            unresolved += 1
            continue

        when = _parse_time(row["punch_time"])
        if when is None:
            unreadable += 1
            continue

        pin = pin[:24]
        key = (pin, when)
        if key in existing:
            duplicate += 1
            continue
        existing.add(key)

        batch.append(AttendanceLog(
            device_sn=device.serial_number,
            user_id=pin,
            # The file's own digits, unshifted, labelled with what the
            # operator says this device's clock means (D10).
            timestamp=when,
            status=_int(row["workstate"], 0),
            punch=_int(row["verifycode"], 0),
            source="zktime_restore",
            timezone=zone,
        ))
        inserted += 1

        if len(batch) >= _BATCH:
            db.bulk_save_objects(batch)
            db.commit()
            batch = []

    if batch:
        db.bulk_save_objects(batch)
        db.commit()

    log.info(
        "zktime restore: attendance onto %s — %d inserted, %d already present, "
        "%d with no resolvable PIN, %d with an unreadable timestamp",
        device.serial_number, inserted, duplicate, unresolved, unreadable,
    )
    return {
        "inserted": inserted,
        "already_present": duplicate,
        "unresolved_pin": unresolved,
        "unreadable_time": unreadable,
    }


def _restore_templates(db: Session, conn: sqlite3.Connection, *, device_sn: str) -> dict:
    """Fingerprint templates from ``hr_biotemplate`` into ``biometric_templates``.

    Opt-in, and off by default, for a reason worth stating plainly: these
    templates were captured by ZKTime, and whether this app can hand one back
    to a terminal through E4's ``DATA UPDATE BIODATA`` and have it accepted has
    **not** been established. Storing them is safe and useful — it is a copy of
    enrolment data that would otherwise exist nowhere else. Replaying them is
    the unverified part.

    So ``source_device_sn`` is set to the device being restored onto. That
    column exists so E4 never pushes a template back to the terminal it came
    from, and setting it this way means a restored template will not be pushed
    down automatically. An operator who wants that can still ask for it
    explicitly; nothing here decides on their behalf that an unverified format
    should be written to a live terminal.

    ``version`` ('10.0') is split into the major/minor a BIODATA command
    carries, so what does eventually reach a device describes itself honestly.
    ``size`` is ignored — it is 0 on all 74 rows in the operator's file.
    """
    if not _has_table(conn, "hr_biotemplate"):
        return {"stored": 0, "skipped": 0, "note": "the file has no hr_biotemplate table"}

    pins = _pin_by_row_id(conn)
    stored = skipped = 0

    for row in conn.execute(
        "SELECT employee_id, bio_type, version, data_format, template_no, "
        "       template_no_index, template_data, valid_flag, is_duress "
        "FROM hr_biotemplate ORDER BY id"
    ):
        pin = pins.get(row["employee_id"])
        payload = _text(row["template_data"])
        if not is_person_pin(pin) or not _looks_like_base64(payload):
            skipped += 1
            continue

        pin = pin[:24]
        type_value = _int(row["bio_type"], 1)
        no_value = _int(row["template_no"], 0)

        record = (
            db.query(BiometricTemplate)
            .filter_by(user_id=pin, type=type_value, no=no_value)
            .first()
        )
        if record is None:
            record = BiometricTemplate(user_id=pin, type=type_value, no=no_value)
            db.add(record)

        major, minor = _version_parts(row["version"])
        record.record_index = _int(row["template_no_index"], 0)
        record.valid = _int(row["valid_flag"], 1)
        record.duress = _int(row["is_duress"], 0)
        record.majorver = major
        record.minorver = minor
        record.format = _int(row["data_format"], 0)
        record.tmp = payload
        record.source_device_sn = device_sn

        # Same reason as the ADMS biodata path: makes this row visible to the
        # next iteration so two records for one key merge instead of racing
        # the unique constraint.
        db.flush()
        stored += 1

    db.commit()
    log.info(
        "zktime restore: templates — %d stored, %d skipped", stored, skipped
    )
    return {"stored": stored, "skipped": skipped}


def _warnings(db: Session, conn: sqlite3.Connection, parts, terminal_id,
              created_device=None) -> tuple:
    """Everything worth saying about a finished restore, in two piles.

    ``warnings`` is what will bite. The first of them is the one that actually
    does: punches restored without their people land in the table correctly
    and are then hidden by the attendance screen's ``roster_only`` filter,
    which drops any PIN it cannot put a name to. The rows are there; the page
    looks empty. Saying so is the difference between that and a bug report.

    ``notes`` is documented behaviour an operator reconciling counts will want
    and nobody needs shouted at them twice. Same split, same reasoning, as
    ``zktime_export._warnings``, including the twin lists of translation codes.
    """
    warnings, notes, warning_codes, note_codes = [], [], [], []

    if created_device is not None:
        # A warning, not a note. The timezone was chosen by a default, and it
        # is the label every punch in this restore was just filed under — an
        # operator who reads this now fixes it with one call, while one who
        # finds out in three months does it with a year of history in the way.
        warnings.append(
            f"Device {created_device.serial_number} was not registered here, "
            "so it was created from this file and approved in your name "
            f"(name: {created_device.name or 'unnamed'}, IP: "
            f"{created_device.ip_address or 'unknown'}). Its timezone is the "
            f"default, {created_device.timezone} — the restored punches are "
            "labelled with it. If that is the wrong zone, change it on the "
            "device's page: that relabels these punches too, without moving "
            "any of their digits."
        )
        warning_codes.append(fragment(
            "backup.restore_device_created",
            sn=created_device.serial_number,
            name=created_device.name or fragment("backup.unnamed"),
            ip=created_device.ip_address or fragment("backup.unknown"),
            timezone=created_device.timezone,
        ))

    if "attendance" in parts and "employees" not in parts:
        pins = {pin for pin in _pin_by_row_id(conn).values() if is_person_pin(pin)}
        known = {
            row[0] for row in db.execute(
                select(Employee.user_id).where(Employee.user_id.in_(pins))
            )
        } if pins else set()
        absent = len(pins - known)
        if absent:
            warnings.append(
                f"{absent} of the {len(pins)} people in this file are not on the "
                "roster. Their restored punches are stored, but the Attendance "
                "screen hides punches whose PIN it cannot name — restore "
                "Employees too, or tick 'Show hidden records', to see them."
            )
            warning_codes.append(fragment(
                "backup.restore_absent_people", absent=absent, total=len(pins)
            ))

    if terminal_id is None and len(list_terminals(conn)) > 1:
        warnings.append(
            "This file holds more than one terminal and no terminal was "
            "chosen, so every punch in it was restored onto the selected "
            "device."
        )
        warning_codes.append(fragment("backup.restore_multi_terminal"))

    ignored = _derived_row_counts(conn)
    if ignored:
        spelled = ", ".join(f"{count:,} in {name}" for name, count in ignored.items())
        notes.append(
            f"ZKTime's own calculated attendance ({spelled}) was not imported. "
            "Those are its shift engine's results, not punches; this app "
            "recalculates from the punches themselves."
        )
        note_codes.append(fragment("backup.restore_derived_ignored", spelled=[
            fragment("backup.count_in", count=count, table=name)
            for name, count in ignored.items()
        ]))

    return warnings, notes, warning_codes, note_codes
