"""Writing this app's data back out as a ZKTime .NET backup file (F2).

The other half of ``zktime_backup``. That module reads ZKTime's Backup output;
this one produces a file ZKTime's Restore will accept.

Why this builds on a template instead of writing a database from scratch
------------------------------------------------------------------------
A ZKTime backup is 82 tables, and only four of them hold anything this app
knows about. The rest are ZKTime's own installation: ``Sys_Config`` is a
single opaque BLOB, ``sys_menu`` (42 rows) and ``sys_privilege`` (122) are its
UI, ``sys_user``/``sys_role_rights`` are its logins and permissions, and
``att_shift``/``att_timetable``/``att_StatisticItem`` are the shift rules its
attendance engine runs on. None of that is derivable from anything here.

A file generated from an empty schema would open in ZKTime with no menus, no
login and no configuration — technically a ZKTime database, practically
useless. So an export starts from a real backup the operator supplies, copies
it, and replaces only the parts this app is the authority on. Everything else
survives byte for byte, including the columns of ``hr_employee`` this app does
not model: ``department_id``, ``position_id``, ``emp_pwd``, ``emp_hiredate``.
Those are the reason people are *merged* rather than rewritten — a
wipe-and-reinsert would silently strip every person's department and device
password.

What the export writes
----------------------
* ``att_terminal`` — merged on ``terminal_sns``. The app supplies name, IP and
  port; every other column (firmware version, capabilities, counters) is the
  template's and is left alone, because the app has no better answer.
* ``hr_employee`` — merged on ``emp_pin``. Only name, privilege and card are
  written.
* ``att_punches`` — **replaced in full.** This app is the system of record for
  punches; a merge would have no key to merge on.
* ``hr_biotemplate`` — replaced from ``biometric_templates``.
* ``att_day_summary`` / ``att_day_details`` — **emptied.** They are ZKTime's
  shift engine's conclusions about a punch set that has just been replaced;
  keeping them would put totals in the file that contradict the punches beside
  them. ZKTime recalculates them. This is the same judgement the restore
  direction makes in refusing to import them.

What it deliberately does not write
-----------------------------------
``fingerprint_templates`` — the SDK-sourced table, filled by pyzk's
``get_templates()`` over TCP 4370 and stored as ``codecs.encode(blob, 'hex')``.

The obstacle is **not** the encoding. ZKTime's ``template_data`` is base64 of a
1006-byte blob whose bytes 2..6 are the ASCII signature ``SS21``; pyzk's column
is hex of its own blob. hex → bytes → base64 round-trips exactly, so if the two
blobs are the same object the conversion is three lines.

What is unverified is whether they *are* the same object — whether the template
pyzk reads off the device and the one ZKTime files under a person carry the
same header and payload. Nothing here has ever compared the two, and a wrong
assumption writes a file full of templates that look valid and enrol nobody.
So the count is reported, with the note that restoring a ZKTime backup *with*
Fingerprint templates ticked fills ``biometric_templates``, which this module
does export. Settling the question needs one comparison against real data from
both sources; until then this stays a gap named rather than a guess taken.
"""

import logging
import os
import shutil
import sqlite3
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.errors import fragment
from app.models import AttendanceLog, BiometricTemplate, Device, Employee, FingerprintTemplate
from app.services.punch_filter import is_person_pin
from app.services.zktime_backup import ZKTimeBackupError, open_backup

log = logging.getLogger(__name__)

# How ZKTime spells a punch time in att_punches, confirmed against every row of
# the operator's file.
_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"

# Rows per INSERT batch. att_punches is the only table here big enough to care.
_BATCH = 2000

# Tables emptied because their contents are derived from the punches we replace.
_DERIVED_TABLES = ("att_day_summary", "att_day_details")

# Tables whose `employee_id` must not outlive the person it names. Only
# consulted when pruning, which is opt-in; see `build_export`.
_EMPLOYEE_DEPENDENTS = (
    "att_employee_shift", "att_employee_smartshift", "att_employee_temp_shift",
    "att_employee_zone", "att_exceptionassign", "att_EmployeeLeaveType",
    "ac_userPrivilege", "hr_employee_group", "hr_template",
    "pay_empDetail", "pay_EmployeeWorkCode", "pay_LoanDetail", "pay_reimbursement",
)

# The empty-string-and-zero shape ZKTime itself writes into a new hr_employee
# row, copied from the operator's file rather than guessed. Only used for a
# person the template has never seen; an existing row is updated in place and
# keeps whatever ZKTime put in these columns.
_NEW_EMPLOYEE_DEFAULTS = {
    "emp_ssn": "", "emp_role": "", "emp_username": "", "emp_pwd": "",
    "emp_timezone": "", "emp_phone": "", "emp_pin2": "", "emp_group": "",
    "emp_hiredate": "1970-01-01 00:00:00", "emp_address": "", "emp_active": 1,
    "emp_firereason": "", "emp_emergencyphone1": "", "emp_emergencyphone2": "",
    "emp_emergencyname": "", "emp_emergencyaddress": "", "emp_country": "",
    "emp_city": "", "emp_state": "", "emp_postal": "", "emp_fax": "",
    "emp_email": "", "emp_title": "", "emp_hourlyrate1": 0, "emp_hourlyrate2": 0,
    "emp_hourlyrate3": 0, "emp_hourlyrate4": 0, "emp_hourlyrate5": 0,
    "emp_gender": 0, "emp_operationmode": 0, "IsSelect": 0, "middleware_id": 0,
    "nationalID": "", "emp_payroll_id": "", "emp_payroll_type": "",
}

# Likewise for a terminal the template has never seen.
_NEW_TERMINAL_DEFAULTS = {
    "terminal_status": 1, "terminal_category": 1, "terminal_connectpwd": "",
    "terminal_dateformat": "YYYYMMDD", "terminal_baudrate": 0, "IsSelect": 0,
    "terminal_sn": 0, "policy": 1, "first_connect": 1, "isfromWDMS": 0,
    "connection_model": 0, "p2pUID": "", "BioPhotoFun": 0, "BioDataFun": 0,
    "VisilightFun": 0,
}


def export_filename(when: datetime = None) -> str:
    """The name ZKTime gives its own backups.

    The operator's file is ``bak_zktime_20261806.db`` and its newest punch is
    2026-06-18, so the date is spelled **YYYYDDMM** — year, day, month. That
    reading rests on one file, which is thin evidence for an unusual ordering,
    but matching the only example there is beats imposing a tidier one: an
    operator sorting a directory of these wants ours to sit among ZKTime's,
    not beside them. Nothing parses this name back, so a wrong guess here
    costs a filename and nothing else.
    """
    when = when or datetime.now()
    return f"bak_zktime_{when:%Y%d%m}.db"


def _naive(value):
    """A stored timestamp as the naive wall clock ZKTime expects.

    ``AttendanceLog.timestamp`` reads back tz-aware through ``UTCDateTime``
    even though the column holds a naive device wall clock. Formatting it
    without stripping that would be harmless here, but *comparing* it is not —
    the same mismatch is what broke the restore direction's duplicate check —
    so the stripping is done in one obvious place on both sides.
    """
    if value is None:
        return None
    return value.replace(tzinfo=None) if value.tzinfo else value


# ---------------------------------------------------------------------------
# Terminals
# ---------------------------------------------------------------------------

def _merge_terminals(conn: sqlite3.Connection, db: Session, extra_serials) -> tuple:
    """Bring ``att_terminal`` up to date; return ``{serial: terminal_id}``.

    Merged rather than replaced because the template's row carries a dozen
    facts this app never learns — firmware version, ZEM model, the terminal's
    own user and fingerprint counters — and an export that blanked them would
    be a worse backup than the one it was built from.

    ``extra_serials`` are device serials that appear in ``attendance_logs``
    but have no ``devices`` row, which happens when an operator deletes a
    device and keeps its history. A stub terminal is created for each, because
    the alternative is a punch with no terminal, and the punch is the thing
    worth keeping.
    """
    existing = {}
    for row in conn.execute(
        "SELECT id, terminal_sns FROM att_terminal WHERE terminal_sns IS NOT NULL"
    ):
        serial = str(row[1] or "").strip()
        if serial:
            existing[serial] = row[0]

    next_id = (conn.execute("SELECT COALESCE(MAX(id), 0) FROM att_terminal").fetchone()[0]) + 1
    next_no = (conn.execute(
        "SELECT COALESCE(MAX(terminal_no), 0) FROM att_terminal"
    ).fetchone()[0]) + 1

    updated = created = 0
    mapping = dict(existing)

    for device in db.query(Device).order_by(Device.id):
        serial = str(device.serial_number or "").strip()
        if not serial:
            continue
        if serial in existing:
            conn.execute(
                "UPDATE att_terminal SET terminal_name = ?, terminal_tcpip = ?, "
                "terminal_port = ? WHERE id = ?",
                (device.name or "", device.ip_address or "", device.port or 4370,
                 existing[serial]),
            )
            updated += 1
            continue

        columns = dict(_NEW_TERMINAL_DEFAULTS)
        columns.update({
            "id": next_id, "terminal_no": next_no, "terminal_sns": serial,
            "terminal_name": device.name or "", "terminal_tcpip": device.ip_address or "",
            "terminal_port": device.port or 4370,
        })
        _insert(conn, "att_terminal", columns)
        mapping[serial] = next_id
        next_id += 1
        next_no += 1
        created += 1

    stubs = 0
    for serial in sorted(set(extra_serials) - set(mapping)):
        columns = dict(_NEW_TERMINAL_DEFAULTS)
        columns.update({
            "id": next_id, "terminal_no": next_no, "terminal_sns": serial,
            "terminal_name": "", "terminal_tcpip": "", "terminal_port": 4370,
        })
        _insert(conn, "att_terminal", columns)
        mapping[serial] = next_id
        next_id += 1
        next_no += 1
        stubs += 1

    return mapping, {"updated": updated, "created": created, "stubs": stubs}


# ---------------------------------------------------------------------------
# People
# ---------------------------------------------------------------------------

def _merge_employees(conn: sqlite3.Connection, db: Session, punch_pins,
                     prune_missing: bool) -> tuple:
    """Bring ``hr_employee`` up to date; return ``{emp_pin: id}``.

    Only three columns are written: the name, the privilege and the card. Every
    other column belongs to ZKTime — a person's department, position, hire
    date and device password among them — and an export that overwrote those
    with this app's blanks would hand back a roster stripped of its structure.

    ``punch_pins`` are PINs that appear in ``attendance_logs``. Some belong to
    nobody on the roster: ``delete_employee`` removes the person and keeps
    their punches on purpose, because payroll has already been run off them.
    Those get a stub row with an empty name, which is exactly what the data
    says — there are punches under this PIN and no name to put to it — and is
    what keeps the punch exportable at all.
    """
    existing = {}
    for row in conn.execute("SELECT id, emp_pin FROM hr_employee"):
        pin = str(row[1] or "").strip()
        if pin:
            existing[pin] = row[0]

    next_id = (conn.execute("SELECT COALESCE(MAX(id), 0) FROM hr_employee").fetchone()[0]) + 1
    # A new person needs a department or ZKTime has nowhere to file them.
    # Guarded because `hr_department` is not one of the tables `open_backup`
    # insists on: a template without it is unusual but not invalid, and the
    # export should write the person with a null department rather than fail.
    default_department = None
    if _has_table(conn, "hr_department"):
        default_department = conn.execute(
            "SELECT id FROM hr_department ORDER BY defaultDepartment DESC, id LIMIT 1"
        ).fetchone()

    mapping = dict(existing)
    updated = created = 0
    roster_pins = set()

    for employee in db.query(Employee).order_by(Employee.id):
        pin = str(employee.user_id or "").strip()
        if not pin:
            continue
        roster_pins.add(pin)
        # Employee.card defaults to the string "0", which is this app's way of
        # saying "no card" — the same thing ZKTime spells as ''. Writing "0"
        # through would invent a card number nobody carries.
        card = "" if str(employee.card or "").strip() in ("", "0") else str(employee.card).strip()

        if pin in existing:
            conn.execute(
                "UPDATE hr_employee SET emp_firstname = ?, emp_lastname = ?, "
                "emp_privilege = ?, emp_cardNumber = ? WHERE id = ?",
                (employee.name or "", "", str(employee.privilege or 0), card,
                 existing[pin]),
            )
            updated += 1
            continue

        columns = dict(_NEW_EMPLOYEE_DEFAULTS)
        columns.update({
            "id": next_id, "emp_pin": pin, "emp_firstname": employee.name or "",
            "emp_lastname": "", "emp_privilege": str(employee.privilege or 0),
            "emp_cardNumber": card,
        })
        if default_department:
            columns["department_id"] = default_department[0]
        _insert(conn, "hr_employee", columns)
        mapping[pin] = next_id
        next_id += 1
        created += 1

    stubs = 0
    for pin in sorted(set(punch_pins) - set(mapping)):
        columns = dict(_NEW_EMPLOYEE_DEFAULTS)
        # An empty name rather than the PIN repeated as one. The app's own
        # convention for an unnamed person is to fall back to the PIN at the
        # point of display, never to store it as a name.
        columns.update({
            "id": next_id, "emp_pin": pin, "emp_firstname": "", "emp_lastname": "",
            "emp_privilege": "0", "emp_cardNumber": "",
        })
        if default_department:
            columns["department_id"] = default_department[0]
        _insert(conn, "hr_employee", columns)
        mapping[pin] = next_id
        next_id += 1
        stubs += 1

    pruned = 0
    if prune_missing:
        # Everyone the template holds that this app has neither on its roster
        # nor in its punch history. Their dependent rows go first, so nothing
        # is left pointing at an id that no longer resolves.
        doomed = [
            (pin, row_id) for pin, row_id in existing.items()
            if pin not in roster_pins and pin not in punch_pins
        ]
        for _pin, row_id in doomed:
            for table in _EMPLOYEE_DEPENDENTS:
                if _has_table(conn, table):
                    conn.execute(f"DELETE FROM {table} WHERE employee_id = ?", (row_id,))
            conn.execute("DELETE FROM hr_employee WHERE id = ?", (row_id,))
            mapping.pop(_pin, None)
            pruned += 1

    return mapping, {"updated": updated, "created": created,
                     "stubs": stubs, "pruned": pruned}


# ---------------------------------------------------------------------------
# Punches and templates
# ---------------------------------------------------------------------------

def _replace_punches(conn: sqlite3.Connection, db: Session, pins, terminals) -> dict:
    """Empty ``att_punches`` and write every row this app holds.

    Replaced whole rather than merged: ZKTime's punch rows have no stable key
    to merge on — ``id`` is a local autoincrement that means nothing outside
    the file it came from — so a merge would either duplicate everything or
    require the same (person, time, terminal) comparison the restore direction
    does, for no gain. This app is the system of record for punches; the file
    is a snapshot of it.
    """
    conn.execute("DELETE FROM att_punches")

    columns = ("id", "employee_id", "punch_time", "workcode", "workstate",
               "verifycode", "terminal_id", "punch_type", "IsSelect",
               "middleware_id", "login_combination", "status", "processed")
    statement = (
        f"INSERT INTO att_punches ({', '.join(columns)}) "
        f"VALUES ({', '.join('?' * len(columns))})"
    )

    query = select(
        AttendanceLog.device_sn, AttendanceLog.user_id, AttendanceLog.timestamp,
        AttendanceLog.status, AttendanceLog.punch,
    ).order_by(AttendanceLog.timestamp, AttendanceLog.id)

    written = skipped = 0
    row_id = 0
    batch = []

    for device_sn, user_id, timestamp, status, punch in db.execute(
        query.execution_options(stream_results=True, yield_per=_BATCH)
    ):
        pin = str(user_id or "").strip()
        when = _naive(timestamp)
        # A PIN of 0 is a device event, not attendance, and was never given an
        # hr_employee row above. Nothing is guessed for it; it simply does not
        # belong in a file whose att_punches means "somebody badged".
        if when is None or not is_person_pin(pin) or pin not in pins:
            skipped += 1
            continue

        row_id += 1
        batch.append((
            row_id, pins[pin], when.strftime(_TIME_FORMAT), 0,
            int(status or 0), str(punch if punch is not None else 0),
            terminals.get(str(device_sn or "").strip()),
            # The constants every row of the operator's file carries. Written
            # explicitly rather than left to default so the exported rows are
            # the same shape ZKTime writes, not merely valid.
            "0", 0, 0, 0, 0, 0,
        ))
        written += 1

        if len(batch) >= _BATCH:
            conn.executemany(statement, batch)
            batch = []

    if batch:
        conn.executemany(statement, batch)

    return {"written": written, "skipped": skipped}


def _replace_biotemplates(conn: sqlite3.Connection, db: Session, pins) -> tuple:
    """Empty ``hr_biotemplate`` and write the app's biometric templates.

    ``size`` is written as 0, which looks wrong and is not: every one of the 74
    rows in the operator's own file has ``size = 0``, so 0 is what ZKTime
    itself puts there. Computing a real length would make our file differ from
    ZKTime's in a column ZKTime evidently does not use.

    ``version`` is reassembled from the major/minor the app stored when the
    template arrived, so a template that came out of a ZKTime backup as '10.0'
    goes back as '10.0'.
    """
    conn.execute("DELETE FROM hr_biotemplate")

    columns = ("id", "valid_flag", "is_duress", "bio_type", "version",
               "data_format", "template_no", "template_no_index",
               "template_data", "size", "employee_id")
    statement = (
        f"INSERT INTO hr_biotemplate ({', '.join(columns)}) "
        f"VALUES ({', '.join('?' * len(columns))})"
    )

    written = skipped = 0
    row_id = 0
    batch = []
    # (pin, finger index) of every fingerprint actually written. The SDK
    # warning compares against this: a template it holds for a finger already
    # in the file is a duplicate, not a gap.
    fingerprints = set()

    for record in db.query(BiometricTemplate).order_by(BiometricTemplate.id):
        pin = str(record.user_id or "").strip()
        if pin not in pins or not record.tmp:
            skipped += 1
            continue
        row_id += 1
        batch.append((
            row_id, int(record.valid or 0), int(record.duress or 0),
            int(record.type or 0),
            f"{int(record.majorver or 0)}.{int(record.minorver or 0)}",
            int(record.format or 0), int(record.no or 0),
            int(record.record_index or 0), record.tmp, 0, pins[pin],
        ))
        written += 1
        # bio_type 1 is a fingerprint; that is the only modality pyzk's
        # get_templates() produces, so it is the only one worth comparing.
        if int(record.type or 0) == 1:
            fingerprints.add((pin, int(record.no or 0)))

    if batch:
        conn.executemany(statement, batch)

    return {"written": written, "skipped": skipped}, fingerprints


# ---------------------------------------------------------------------------
# Housekeeping
# ---------------------------------------------------------------------------

def _clear_derived(conn: sqlite3.Connection) -> dict:
    cleared = {}
    for table in _DERIVED_TABLES:
        if _has_table(conn, table):
            count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            conn.execute(f"DELETE FROM {table}")
            cleared[table] = count
    return cleared


def _fix_sequences(conn: sqlite3.Connection, tables) -> None:
    """Point ``sqlite_sequence`` at the real high-water mark of each table.

    Every id above is assigned explicitly, which leaves SQLite's own
    autoincrement counter wherever the template left it. ZKTime inserts into
    these tables with AUTOINCREMENT, so a stale counter would hand the next
    row an id that is already taken — and ``att_punches`` is precisely the
    table ZKTime writes to on every sync.
    """
    if not _has_table(conn, "sqlite_sequence"):
        return
    for table in tables:
        if not _has_table(conn, table):
            continue
        high = conn.execute(f"SELECT COALESCE(MAX(id), 0) FROM {table}").fetchone()[0]
        updated = conn.execute(
            "UPDATE sqlite_sequence SET seq = ? WHERE name = ?", (high, table)
        ).rowcount
        if not updated:
            conn.execute(
                "INSERT INTO sqlite_sequence (name, seq) VALUES (?, ?)", (table, high)
            )


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _insert(conn: sqlite3.Connection, table: str, columns: dict) -> None:
    names = list(columns)
    conn.execute(
        f"INSERT INTO {table} ({', '.join(names)}) "
        f"VALUES ({', '.join('?' * len(names))})",
        [columns[name] for name in names],
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def build_export(db: Session, template_path: str, out_path: str, *,
                 prune_missing: bool = False) -> dict:
    """Write a ZKTime backup holding this app's data. Returns what it did.

    ``prune_missing`` removes people the template holds that this app has
    neither on its roster nor in its punch history, along with their shift,
    zone and payroll rows. Off by default, and deliberately: a fresh install
    that has never had a restore run against it has an empty roster, and the
    obliging thing for this function to do in that case is nothing, not to
    hand back a backup with every employee deleted.
    """
    # Validated through the reader, so a template that is not a ZKTime backup
    # is refused by the same rules and with the same wording as one being
    # restored — rather than copied, half-written and found wanting.
    open_backup(template_path).close()

    template_punches = _template_punch_count(template_path)

    shutil.copyfile(template_path, out_path)

    conn = sqlite3.connect(out_path)
    try:
        # Serials that punches refer to, whether or not a device row survives.
        punch_serials = {
            str(row[0] or "").strip()
            for row in db.execute(select(AttendanceLog.device_sn).distinct())
            if str(row[0] or "").strip()
        }
        punch_pins = {
            str(row[0] or "").strip()
            for row in db.execute(select(AttendanceLog.user_id).distinct())
            if is_person_pin(row[0])
        }

        with conn:
            terminals, terminal_counts = _merge_terminals(conn, db, punch_serials)
            pins, employee_counts = _merge_employees(
                conn, db, punch_pins, prune_missing
            )
            punches = _replace_punches(conn, db, pins, terminals)
            templates, fingerprints = _replace_biotemplates(conn, db, pins)
            cleared = _clear_derived(conn)
            _fix_sequences(
                conn,
                ("att_punches", "hr_biotemplate", "hr_employee", "att_terminal")
                + _DERIVED_TABLES,
            )

        # Outside the transaction — SQLite refuses to VACUUM inside one. The
        # file has just lost ~184k derived rows and gained 64k punches; without
        # this it keeps every freed page and exports larger than it needs to.
        conn.execute("VACUUM")
    except Exception:
        # A half-written export is worse than none: it is a plausible-looking
        # backup file with some of the data in it.
        conn.close()
        if os.path.exists(out_path):
            os.remove(out_path)
        raise
    finally:
        if conn:
            conn.close()

    summary = {
        "filename": export_filename(),
        "size": os.path.getsize(out_path),
        "terminals": terminal_counts,
        "employees": employee_counts,
        "punches": punches,
        "templates": templates,
        "cleared": cleared,
        "pruned_missing_people": prune_missing,
    }
    (summary["warnings"], summary["notes"],
     summary["warning_codes"], summary["note_codes"]) = _warnings(
        db, summary, template_punches, fingerprints
    )
    log.info(
        "zktime export: %s — %d punch(es), %d person/people, %d template(s)",
        summary["filename"], punches["written"],
        employee_counts["updated"] + employee_counts["created"] + employee_counts["stubs"],
        templates["written"],
    )
    return summary


def _template_punch_count(path: str) -> int:
    conn = open_backup(path)
    try:
        return conn.execute("SELECT COUNT(*) FROM att_punches").fetchone()[0]
    finally:
        conn.close()


def _warnings(db: Session, summary: dict, template_punches: int,
              fingerprints: set) -> tuple:
    """Everything worth saying about a finished export, in two piles.

    The split is the difference between *this backup is not what you think it
    is* and *here is what that number means*. Both were one list, and the
    result was five paragraphs of equal weight in which the one that mattered
    — no fingerprints in this file — sat between two restatements of documented
    behaviour. An operator who reads all five once will not read any of them
    the second time.

    ``warnings`` is for a file that will disappoint someone later: it holds
    less than its template, or it is missing a whole category of data. Those
    are shown.

    ``notes`` is for arithmetic that is correct and surprising only until
    explained — why 83,160 punches became 83,160 and not 96,345. They are kept
    (an operator reconciling counts needs them) and folded away.

    Each pile has a twin of translation codes, in the same order, for the UI
    (see app/errors.py) — the English strings stay what logs and tests read.
    """
    warnings, notes, warning_codes, note_codes = [], [], [], []
    written = summary["punches"]["written"]

    # The footgun this whole feature has: att_punches is replaced wholesale, so
    # exporting from an app that holds less history than the template hands
    # back a *smaller* backup than the one it was built from. Nothing is lost
    # — the template is untouched — but an operator who overwrites their old
    # backup with this one would lose the difference.
    if template_punches and written < template_punches:
        warnings.append(
            f"This export holds {written:,} punches; the template you built it "
            f"from held {template_punches:,}. att_punches is replaced in full, "
            "so the difference is not in this file. Keep the original backup "
            "rather than overwriting it, and restore it here first if you "
            "want one file with everything."
        )
        warning_codes.append(fragment(
            "backup.export_fewer_punches", written=written, template=template_punches
        ))

    # SDK-pulled templates the file does not already carry for that finger.
    #
    # Counting the whole table was a false alarm, and a loud one: the same 72
    # fingers routinely exist in both tables — `biometric_templates` from the
    # device's own biodata push (or a restore), `fingerprint_templates` from
    # an SDK pull — so a backup with all 72 fingerprints in it was reporting
    # "72 not exported". What matters is whether any finger is in the SDK
    # table and in no other, because only that is missing from the file.
    missing = [
        (pin, finger) for pin, finger in db.execute(
            select(FingerprintTemplate.user_id, FingerprintTemplate.finger_id)
        )
        if (str(pin or "").strip(), int(finger or 0)) not in fingerprints
    ]
    if missing:
        people = len({pin for pin, _finger in missing})
        warnings.append(
            f"{len(missing)} SDK-pulled fingerprint template(s) across "
            f"{people} person/people are not in this backup"
            + (" — it carries no fingerprints at all"
               if not summary["templates"]["written"] else "")
            + ". They exist only in pyzk's hex encoding, not the base64 "
            "hr_biotemplate uses. Re-encoding is mechanical, but whether a "
            "template read over the SDK is interchangeable with one ZKTime "
            "stores has not been verified here, so they were left out rather "
            "than written on an assumption. Restoring a ZKTime backup with "
            "Fingerprint templates ticked, or Sync Templates on the device, "
            "is what puts a finger in this file."
        )
        warning_codes.append(fragment(
            "backup.export_missing_fingerprints"
            if summary["templates"]["written"]
            else "backup.export_no_fingerprints",
            count=len(missing), people=people,
        ))

    stubs = summary["employees"]["stubs"]
    if stubs:
        notes.append(
            f"{stubs} person/people appear in attendance history but not on the "
            "roster — deleted employees whose punches were kept. They were "
            "written with a blank name so their punches stay in the file."
        )
        note_codes.append(fragment("backup.export_stubs", count=stubs))

    if summary["punches"]["skipped"]:
        notes.append(
            f"{summary['punches']['skipped']:,} stored record(s) were left out: "
            "they carry PIN 0, which a terminal writes for a device event or a "
            "scan that matched nobody, and is not attendance."
        )
        note_codes.append(fragment(
            "backup.export_skipped_pin0", count=summary["punches"]["skipped"]
        ))

    cleared = summary["cleared"]
    if cleared:
        spelled = ", ".join(f"{count:,} from {name}" for name, count in cleared.items())
        notes.append(
            f"ZKTime's calculated attendance was cleared ({spelled}). It was "
            "computed from the punches this export replaced, so keeping it "
            "would put totals in the file that disagree with the punches next "
            "to them. ZKTime recalculates on demand."
        )
        note_codes.append(fragment("backup.export_cleared", spelled=[
            fragment("backup.count_from", count=count, table=name)
            for name, count in cleared.items()
        ]))

    return warnings, notes, warning_codes, note_codes
