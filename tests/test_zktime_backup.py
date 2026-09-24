"""Restoring a ZKTime .NET backup (F1).

The fixture below is a real ZKTime database in miniature: the DDL is copied
verbatim from the operator's own ``bak_zktime_20261806.db`` rather than
paraphrased, because the details these tests exist to pin down are details of
*that* schema — ``att_punches.employee_id`` being a foreign key to
``hr_employee.id`` and not a PIN, the serial living in ``terminal_sns`` and not
``terminal_sn``, the name living entirely in ``emp_firstname``. A tidied-up
fixture would test a schema nobody ships.
"""

import os
import sqlite3
import tempfile
import unittest
from datetime import datetime

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import config
from app.database import Base, get_db
from app.deps import require_admin, require_auth
from app.models import AttendanceLog, AuditLog, BiometricTemplate, Device, Employee, User
from app.routers import backup as backup_router
from app.models import FingerprintTemplate
from app.services import zktime_backup, zktime_export
from app.services.zktime_backup import ZKTimeBackupError

SN = "2145222460344"
OTHER_SN = "9900000000001"

# Straight out of the operator's file.
_DDL = """
CREATE TABLE hr_employee (id integer primary key autoincrement,
    emp_pin TEXT not null, emp_ssn TEXT, emp_role TEXT, emp_firstname TEXT not null,
    emp_lastname TEXT, emp_username TEXT, emp_pwd TEXT, emp_timezone TEXT,
    emp_phone TEXT, emp_pin2 TEXT, emp_photo BLOB, emp_privilege TEXT,
    emp_group TEXT, emp_hiredate DATETIME, emp_address TEXT, emp_active INT not null,
    emp_firedate DATETIME, emp_firereason TEXT, emp_emergencyphone1 TEXT,
    emp_emergencyphone2 TEXT, emp_emergencyname TEXT, emp_emergencyaddress TEXT,
    emp_cardNumber TEXT, emp_country TEXT, emp_city TEXT, emp_state TEXT,
    emp_postal TEXT, emp_fax TEXT, emp_email TEXT, emp_title TEXT,
    emp_hourlyrate1 NUMERIC, emp_hourlyrate2 NUMERIC, emp_hourlyrate3 NUMERIC,
    emp_hourlyrate4 NUMERIC, emp_hourlyrate5 NUMERIC, emp_gender INT,
    emp_birthday DATETIME, emp_operationmode INT, IsSelect INT, middleware_id BIGINT,
    nationalID TEXT, emp_payroll_id TEXT, emp_payroll_type TEXT, emp_Verify TEXT,
    emp_ViceCard TEXT, department_id INT, position_id INT);
CREATE TABLE att_terminal (id integer primary key autoincrement,
    terminal_no INT not null, terminal_status INT not null, terminal_name TEXT,
    terminal_location TEXT, terminal_category INT not null, terminal_type TEXT,
    terminal_connectpwd TEXT, terminal_domainname TEXT, terminal_dateformat TEXT,
    terminal_tcpip TEXT, AGR_version TEXT, terminal_port INT, terminal_baudrate INT,
    terminal_users INT, terminal_fingerprints INT, terminal_faces INT,
    terminal_palms INT, terminal_fvs INT, terminal_punches INT, IsSelect INT,
    terminal_sn BIGINT, terminal_sns TEXT, policy INT, first_connect BOOL,
    terminal_desc TEXT, terminal_photostamp TEXT, terminal_AttLogStamp TEXT,
    isfromWDMS INT, connection_model INT, terminal_zem TEXT,
    terminal_firmversion TEXT, terminal_admins INT, p2pUID TEXT, BioPhotoFun INT,
    BioDataFun INT, VisilightFun INT);
CREATE TABLE att_punches (id integer primary key autoincrement,
    employee_id INT not null, punch_time DATETIME not null, workcode INT,
    workstate INT, verifycode TEXT, terminal_id INT, punch_type TEXT, operator TEXT,
    operator_reason TEXT, operator_time DATETIME, IsSelect INT, reserved1 TEXT,
    reserved2 TEXT, middleware_id BIGINT, attendance_event TEXT,
    login_combination INT, status INT, annotation TEXT, processed INT,
    constraint FK63030A9050F52429 foreign key (employee_id) references hr_employee,
    constraint FK63030A9060342464 foreign key (terminal_id) references att_terminal);
CREATE TABLE hr_biotemplate (id integer primary key autoincrement,
    valid_flag INT not null, is_duress INT not null, bio_type INT not null,
    version TEXT not null, data_format INT not null, template_no INT not null,
    template_no_index INT not null, template_data TEXT, size INT not null,
    employee_id INT not null,
    constraint FKF43C035350F52429 foreign key (employee_id) references hr_employee);
-- The remaining tables are trimmed to the columns these tests touch; the four
-- above are verbatim because they are the ones the export writes into, and a
-- narrowed column list would hide exactly the mistakes this file exists to catch.
CREATE TABLE att_day_summary (id integer primary key autoincrement, att_date DATETIME not null,
    employee_id INT not null);
CREATE TABLE att_day_details (id integer primary key autoincrement, employee_id INT not null);
CREATE TABLE hr_department (id integer primary key autoincrement, dept_code INT not null,
    dept_name TEXT not null, dept_parentcode INT not null, defaultDepartment INT,
    company_id INT not null);
CREATE TABLE att_employee_shift (id integer primary key autoincrement, employee_id INT not null,
    shift_id INT not null);
CREATE TABLE pay_empDetail (id integer primary key autoincrement, employee_id INT not null,
    amount NUMERIC);
-- ZKTime's own installation: none of this is derivable from the app, and an
-- export must hand every row of it back untouched.
CREATE TABLE Sys_Config (ID integer primary key autoincrement, ConfigType SMALLINT not null unique,
    Data BLOB);
CREATE TABLE sys_menu (id integer primary key autoincrement, name TEXT);
CREATE TABLE sys_user (id integer primary key autoincrement, username TEXT, pwd TEXT);
CREATE TABLE att_shift (id integer primary key autoincrement, shift_name TEXT);
"""

# Three people. The second has a PIN of 0 — a device event row, not a person —
# and the third is the leaver case: on the roster in the file, punches of their
# own. `emp_lastname` is '' throughout, as it is in all 52 real rows.
_PEOPLE = [
    (1, "201", "DONG THIEN VU", "", "3", "", 1),
    (2, "0", "", "", "0", "", 1),
    (3, "206", "NGUYEN NHA HIEU", "", "0", "1234567", 1),
]

# employee_id here is hr_employee.id — 1 and 3, NOT the PINs 201 and 206.
# A restore that reads this column as a PIN would file these punches under
# people called "1" and "3".
_PUNCHES = [
    (1, 1, "2023-02-16 17:12:15", 0, 0, "1", 1),
    (2, 1, "2024-06-01 08:01:02", 0, 0, "1", 1),
    (3, 3, "2024-06-01 08:05:44", 0, 0, "4", 1),
    (4, 2, "2024-06-01 08:06:00", 0, 0, "1", 1),   # PIN 0 — must be dropped
    (5, 3, "2026-06-18 08:58:33", 0, 0, "1", 1),
    (6, 1, "2024-07-02 09:00:00", 0, 0, "1", 2),   # a second terminal
]

# 'AAAA' is valid base64; the second row's payload is not, and must be skipped
# rather than stored as something a device could later be handed.
_TEMPLATES = [
    (1, 1, 0, 1, "10.0", 0, 6, 0, "U3F1YXJl", 0, 1),
    (2, 1, 0, 1, "10.0", 0, 7, 0, "not valid base64!!", 0, 1),
    (3, 1, 0, 9, "9.5", 0, 0, 0, "RmFjZQ==", 0, 3),
]


def build_backup(path, *, punches=_PUNCHES, people=_PEOPLE, templates=_TEMPLATES,
                 terminals=None):
    conn = sqlite3.connect(path)
    conn.executescript(_DDL)
    conn.executemany(
        "INSERT INTO hr_employee (id, emp_pin, emp_firstname, emp_lastname, "
        "emp_privilege, emp_cardNumber, emp_active) VALUES (?,?,?,?,?,?,?)", people
    )
    if terminals is None:
        # terminal_port is 4370 on both, as it is in the operator's file. The
        # second is left at 0 nowhere — a file that never used TCP is covered
        # by TerminalAdoptionTests instead, where the fallback is the point.
        terminals = [
            (1, 1, 1, "WTS Access Control", 1, "192.168.79.10", 4370, 0, SN, 92029),
            (2, 2, 1, "Back Gate", 1, "192.168.79.11", 4370, 0, OTHER_SN, 12),
        ]
    conn.executemany(
        "INSERT INTO att_terminal (id, terminal_no, terminal_status, terminal_name, "
        "terminal_category, terminal_tcpip, terminal_port, terminal_sn, terminal_sns, "
        "terminal_punches) VALUES (?,?,?,?,?,?,?,?,?,?)", terminals
    )
    conn.executemany(
        "INSERT INTO att_punches (id, employee_id, punch_time, workcode, workstate, "
        "verifycode, terminal_id) VALUES (?,?,?,?,?,?,?)", punches
    )
    conn.executemany(
        "INSERT INTO hr_biotemplate (id, valid_flag, is_duress, bio_type, version, "
        "data_format, template_no, template_no_index, template_data, size, employee_id) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)", templates
    )
    conn.executemany(
        "INSERT INTO att_day_summary (id, att_date, employee_id) VALUES (?,?,?)",
        [(i, "2024-06-01 00:00:00", 1) for i in range(1, 6)],
    )
    conn.executemany(
        "INSERT INTO att_day_details (id, employee_id) VALUES (?,?)",
        [(i, 1) for i in range(1, 4)],
    )
    conn.execute(
        "INSERT INTO hr_department (id, dept_code, dept_name, dept_parentcode, "
        "defaultDepartment, company_id) VALUES (1, 1, 'Department', 0, 1, 1)"
    )
    # Shift and payroll rows for person 3, so pruning can be shown to take
    # their dependents with them.
    conn.execute("INSERT INTO att_employee_shift (id, employee_id, shift_id) VALUES (1, 3, 1)")
    conn.execute("INSERT INTO pay_empDetail (id, employee_id, amount) VALUES (1, 3, 100)")
    # ZKTime's own rows, including an opaque BLOB nothing here can regenerate.
    conn.execute("INSERT INTO Sys_Config (ID, ConfigType, Data) VALUES (1, 1, ?)",
                 (b"\x00\x01opaque ZKTime configuration blob\xff",))
    conn.executemany("INSERT INTO sys_menu (id, name) VALUES (?,?)",
                     [(i, f"Menu {i}") for i in range(1, 8)])
    conn.executemany("INSERT INTO sys_user (id, username, pwd) VALUES (?,?,?)",
                     [(1, "admin", "hash"), (2, "hr", "hash2")])
    conn.execute("INSERT INTO att_shift (id, shift_name) VALUES (1, 'Default')")
    conn.commit()
    conn.close()
    return path


class BackupTestCase(unittest.TestCase):
    """A fresh in-memory app database and a ZKTime backup file on disk."""

    def setUp(self):
        self.engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
        )
        Base.metadata.create_all(bind=self.engine)
        self.Session = sessionmaker(bind=self.engine, autocommit=False, autoflush=False)

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = build_backup(os.path.join(self.tmp.name, "bak.db"))

        db = self.Session()
        try:
            db.add(Device(serial_number=SN, ip_address="192.168.79.10", port=4370,
                          name="WTS Access Control", status="approved",
                          timezone="Asia/Ho_Chi_Minh"))
            db.commit()
        finally:
            db.close()

    def open(self):
        conn = zktime_backup.open_backup(self.path)
        self.addCleanup(conn.close)
        return conn

    def restore(self, db, **kwargs):
        kwargs.setdefault("device_sn", SN)
        kwargs.setdefault("terminal_id", 1)
        return zktime_backup.restore(db, self.open(), **kwargs)


class OpeningTests(BackupTestCase):

    def test_a_file_that_is_not_sqlite_is_refused_by_name(self):
        path = os.path.join(self.tmp.name, "notadb.db")
        with open(path, "wb") as handle:
            handle.write(b"PK\x03\x04 this is a zip, not a database")
        with self.assertRaises(ZKTimeBackupError) as caught:
            zktime_backup.open_backup(path)
        self.assertIn("not a SQLite database", str(caught.exception))

    def test_a_sqlite_database_that_is_not_a_zktime_backup_names_what_is_missing(self):
        path = os.path.join(self.tmp.name, "other.db")
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE something (id integer)")
        conn.commit()
        conn.close()
        with self.assertRaises(ZKTimeBackupError) as caught:
            zktime_backup.open_backup(path)
        message = str(caught.exception)
        self.assertIn("hr_employee", message)
        self.assertIn("att_punches", message)

    def test_a_missing_file_is_an_error_not_a_crash(self):
        with self.assertRaises(ZKTimeBackupError):
            zktime_backup.open_backup(os.path.join(self.tmp.name, "gone.db"))

    def test_the_backup_is_opened_read_only(self):
        """A restore must not be able to damage the one copy of the history."""
        conn = self.open()
        with self.assertRaises(sqlite3.OperationalError):
            conn.execute("DELETE FROM att_punches")


class PreviewTests(BackupTestCase):

    def test_the_serial_comes_from_terminal_sns_not_terminal_sn(self):
        terminals = zktime_backup.list_terminals(self.open())
        self.assertEqual([t["serial"] for t in terminals], [SN, OTHER_SN])

    def test_punches_are_counted_not_read_from_the_terminals_own_total(self):
        """att_terminal.terminal_punches is 92,029 and the file holds 5."""
        first = zktime_backup.list_terminals(self.open())[0]
        self.assertEqual(first["punches"], 5)

    def test_preview_counts_punches_that_resolve_to_nobody(self):
        preview = zktime_backup.inspect_backup(self.open(), terminal_id=1)
        self.assertEqual(preview["punches"]["total"], 5)
        self.assertEqual(preview["punches"]["unresolved"], 1)   # the PIN-0 row

    def test_preview_reports_the_derived_tables_it_will_not_import(self):
        preview = zktime_backup.inspect_backup(self.open())
        self.assertEqual(preview["derived_tables_ignored"]["att_day_summary"], 5)
        self.assertEqual(preview["derived_tables_ignored"]["att_day_details"], 3)


class EmployeeRestoreTests(BackupTestCase):

    def test_people_are_created_and_pin_zero_is_not_one_of_them(self):
        db = self.Session()
        self.addCleanup(db.close)
        summary = self.restore(db, parts=("employees",))
        self.assertEqual(summary["employees"]["created"], 2)
        self.assertEqual(summary["employees"]["skipped"], 1)
        self.assertEqual(
            sorted(e.user_id for e in db.query(Employee).all()), ["201", "206"]
        )

    def test_an_empty_last_name_does_not_leave_a_trailing_space(self):
        """Every row in the operator's file has emp_lastname = ''."""
        db = self.Session()
        self.addCleanup(db.close)
        self.restore(db, parts=("employees",))
        self.assertEqual(
            db.query(Employee).filter_by(user_id="201").first().name, "DONG THIEN VU"
        )

    def test_privilege_and_card_come_across(self):
        db = self.Session()
        self.addCleanup(db.close)
        self.restore(db, parts=("employees",))
        admin = db.query(Employee).filter_by(user_id="201").first()
        carded = db.query(Employee).filter_by(user_id="206").first()
        self.assertEqual(admin.privilege, 3)
        self.assertEqual(carded.card, "1234567")

    def test_a_restore_never_erases_a_name_an_operator_typed(self):
        """The rule every employee source obeys: fill in, never empty out."""
        db = self.Session()
        self.addCleanup(db.close)
        db.add(Employee(user_id="201", name="Operator's Own Spelling", card="9999"))
        db.commit()

        self.restore(db, parts=("employees",))

        kept = db.query(Employee).filter_by(user_id="201").first()
        # The file has a name for this person, so it wins — but the card,
        # which the file leaves empty, must survive.
        self.assertEqual(kept.name, "DONG THIEN VU")
        self.assertEqual(kept.card, "9999")

    def test_no_device_enrolment_link_is_invented(self):
        """A backup says who was in ZKTime, not who is on the terminal now."""
        from app.models import DeviceEmployee
        db = self.Session()
        self.addCleanup(db.close)
        self.restore(db, parts=("employees",))
        self.assertEqual(db.query(DeviceEmployee).count(), 0)


class AttendanceRestoreTests(BackupTestCase):

    def test_punches_are_filed_under_the_pin_not_the_employee_row_id(self):
        """The mistake that would misattribute all 64,132 real rows."""
        db = self.Session()
        self.addCleanup(db.close)
        self.restore(db, parts=("attendance",))

        pins = sorted({row.user_id for row in db.query(AttendanceLog).all()})
        self.assertEqual(pins, ["201", "206"])
        # The raw employee_id values must appear nowhere.
        self.assertNotIn("1", pins)
        self.assertNotIn("3", pins)

    def test_a_punch_belonging_to_pin_zero_is_not_attendance(self):
        db = self.Session()
        self.addCleanup(db.close)
        summary = self.restore(db, parts=("attendance",))
        self.assertEqual(summary["attendance"]["unresolved_pin"], 1)
        self.assertEqual(db.query(AttendanceLog).count(), 4)

    def test_only_the_chosen_terminals_punches_are_restored(self):
        db = self.Session()
        self.addCleanup(db.close)
        self.restore(db, parts=("attendance",), terminal_id=1)
        # Punch 6 belongs to terminal 2 and must not be here.
        self.assertEqual(db.query(AttendanceLog).count(), 4)

    def test_omitting_the_terminal_takes_every_punch_in_the_file(self):
        db = self.Session()
        self.addCleanup(db.close)
        self.restore(db, parts=("attendance",), terminal_id=None)
        self.assertEqual(db.query(AttendanceLog).count(), 5)

    def test_the_wall_clock_is_stored_verbatim_and_never_shifted(self):
        """D10: the digits are kept and labelled, not converted."""
        db = self.Session()
        self.addCleanup(db.close)
        self.restore(db, parts=("attendance",))

        row = (db.query(AttendanceLog)
                 .order_by(AttendanceLog.timestamp.desc()).first())
        stored = row.timestamp.replace(tzinfo=None)
        self.assertEqual(stored, datetime(2026, 6, 18, 8, 58, 33))
        self.assertEqual(row.timezone, "Asia/Ho_Chi_Minh")

    def test_restored_rows_say_where_they_came_from(self):
        db = self.Session()
        self.addCleanup(db.close)
        self.restore(db, parts=("attendance",))
        self.assertEqual(
            {row.source for row in db.query(AttendanceLog).all()}, {"zktime_restore"}
        )

    def test_the_verify_mode_is_carried_across(self):
        db = self.Session()
        self.addCleanup(db.close)
        self.restore(db, parts=("attendance",))
        row = (db.query(AttendanceLog)
                 .filter_by(user_id="206")
                 .order_by(AttendanceLog.timestamp).first())
        self.assertEqual(row.punch, 4)

    def test_restoring_twice_inserts_nothing_the_second_time(self):
        """The property that makes a re-run after a failure safe.

        It is also the one that broke first: `AttendanceLog.timestamp` reads
        back timezone-aware through UTCDateTime while the file's values are
        naive, so an unnormalised duplicate check matched nothing and the
        second run hit the unique constraint instead of skipping.
        """
        db = self.Session()
        self.addCleanup(db.close)

        first = self.restore(db, parts=("attendance",))
        second = self.restore(db, parts=("attendance",))

        self.assertEqual(first["attendance"]["inserted"], 4)
        self.assertEqual(second["attendance"]["inserted"], 0)
        self.assertEqual(second["attendance"]["already_present"], 4)
        self.assertEqual(db.query(AttendanceLog).count(), 4)

    def test_a_punch_already_pushed_by_the_device_is_not_duplicated(self):
        db = self.Session()
        self.addCleanup(db.close)
        db.add(AttendanceLog(
            device_sn=SN, user_id="201", timestamp=datetime(2024, 6, 1, 8, 1, 2),
            status=0, punch=1, source="adms_push", timezone="Asia/Ho_Chi_Minh",
        ))
        db.commit()

        summary = self.restore(db, parts=("attendance",))
        self.assertEqual(summary["attendance"]["already_present"], 1)
        self.assertEqual(db.query(AttendanceLog).count(), 4)
        # The device's own record keeps its provenance — it was not rewritten.
        kept = db.query(AttendanceLog).filter_by(
            user_id="201", timestamp=datetime(2024, 6, 1, 8, 1, 2)
        ).first()
        self.assertEqual(kept.source, "adms_push")

    def test_an_unreadable_timestamp_is_counted_not_guessed_at(self):
        path = os.path.join(self.tmp.name, "bad_time.db")
        build_backup(path, punches=[(1, 1, "not a date", 0, 0, "1", 1)])
        db = self.Session()
        self.addCleanup(db.close)
        summary = zktime_backup.restore(
            db, zktime_backup.open_backup(path), device_sn=SN,
            terminal_id=1, parts=("attendance",),
        )
        self.assertEqual(summary["attendance"]["unreadable_time"], 1)
        self.assertEqual(db.query(AttendanceLog).count(), 0)

    def test_derived_attendance_is_not_imported(self):
        """att_day_summary is ZKTime's arithmetic, not punches."""
        db = self.Session()
        self.addCleanup(db.close)
        self.restore(db, parts=("attendance",), terminal_id=None)
        self.assertEqual(db.query(AttendanceLog).count(), 5)


class TerminalAdoptionTests(BackupTestCase):
    """Restoring onto a terminal this app has no ``devices`` row for.

    The offline-terminal case, and a common one: a machine that was never
    pointed at this server has never announced itself, so the only record of
    it anywhere is inside the backup being restored.
    """

    def test_a_serial_in_neither_the_app_nor_the_file_is_refused(self):
        """The limit on adopting a terminal: it must be one the file names."""
        db = self.Session()
        self.addCleanup(db.close)
        with self.assertRaises(ZKTimeBackupError) as caught:
            self.restore(db, device_sn="NEVERSEEN", parts=("attendance",))
        self.assertIn("NEVERSEEN", str(caught.exception))
        # And it says what the file does hold, so the operator can pick.
        self.assertIn(SN, str(caught.exception))
        self.assertEqual(db.query(Device).count(), 1)

    def test_a_terminal_in_the_file_is_registered_from_the_file(self):
        """The offline-terminal case: no device row, and no way to go and read
        the serial off the machine, so it comes out of the backup."""
        db = self.Session()
        self.addCleanup(db.close)
        db.query(Device).delete()
        db.commit()

        summary = self.restore(db, parts=("attendance",), created_by="tester")

        self.assertTrue(summary["device_created"])
        device = db.query(Device).filter_by(serial_number=SN).one()
        self.assertEqual(device.name, "WTS Access Control")
        self.assertEqual(device.ip_address, "192.168.79.10")
        self.assertEqual(device.port, 4370)
        # Approved, for the same reason POST /devices approves what an admin
        # types in: a named operator choosing this terminal is the approval.
        self.assertEqual(device.status, "approved")
        self.assertEqual(device.approved_by, "tester")
        self.assertIsNotNone(device.approved_at)
        self.assertEqual(device.timezone, config.DEFAULT_DEVICE_TIMEZONE)
        # And the punches actually landed on it.
        self.assertTrue(db.query(AttendanceLog).filter_by(device_sn=SN).count())

    def test_registering_from_the_file_is_said_out_loud(self):
        db = self.Session()
        self.addCleanup(db.close)
        db.query(Device).delete()
        db.commit()

        summary = self.restore(db, parts=("attendance",), created_by="tester")

        self.assertEqual(summary["device_timezone"], config.DEFAULT_DEVICE_TIMEZONE)
        spoken = " ".join(summary["warnings"])
        self.assertIn(SN, spoken)
        # The timezone is the one that matters: it labels every restored punch.
        self.assertIn(config.DEFAULT_DEVICE_TIMEZONE, spoken)

    def test_an_already_registered_device_is_left_exactly_as_it_was(self):
        db = self.Session()
        self.addCleanup(db.close)

        summary = self.restore(db, parts=("attendance",), created_by="tester")

        self.assertFalse(summary["device_created"])
        device = db.query(Device).filter_by(serial_number=SN).one()
        # The operator's own timezone, not the default, and no re-approval.
        self.assertEqual(device.timezone, "Asia/Ho_Chi_Minh")
        self.assertIsNone(device.approved_by)
        self.assertEqual(db.query(Device).count(), 1)

    def test_a_file_that_records_no_port_falls_back_to_the_sdk_default(self):
        """0 is what ZKTime stores for a terminal it never reached over TCP,
        and 0 is not a port this app can ever connect to."""
        db = self.Session()
        self.addCleanup(db.close)
        db.query(Device).delete()
        db.commit()

        self.path = build_backup(
            os.path.join(self.tmp.name, "noport.db"),
            terminals=[(1, 1, 1, "WTS Access Control", 1, "192.168.79.10",
                        0, 0, SN, 92029)],
        )
        self.restore(db, parts=("attendance",), created_by="tester")

        self.assertEqual(db.query(Device).filter_by(serial_number=SN).one().port, 4370)

    def test_a_bad_part_name_does_not_leave_a_device_behind(self):
        """Validation runs before the device is resolved, now that resolving
        it can create one."""
        db = self.Session()
        self.addCleanup(db.close)
        db.query(Device).delete()
        db.commit()

        with self.assertRaises(ZKTimeBackupError):
            self.restore(db, parts=("attendance", "payroll"))
        self.assertEqual(db.query(Device).count(), 0)


class TemplateRestoreTests(BackupTestCase):

    def test_templates_are_not_in_the_default_set(self):
        db = self.Session()
        self.addCleanup(db.close)
        summary = zktime_backup.restore(db, self.open(), device_sn=SN, terminal_id=1)
        self.assertNotIn("templates", summary)
        self.assertEqual(db.query(BiometricTemplate).count(), 0)

    def test_the_version_string_becomes_the_major_minor_a_command_carries(self):
        db = self.Session()
        self.addCleanup(db.close)
        self.restore(db, parts=("templates",))
        row = db.query(BiometricTemplate).filter_by(user_id="201", no=6).first()
        self.assertEqual((row.majorver, row.minorver), (10, 0))

    def test_a_payload_that_is_not_base64_is_skipped(self):
        db = self.Session()
        self.addCleanup(db.close)
        summary = self.restore(db, parts=("templates",))
        self.assertEqual(summary["templates"]["stored"], 2)
        self.assertEqual(summary["templates"]["skipped"], 1)

    def test_the_payload_is_stored_byte_for_byte(self):
        db = self.Session()
        self.addCleanup(db.close)
        self.restore(db, parts=("templates",))
        row = db.query(BiometricTemplate).filter_by(user_id="201", no=6).first()
        self.assertEqual(row.tmp, "U3F1YXJl")

    def test_a_restored_template_is_not_queued_back_to_the_device(self):
        """source_device_sn is set to the target, which is what stops E4."""
        db = self.Session()
        self.addCleanup(db.close)
        self.restore(db, parts=("templates",))
        self.assertEqual(
            {r.source_device_sn for r in db.query(BiometricTemplate).all()}, {SN}
        )

    def test_a_file_without_the_template_table_says_so_instead_of_failing(self):
        path = os.path.join(self.tmp.name, "no_bio.db")
        build_backup(path)
        conn = sqlite3.connect(path)
        conn.execute("DROP TABLE hr_biotemplate")
        conn.commit()
        conn.close()

        db = self.Session()
        self.addCleanup(db.close)
        summary = zktime_backup.restore(
            db, zktime_backup.open_backup(path), device_sn=SN, parts=("templates",)
        )
        self.assertEqual(summary["templates"]["stored"], 0)
        self.assertIn("hr_biotemplate", summary["templates"]["note"])


class WarningTests(BackupTestCase):

    def test_attendance_without_employees_warns_that_the_screen_will_hide_it(self):
        db = self.Session()
        self.addCleanup(db.close)
        summary = self.restore(db, parts=("attendance",))
        self.assertTrue(
            any("not on the roster" in note for note in summary["warnings"]),
            summary["warnings"],
        )

    def test_no_such_warning_when_the_people_are_restored_too(self):
        db = self.Session()
        self.addCleanup(db.close)
        summary = self.restore(db, parts=("employees", "attendance"))
        self.assertFalse(any("not on the roster" in n for n in summary["warnings"]))

    def test_no_such_warning_when_the_roster_already_holds_them(self):
        db = self.Session()
        self.addCleanup(db.close)
        db.add(Employee(user_id="201", name="A"))
        db.add(Employee(user_id="206", name="B"))
        db.commit()
        summary = self.restore(db, parts=("attendance",))
        self.assertFalse(any("not on the roster" in n for n in summary["warnings"]))

    def test_a_multi_terminal_file_restored_whole_says_so(self):
        db = self.Session()
        self.addCleanup(db.close)
        summary = self.restore(db, parts=("attendance",), terminal_id=None)
        self.assertTrue(
            any("more than one terminal" in n for n in summary["warnings"]),
            summary["warnings"],
        )

    def test_an_unknown_part_is_refused_before_anything_is_written(self):
        db = self.Session()
        self.addCleanup(db.close)
        with self.assertRaises(ZKTimeBackupError):
            self.restore(db, parts=("employees", "payroll"))
        self.assertEqual(db.query(Employee).count(), 0)


# ---------------------------------------------------------------------------
# The HTTP surface
# ---------------------------------------------------------------------------

class RouterTests(BackupTestCase):

    def setUp(self):
        super().setUp()
        self.stage = tempfile.TemporaryDirectory()
        self.addCleanup(self.stage.cleanup)
        self._old_dir = config.BACKUP_STAGE_DIR
        config.BACKUP_STAGE_DIR = self.stage.name
        self.addCleanup(lambda: setattr(config, "BACKUP_STAGE_DIR", self._old_dir))

        # The export template is persistent by design, so a test must never
        # be able to write into a real installation's data directory.
        self.template_home = tempfile.TemporaryDirectory()
        self.addCleanup(self.template_home.cleanup)
        self._old_template = config.BACKUP_TEMPLATE_PATH
        config.BACKUP_TEMPLATE_PATH = os.path.join(self.template_home.name, "tpl.db")
        self.addCleanup(lambda: setattr(config, "BACKUP_TEMPLATE_PATH", self._old_template))

        app = FastAPI()
        app.include_router(backup_router.router)

        def _override_get_db():
            db = self.Session()
            try:
                yield db
            finally:
                db.close()

        admin = User(id=1, username="tester", role="admin", password_hash="x")
        app.dependency_overrides[get_db] = _override_get_db
        app.dependency_overrides[require_admin] = lambda: admin
        app.dependency_overrides[require_auth] = lambda: admin
        self.client = TestClient(app, client=("203.0.113.10", 40000))

    def upload(self):
        with open(self.path, "rb") as handle:
            return self.client.post("/backup/upload", content=handle.read())

    def set_template(self):
        with open(self.path, "rb") as handle:
            response = self.client.post("/backup/template", content=handle.read())
        self.assertEqual(response.status_code, 200, response.text)
        return response

    def test_upload_answers_with_a_token_and_a_preview(self):
        response = self.upload()
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["token"])
        self.assertEqual(body["preview"]["employees"], 3)
        self.assertEqual(body["preview"]["punches"]["total"], 6)

    def test_upload_says_which_terminals_are_registered_here(self):
        terminals = self.upload().json()["preview"]["terminals"]
        by_serial = {t["serial"]: t["registered_here"] for t in terminals}
        self.assertTrue(by_serial[SN])
        self.assertFalse(by_serial[OTHER_SN])

    def test_an_empty_body_is_refused(self):
        response = self.client.post("/backup/upload", content=b"")
        self.assertEqual(response.status_code, 400)
        self.assertIn("empty", response.json()["detail"].lower())

    def test_a_file_that_is_not_a_backup_is_refused_and_not_kept(self):
        response = self.client.post("/backup/upload", content=b"PK\x03\x04 nope")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(os.listdir(self.stage.name), [])

    def test_a_damaged_backup_is_refused_with_a_sentence_and_not_kept(self):
        """Right magic bytes, unreadable contents — a half-finished copy."""
        with open(self.path, "rb") as handle:
            truncated = handle.read(2048)
        response = self.client.post("/backup/upload", content=truncated)
        self.assertEqual(response.status_code, 400)
        self.assertIn("truncated or corrupt", response.json()["detail"])
        self.assertEqual(os.listdir(self.stage.name), [])

    def test_upload_then_restore_writes_the_records(self):
        token = self.upload().json()["token"]
        response = self.client.post("/backup/restore", json={
            "token": token, "device_sn": SN, "terminal_id": 1,
            "parts": ["employees", "attendance"],
        })
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["employees"]["created"], 2)
        self.assertEqual(body["attendance"]["inserted"], 4)

    def test_the_staged_file_is_deleted_once_it_has_been_restored(self):
        token = self.upload().json()["token"]
        self.client.post("/backup/restore", json={"token": token, "device_sn": SN})
        self.assertEqual(os.listdir(self.stage.name), [])

    def test_a_token_that_was_never_staged_is_a_404(self):
        response = self.client.post("/backup/restore", json={
            "token": "a" * 32, "device_sn": SN,
        })
        self.assertEqual(response.status_code, 404)

    def test_a_token_shaped_like_a_path_cannot_escape_the_staging_directory(self):
        for token in ("../../etc/passwd", "..", "a/b", ""):
            response = self.client.post("/backup/restore", json={
                "token": token, "device_sn": SN,
            })
            self.assertIn(response.status_code, (400, 422), token)

    def test_a_serial_in_neither_the_app_nor_the_file_is_a_400_not_a_500(self):
        token = self.upload().json()["token"]
        response = self.client.post("/backup/restore", json={
            "token": token, "device_sn": "NEVERSEEN",
        })
        self.assertEqual(response.status_code, 400)
        self.assertIn("NEVERSEEN", response.json()["detail"])

    def test_restoring_onto_a_terminal_this_app_has_never_seen_registers_it(self):
        db = self.Session()
        self.addCleanup(db.close)
        db.query(Device).delete()
        db.commit()

        token = self.upload().json()["token"]
        response = self.client.post("/backup/restore", json={
            "token": token, "device_sn": SN, "terminal_id": 1,
        })

        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["device_created"])
        self.assertEqual(db.query(Device).filter_by(serial_number=SN).count(), 1)

    def test_the_upload_marks_a_terminal_the_app_has_never_seen(self):
        """What the picker needs to offer it as "will be added"."""
        db = self.Session()
        self.addCleanup(db.close)
        db.query(Device).delete()
        db.commit()

        terminal = next(t for t in self.upload().json()["preview"]["terminals"]
                        if t["serial"] == SN)
        self.assertFalse(terminal["registered_here"])
        # The fields a device is built from come across in the preview too.
        self.assertEqual(terminal["ip_address"], "192.168.79.10")
        self.assertEqual(terminal["port"], 4370)

    def test_a_device_created_by_a_restore_is_audited_as_a_device_create(self):
        """Found on the roster whichever route registered it."""
        db = self.Session()
        self.addCleanup(db.close)
        db.query(Device).delete()
        db.commit()

        token = self.upload().json()["token"]
        self.client.post("/backup/restore", json={"token": token, "device_sn": SN})

        rows = db.query(AuditLog).filter_by(action="device_create").all()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].target, SN)
        self.assertEqual(rows[0].actor, "tester")

    def test_no_device_create_is_audited_when_the_device_was_already_there(self):
        token = self.upload().json()["token"]
        self.client.post("/backup/restore", json={"token": token, "device_sn": SN})

        db = self.Session()
        self.addCleanup(db.close)
        self.assertEqual(db.query(AuditLog).filter_by(action="device_create").count(), 0)

    def test_selecting_nothing_to_restore_is_refused(self):
        token = self.upload().json()["token"]
        response = self.client.post("/backup/restore", json={
            "token": token, "device_sn": SN, "parts": [],
        })
        self.assertEqual(response.status_code, 400)

    def test_templates_are_left_out_unless_asked_for(self):
        token = self.upload().json()["token"]
        body = self.client.post("/backup/restore", json={
            "token": token, "device_sn": SN,
        }).json()
        self.assertNotIn("templates", body)

    def test_discarding_removes_the_staged_file(self):
        token = self.upload().json()["token"]
        self.assertEqual(len(os.listdir(self.stage.name)), 1)
        response = self.client.delete(f"/backup/upload/{token}")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["discarded"])
        self.assertEqual(os.listdir(self.stage.name), [])

    def test_an_expired_upload_is_refused_and_deleted(self):
        token = self.upload().json()["token"]
        path = os.path.join(self.stage.name, f"{token}.db")
        stale = os.path.getmtime(path) - config.BACKUP_STAGE_TTL_SECONDS - 60
        os.utime(path, (stale, stale))

        response = self.client.post("/backup/restore", json={
            "token": token, "device_sn": SN,
        })
        self.assertEqual(response.status_code, 404)
        self.assertIn("expired", response.json()["detail"].lower())
        self.assertFalse(os.path.exists(path))

    def test_a_new_upload_purges_expired_ones(self):
        stale_path = os.path.join(self.stage.name, "x" * 24 + ".db")
        with open(stale_path, "wb") as handle:
            handle.write(b"stale")
        old = os.path.getmtime(stale_path) - config.BACKUP_STAGE_TTL_SECONDS - 60
        os.utime(stale_path, (old, old))

        self.upload()
        self.assertFalse(os.path.exists(stale_path))

    def test_a_staged_upload_is_not_world_readable(self):
        """It holds fingerprint templates and usually lives under /tmp."""
        token = self.upload().json()["token"]
        path = os.path.join(self.stage.name, f"{token}.db")
        self.assertEqual(os.stat(path).st_mode & 0o077, 0)


class BodyLimitTests(unittest.TestCase):
    """MaxBodySizeMiddleware's per-path ceiling.

    A ZKTime backup is an order of magnitude past the global limit, and the
    2 MB default is correct for every other browser-facing route — so the
    exception has to apply to /backup/upload and to nothing else.
    """

    def setUp(self):
        from app.middleware import MaxBodySizeMiddleware
        self.middleware = MaxBodySizeMiddleware(
            app=None, max_bytes=2_000_000,
            overrides={"/backup/upload": 128_000_000,
                       "/backup/template": 128_000_000},
        )

    def test_the_backup_upload_path_gets_the_larger_ceiling(self):
        self.assertEqual(self.middleware._limit_for("/backup/upload"), 128_000_000)

    def test_the_template_upload_gets_it_too(self):
        """It takes the same 19 MB database as /backup/upload."""
        self.assertEqual(self.middleware._limit_for("/backup/template"), 128_000_000)

    def test_every_other_path_keeps_the_global_limit(self):
        # /backup/restore and /backup/export take small JSON; exempting the
        # whole "/backup" prefix would hand them a 128 MB ceiling for nothing.
        for path in ("/devices", "/attendance", "/backup/restore",
                     "/backup/export", "/iclock/cdata"):
            self.assertEqual(self.middleware._limit_for(path), 2_000_000, path)

    def test_the_longest_matching_prefix_wins(self):
        from app.middleware import MaxBodySizeMiddleware
        middleware = MaxBodySizeMiddleware(
            app=None, max_bytes=100,
            overrides={"/a": 200, "/a/b": 300},
        )
        self.assertEqual(middleware._limit_for("/a/b/c"), 300)
        self.assertEqual(middleware._limit_for("/a/x"), 200)


class MigrationTests(unittest.TestCase):
    """Widening attendance_logs.source — the one retype migrations.py performs."""

    def test_widening_is_a_no_op_on_a_freshly_created_schema(self):
        from app.migrations import widen_attendance_source
        engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
        )
        Base.metadata.create_all(bind=engine)
        self.assertFalse(widen_attendance_source(engine))

    def test_widening_does_not_fail_when_the_table_does_not_exist(self):
        from app.migrations import widen_attendance_source
        engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
        )
        self.assertFalse(widen_attendance_source(engine))

    def test_the_column_accepts_the_restore_value(self):
        engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
        )
        Base.metadata.create_all(bind=engine)
        Session = sessionmaker(bind=engine)
        db = Session()
        self.addCleanup(db.close)
        db.add(AttendanceLog(
            device_sn=SN, user_id="201", timestamp=datetime(2024, 1, 1, 9, 0),
            status=0, punch=1, source="zktime_restore", timezone="Asia/Ho_Chi_Minh",
        ))
        db.commit()
        self.assertEqual(db.query(AttendanceLog).first().source, "zktime_restore")


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# Export (F2)
# ---------------------------------------------------------------------------

class ExportTestCase(BackupTestCase):
    """A backup file as the template, and an app holding data to write into it."""

    def setUp(self):
        super().setUp()
        self.out = os.path.join(self.tmp.name, "export.db")
        db = self.Session()
        try:
            db.add(Employee(user_id="201", name="DONG THIEN VU", privilege=3, card="0"))
            db.add(Employee(user_id="206", name="NGUYEN NHA HIEU", privilege=0, card="1234567"))
            for when, pin, status, punch in [
                (datetime(2024, 6, 1, 8, 1, 2), "201", 0, 1),
                (datetime(2024, 6, 1, 8, 5, 44), "206", 0, 4),
                (datetime(2026, 6, 18, 8, 58, 33), "206", 0, 1),
            ]:
                db.add(AttendanceLog(device_sn=SN, user_id=pin, timestamp=when,
                                     status=status, punch=punch, source="zktime_restore",
                                     timezone="Asia/Ho_Chi_Minh"))
            db.add(BiometricTemplate(
                user_id="201", no=6, record_index=0, valid=1, duress=0, type=1,
                majorver=10, minorver=0, format=0, tmp="U3F1YXJl", source_device_sn=SN,
            ))
            db.commit()
        finally:
            db.close()

    def build(self, **kwargs):
        db = self.Session()
        self.addCleanup(db.close)
        return zktime_export.build_export(db, self.path, self.out, **kwargs)

    def out_conn(self):
        conn = sqlite3.connect(self.out)
        self.addCleanup(conn.close)
        return conn

    def rows(self, sql, params=()):
        return self.out_conn().execute(sql, params).fetchall()


class ExportFilenameTests(unittest.TestCase):

    def test_the_date_is_spelled_the_way_zktime_spells_it(self):
        """bak_zktime_20261806.db, whose newest punch is 2026-06-18: YYYYDDMM."""
        self.assertEqual(
            zktime_export.export_filename(datetime(2026, 6, 18)),
            "bak_zktime_20261806.db",
        )

    def test_a_two_digit_day_and_month_keep_their_leading_zeros(self):
        self.assertEqual(
            zktime_export.export_filename(datetime(2027, 1, 5)),
            "bak_zktime_20270501.db",
        )


class ExportLeavesZKTimesOwnTablesAloneTests(ExportTestCase):
    """The whole reason an export is built on a template rather than generated."""

    def test_the_opaque_config_blob_survives_byte_for_byte(self):
        original = self.rows_of_template("SELECT Data FROM Sys_Config")
        self.build()
        self.assertEqual(self.rows("SELECT Data FROM Sys_Config"), original)

    def rows_of_template(self, sql):
        conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        try:
            return conn.execute(sql).fetchall()
        finally:
            conn.close()

    def test_menus_logins_and_shifts_are_untouched(self):
        before = {
            table: self.rows_of_template(f"SELECT * FROM {table} ORDER BY id")
            for table in ("sys_menu", "sys_user", "att_shift", "hr_department")
        }
        self.build()
        for table, expected in before.items():
            self.assertEqual(
                self.rows(f"SELECT * FROM {table} ORDER BY id"), expected, table
            )

    def test_every_table_in_the_template_is_still_there(self):
        before = self.rows_of_template(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        self.build()
        self.assertEqual(
            self.rows("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"),
            before,
        )

    def test_the_template_itself_is_never_modified(self):
        with open(self.path, "rb") as handle:
            before = handle.read()
        self.build()
        with open(self.path, "rb") as handle:
            self.assertEqual(handle.read(), before)


class ExportEmployeeTests(ExportTestCase):

    def test_a_person_is_merged_not_rewritten(self):
        """The columns this app does not model must survive the export.

        `emp_pwd` is the device password and `department_id` files the person
        in ZKTime's org chart. A wipe-and-reinsert would blank both.
        """
        conn = sqlite3.connect(self.path)
        conn.execute(
            "UPDATE hr_employee SET emp_pwd = '6879', department_id = 1, "
            "position_id = 2 WHERE emp_pin = '201'"
        )
        conn.commit()
        conn.close()

        self.build()

        row = self.rows(
            "SELECT emp_pwd, department_id, position_id FROM hr_employee WHERE emp_pin='201'"
        )
        self.assertEqual(row, [("6879", 1, 2)])

    def test_the_name_privilege_and_card_come_from_this_app(self):
        db = self.Session()
        self.addCleanup(db.close)
        db.query(Employee).filter_by(user_id="201").first().name = "RENAMED HERE"
        db.commit()

        zktime_export.build_export(db, self.path, self.out)

        self.assertEqual(
            self.rows("SELECT emp_firstname, emp_privilege, emp_cardNumber "
                      "FROM hr_employee WHERE emp_pin='201'"),
            [("RENAMED HERE", "3", "")],
        )

    def test_the_apps_no_card_placeholder_is_not_written_as_a_card_number(self):
        """Employee.card defaults to "0"; ZKTime spells no-card as ''."""
        self.build()
        self.assertEqual(
            self.rows("SELECT emp_cardNumber FROM hr_employee WHERE emp_pin='201'"),
            [("",)],
        )

    def test_a_card_number_is_written_through(self):
        self.build()
        self.assertEqual(
            self.rows("SELECT emp_cardNumber FROM hr_employee WHERE emp_pin='206'"),
            [("1234567",)],
        )

    def test_a_person_the_template_never_had_is_added(self):
        db = self.Session()
        self.addCleanup(db.close)
        db.add(Employee(user_id="999", name="NEW STARTER", privilege=0, card="0"))
        db.commit()

        summary = zktime_export.build_export(db, self.path, self.out)

        self.assertEqual(summary["employees"]["created"], 1)
        self.assertEqual(
            self.rows("SELECT emp_firstname, department_id FROM hr_employee WHERE emp_pin='999'"),
            [("NEW STARTER", 1)],     # filed in the template's default department
        )

    def test_punches_by_somebody_off_the_roster_get_a_blank_named_stub(self):
        """delete_employee keeps a leaver's punches on purpose."""
        db = self.Session()
        self.addCleanup(db.close)
        db.add(AttendanceLog(device_sn=SN, user_id="777",
                             timestamp=datetime(2024, 7, 1, 9, 0), status=0, punch=1,
                             source="adms_push", timezone="Asia/Ho_Chi_Minh"))
        db.commit()
        # Make sure the template does not already hold this PIN.
        self.assertEqual(
            self.rows_in_template("SELECT COUNT(*) FROM hr_employee WHERE emp_pin='777'"),
            [(0,)],
        )

        summary = zktime_export.build_export(db, self.path, self.out)

        self.assertEqual(summary["employees"]["stubs"], 1)
        # A blank name, not the PIN repeated as one.
        self.assertEqual(
            self.rows("SELECT emp_firstname FROM hr_employee WHERE emp_pin='777'"),
            [("",)],
        )
        # And the punch survived, which is the point of the stub.
        self.assertEqual(
            self.rows("SELECT COUNT(*) FROM att_punches p JOIN hr_employee e "
                      "ON e.id = p.employee_id WHERE e.emp_pin='777'"),
            [(1,)],
        )

    def rows_in_template(self, sql):
        conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        try:
            return conn.execute(sql).fetchall()
        finally:
            conn.close()

    def test_people_the_app_no_longer_has_are_kept_by_default(self):
        """A fresh install must not hand back a backup with the roster emptied."""
        summary = self.build()
        self.assertEqual(summary["employees"]["pruned"], 0)
        # PIN 0 is in the template and in neither the roster nor the punches.
        self.assertEqual(
            self.rows("SELECT COUNT(*) FROM hr_employee WHERE emp_pin='0'"), [(1,)]
        )

    def test_pruning_removes_them_and_their_dependent_rows(self):
        conn = sqlite3.connect(self.path)
        # Person 3 (PIN 206) has a shift and a payroll row in the fixture;
        # move them onto the person about to be pruned so the cascade shows.
        conn.execute("UPDATE att_employee_shift SET employee_id = 2")
        conn.execute("UPDATE pay_empDetail SET employee_id = 2")
        conn.commit()
        conn.close()

        summary = self.build(prune_missing=True)

        self.assertEqual(summary["employees"]["pruned"], 1)
        self.assertEqual(
            self.rows("SELECT COUNT(*) FROM hr_employee WHERE emp_pin='0'"), [(0,)]
        )
        self.assertEqual(self.rows("SELECT COUNT(*) FROM att_employee_shift"), [(0,)])
        self.assertEqual(self.rows("SELECT COUNT(*) FROM pay_empDetail"), [(0,)])

    def test_pruning_never_removes_somebody_who_still_has_punches(self):
        summary = self.build(prune_missing=True)
        self.assertEqual(summary["employees"]["pruned"], 1)   # only PIN 0
        for pin in ("201", "206"):
            self.assertEqual(
                self.rows("SELECT COUNT(*) FROM hr_employee WHERE emp_pin=?", (pin,)),
                [(1,)], pin,
            )


class ExportPunchTests(ExportTestCase):

    def test_punches_are_replaced_in_full_with_this_apps_records(self):
        summary = self.build()
        self.assertEqual(summary["punches"]["written"], 3)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM att_punches"), [(3,)])

    def test_each_punch_points_at_the_right_person(self):
        self.build()
        self.assertEqual(
            self.rows(
                "SELECT e.emp_pin, p.punch_time FROM att_punches p "
                "JOIN hr_employee e ON e.id = p.employee_id ORDER BY p.punch_time"
            ),
            [("201", "2024-06-01 08:01:02"),
             ("206", "2024-06-01 08:05:44"),
             ("206", "2026-06-18 08:58:33")],
        )

    def test_the_wall_clock_goes_out_exactly_as_it_was_stored(self):
        """Stored naive, read back tz-aware through UTCDateTime, written naive."""
        self.build()
        self.assertEqual(
            self.rows("SELECT punch_time FROM att_punches ORDER BY punch_time DESC LIMIT 1"),
            [("2026-06-18 08:58:33",)],
        )

    def test_status_and_verify_mode_map_back_to_workstate_and_verifycode(self):
        self.build()
        self.assertEqual(
            self.rows(
                "SELECT p.workstate, p.verifycode FROM att_punches p "
                "JOIN hr_employee e ON e.id = p.employee_id "
                "WHERE e.emp_pin='206' ORDER BY p.punch_time LIMIT 1"
            ),
            [(0, "4")],
        )

    def test_each_punch_is_filed_under_its_own_terminal(self):
        self.build()
        self.assertEqual(
            self.rows(
                "SELECT DISTINCT t.terminal_sns FROM att_punches p "
                "JOIN att_terminal t ON t.id = p.terminal_id"
            ),
            [(SN,)],
        )

    def test_a_pin_zero_record_is_not_written_as_attendance(self):
        db = self.Session()
        self.addCleanup(db.close)
        db.add(AttendanceLog(device_sn=SN, user_id="0",
                             timestamp=datetime(2024, 7, 1, 9, 0), status=0, punch=1,
                             source="adms_push", timezone="Asia/Ho_Chi_Minh"))
        db.commit()

        summary = zktime_export.build_export(db, self.path, self.out)

        self.assertEqual(summary["punches"]["written"], 3)
        self.assertEqual(summary["punches"]["skipped"], 1)

    def test_the_autoincrement_counter_is_left_where_zktime_can_use_it(self):
        """ZKTime inserts into att_punches on every sync; a stale seq collides."""
        self.build()
        self.assertEqual(
            self.rows("SELECT seq FROM sqlite_sequence WHERE name='att_punches'"),
            [(3,)],
        )

    def test_derived_attendance_is_emptied_rather_than_left_stale(self):
        summary = self.build()
        self.assertEqual(summary["cleared"]["att_day_summary"], 5)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM att_day_summary"), [(0,)])
        self.assertEqual(self.rows("SELECT COUNT(*) FROM att_day_details"), [(0,)])


class ExportTerminalTests(ExportTestCase):

    def test_a_terminal_keeps_the_facts_only_the_template_knows(self):
        conn = sqlite3.connect(self.path)
        conn.execute(
            "UPDATE att_terminal SET terminal_type = 'F7-C/ID', "
            "terminal_punches = 92029 WHERE terminal_sns = ?", (SN,)
        )
        conn.commit()
        conn.close()

        self.build()

        self.assertEqual(
            self.rows("SELECT terminal_type, terminal_punches FROM att_terminal "
                      "WHERE terminal_sns = ?", (SN,)),
            [("F7-C/ID", 92029)],
        )

    def test_the_name_and_address_come_from_this_app(self):
        db = self.Session()
        self.addCleanup(db.close)
        db.query(Device).filter_by(serial_number=SN).first().name = "Renamed Door"
        db.commit()

        zktime_export.build_export(db, self.path, self.out)

        self.assertEqual(
            self.rows("SELECT terminal_name, terminal_tcpip, terminal_port "
                      "FROM att_terminal WHERE terminal_sns = ?", (SN,)),
            [("Renamed Door", "192.168.79.10", 4370)],
        )

    def test_a_device_the_template_never_had_is_added(self):
        db = self.Session()
        self.addCleanup(db.close)
        db.add(Device(serial_number="NEWDEV0001", ip_address="10.0.0.5", port=4370,
                      name="New Gate", status="approved", timezone="Asia/Ho_Chi_Minh"))
        db.commit()

        summary = zktime_export.build_export(db, self.path, self.out)

        self.assertEqual(summary["terminals"]["created"], 1)
        self.assertEqual(
            self.rows("SELECT terminal_name FROM att_terminal WHERE terminal_sns='NEWDEV0001'"),
            [("New Gate",)],
        )

    def test_punches_from_a_deleted_device_still_get_a_terminal(self):
        db = self.Session()
        self.addCleanup(db.close)
        db.add(AttendanceLog(device_sn="GONEDEV0001", user_id="201",
                             timestamp=datetime(2024, 8, 1, 9, 0), status=0, punch=1,
                             source="adms_push", timezone="Asia/Ho_Chi_Minh"))
        db.commit()

        summary = zktime_export.build_export(db, self.path, self.out)

        self.assertEqual(summary["terminals"]["stubs"], 1)
        self.assertEqual(
            self.rows("SELECT COUNT(*) FROM att_punches p JOIN att_terminal t "
                      "ON t.id = p.terminal_id WHERE t.terminal_sns='GONEDEV0001'"),
            [(1,)],
        )


class ExportTemplateTests(ExportTestCase):

    def test_the_version_is_reassembled_from_major_and_minor(self):
        self.build()
        self.assertEqual(
            self.rows("SELECT version, template_no, template_data FROM hr_biotemplate"),
            [("10.0", 6, "U3F1YXJl")],
        )

    def test_sdk_pulled_templates_are_not_converted_and_are_reported(self):
        """pyzk hex is not ZKTime base64, and guessing would enrol nobody."""
        db = self.Session()
        self.addCleanup(db.close)
        db.add(FingerprintTemplate(user_id="201", finger_id=1, valid=1,
                                   template="deadbeef", source_device_sn=SN))
        db.commit()

        summary = zktime_export.build_export(db, self.path, self.out)

        self.assertEqual(summary["templates"]["written"], 1)   # the biodata one only
        self.assertTrue(
            any("pyzk's hex" in note for note in summary["warnings"]), summary["warnings"]
        )


class ExportWarningTests(ExportTestCase):

    def test_an_export_smaller_than_its_template_says_so_with_both_numbers(self):
        """The footgun: att_punches is replaced, so history can go backwards."""
        summary = self.build()
        note = next((n for n in summary["warnings"] if "template you built it" in n), None)
        self.assertIsNotNone(note, summary["warnings"])
        self.assertIn("3 punches", note)     # what this app holds
        self.assertIn("6", note)             # what the template held

    def test_no_such_warning_when_the_app_holds_at_least_as_much(self):
        db = self.Session()
        self.addCleanup(db.close)
        for minute in range(10):
            db.add(AttendanceLog(device_sn=SN, user_id="201",
                                 timestamp=datetime(2025, 1, 1, 9, minute),
                                 status=0, punch=1, source="adms_push",
                                 timezone="Asia/Ho_Chi_Minh"))
        db.commit()

        summary = zktime_export.build_export(db, self.path, self.out)
        self.assertFalse(any("template you built it" in n for n in summary["warnings"]))


class ExportFailureTests(ExportTestCase):

    def test_a_template_that_is_not_a_backup_is_refused_before_anything_is_copied(self):
        bad = os.path.join(self.tmp.name, "bad.db")
        with open(bad, "wb") as handle:
            handle.write(b"PK\x03\x04 not a database")
        db = self.Session()
        self.addCleanup(db.close)
        with self.assertRaises(ZKTimeBackupError):
            zktime_export.build_export(db, bad, self.out)
        self.assertFalse(os.path.exists(self.out))

    def test_a_failure_part_way_leaves_no_plausible_looking_file_behind(self):
        """Half an export is worse than none — it looks like a backup."""
        db = self.Session()
        self.addCleanup(db.close)
        original = zktime_export._replace_punches

        def explode(*args, **kwargs):
            raise RuntimeError("disk full")

        zktime_export._replace_punches = explode
        try:
            with self.assertRaises(RuntimeError):
                zktime_export.build_export(db, self.path, self.out)
        finally:
            zktime_export._replace_punches = original
        self.assertFalse(os.path.exists(self.out))

    def test_a_template_without_a_department_table_still_exports(self):
        conn = sqlite3.connect(self.path)
        conn.execute("DROP TABLE hr_department")
        conn.commit()
        conn.close()

        db = self.Session()
        self.addCleanup(db.close)
        db.add(Employee(user_id="999", name="NEW STARTER"))
        db.commit()

        summary = zktime_export.build_export(db, self.path, self.out)
        self.assertEqual(summary["employees"]["created"], 1)


class ExportRoundTripTests(ExportTestCase):
    """What comes out must be something this app's own reader accepts."""

    def test_the_export_reads_back_as_a_valid_zktime_backup(self):
        self.build()
        conn = zktime_backup.open_backup(self.out)
        self.addCleanup(conn.close)
        preview = zktime_backup.inspect_backup(conn)
        self.assertEqual(preview["punches"]["total"], 3)
        self.assertEqual(preview["punches"]["unresolved"], 0)

    def test_restoring_the_export_into_a_clean_app_reproduces_the_data(self):
        self.build()

        engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
        )
        Base.metadata.create_all(bind=engine)
        fresh = sessionmaker(bind=engine)()
        self.addCleanup(fresh.close)
        fresh.add(Device(serial_number=SN, ip_address="192.168.79.10", port=4370,
                         name="WTS", status="approved", timezone="Asia/Ho_Chi_Minh"))
        fresh.commit()

        conn = zktime_backup.open_backup(self.out)
        self.addCleanup(conn.close)
        summary = zktime_backup.restore(
            fresh, conn, device_sn=SN,
            parts=("employees", "attendance", "templates"),
        )

        self.assertEqual(summary["attendance"]["inserted"], 3)
        self.assertEqual(
            sorted(e.user_id for e in fresh.query(Employee).all()), ["201", "206"]
        )
        # The name and the punch times survived both directions unchanged.
        self.assertEqual(
            fresh.query(Employee).filter_by(user_id="201").first().name, "DONG THIEN VU"
        )
        newest = (fresh.query(AttendanceLog)
                       .order_by(AttendanceLog.timestamp.desc()).first())
        self.assertEqual(newest.timestamp.replace(tzinfo=None),
                         datetime(2026, 6, 18, 8, 58, 33))


class ExportRouterTests(RouterTests):

    def test_export_returns_a_token_and_a_summary(self):
        self.set_template()
        response = self.client.post("/backup/export", json={})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["token"])
        self.assertTrue(body["filename"].startswith("bak_zktime_"))
        self.assertIn("punches", body)

    def test_the_template_is_not_consumed_by_building_an_export(self):
        """Backup is a button that can be pressed twice."""
        self.set_template()
        first = self.client.post("/backup/export", json={}).json()["token"]
        second = self.client.post("/backup/export", json={}).json()["token"]
        self.assertNotEqual(first, second)
        self.assertTrue(os.path.exists(config.BACKUP_TEMPLATE_PATH))

    def test_the_built_file_downloads_as_a_sqlite_database(self):
        self.set_template()
        out_token = self.client.post("/backup/export", json={}).json()["token"]

        response = self.client.get(f"/backup/export/{out_token}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "application/vnd.sqlite3")
        self.assertIn("bak_zktime_", response.headers["content-disposition"])
        self.assertTrue(response.content.startswith(b"SQLite format 3\x00"))

    def test_downloading_does_not_delete_the_file(self):
        """A download that fails half-way is common; the TTL clears it instead."""
        self.set_template()
        out_token = self.client.post("/backup/export", json={}).json()["token"]
        self.client.get(f"/backup/export/{out_token}")
        self.assertEqual(self.client.get(f"/backup/export/{out_token}").status_code, 200)

    def test_a_token_shaped_like_a_path_cannot_be_downloaded(self):
        for token in ("..", "a/b", "x" * 8):
            response = self.client.get(f"/backup/export/{token}")
            self.assertIn(response.status_code, (400, 404), token)

    def test_exporting_with_no_template_explains_why_rather_than_failing(self):
        response = self.client.post("/backup/export", json={})
        self.assertEqual(response.status_code, 409)
        self.assertIn("template", response.json()["detail"].lower())

    def test_a_restore_saves_its_own_file_as_the_template(self):
        """What makes Backup a single button: restore once and it is ready."""
        self.assertFalse(os.path.exists(config.BACKUP_TEMPLATE_PATH))
        token = self.upload().json()["token"]

        body = self.client.post("/backup/restore", json={
            "token": token, "device_sn": SN, "terminal_id": 1,
        }).json()

        self.assertTrue(body["template_saved"])
        self.assertTrue(os.path.exists(config.BACKUP_TEMPLATE_PATH))
        # And Backup now works without being handed anything.
        self.assertEqual(self.client.post("/backup/export", json={}).status_code, 200)

    def test_the_template_endpoint_reports_what_is_held(self):
        self.assertFalse(self.client.get("/backup/template").json()["configured"])
        self.set_template()
        body = self.client.get("/backup/template").json()
        self.assertTrue(body["configured"])
        self.assertTrue(body["usable"])
        self.assertEqual(body["preview"]["employees"], 3)

    def test_a_template_that_is_not_a_backup_is_refused(self):
        response = self.client.post("/backup/template", content=b"PK\x03\x04 nope")
        self.assertEqual(response.status_code, 400)
        self.assertFalse(os.path.exists(config.BACKUP_TEMPLATE_PATH))

    def test_an_unreadable_template_is_reported_rather_than_offered(self):
        self.set_template()
        with open(config.BACKUP_TEMPLATE_PATH, "wb") as handle:
            handle.write(b"SQLite format 3\x00 truncated nonsense")
        body = self.client.get("/backup/template").json()
        self.assertTrue(body["configured"])
        self.assertFalse(body["usable"])
        self.assertTrue(body["problem"])

    def test_the_template_can_be_cleared(self):
        self.set_template()
        self.assertTrue(self.client.delete("/backup/template").json()["removed"])
        self.assertFalse(self.client.get("/backup/template").json()["configured"])

    def test_setting_a_template_leaves_nothing_in_the_staging_directory(self):
        self.set_template()
        self.assertEqual(os.listdir(self.stage.name), [])

    def test_a_staged_template_is_not_world_readable(self):
        self.set_template()
        self.assertEqual(os.stat(config.BACKUP_TEMPLATE_PATH).st_mode & 0o077, 0)


class CorruptFileTests(BackupTestCase):
    """sqlite3.connect is lazy, so damage surfaces on the first query."""

    def test_a_truncated_database_is_an_error_with_a_sentence_not_a_traceback(self):
        path = os.path.join(self.tmp.name, "truncated.db")
        with open(self.path, "rb") as source, open(path, "wb") as target:
            target.write(source.read(2048))   # a header and nothing else
        with self.assertRaises(ZKTimeBackupError) as caught:
            zktime_backup.open_backup(path)
        self.assertIn("truncated or corrupt", str(caught.exception))

    def test_a_file_with_the_right_header_and_junk_after_it_is_refused(self):
        path = os.path.join(self.tmp.name, "junk.db")
        with open(path, "wb") as handle:
            handle.write(b"SQLite format 3\x00" + b"\xff" * 4096)
        with self.assertRaises(ZKTimeBackupError):
            zktime_backup.open_backup(path)


class MessageSplitTests(ExportTestCase):
    """What gets shouted and what gets folded away.

    The split is load-bearing for the UI: warnings are rendered, notes sit
    behind a disclosure. A message in the wrong pile is either noise that
    trains operators to skip the box, or a real problem they never see.
    """

    def test_an_incomplete_backup_warns_rather_than_notes(self):
        db = self.Session()
        self.addCleanup(db.close)
        db.add(FingerprintTemplate(user_id="201", finger_id=1, valid=1,
                                   template="deadbeef", source_device_sn=SN))
        db.commit()

        summary = zktime_export.build_export(db, self.path, self.out)

        self.assertTrue(any("not in this backup" in w for w in summary["warnings"]))
        self.assertFalse(any("not in this backup" in n for n in summary["notes"]))

    def test_a_backup_with_no_fingerprints_at_all_says_so_in_the_sentence(self):
        """0 written and 72 skipped is worse than either number alone."""
        db = self.Session()
        self.addCleanup(db.close)
        db.query(BiometricTemplate).delete()
        db.add(FingerprintTemplate(user_id="201", finger_id=1, valid=1,
                                   template="deadbeef", source_device_sn=SN))
        db.commit()

        summary = zktime_export.build_export(db, self.path, self.out)

        note = next(w for w in summary["warnings"] if "not in this backup" in w)
        self.assertIn("no fingerprints at all", note)

    def test_that_phrase_is_absent_when_some_templates_did_get_written(self):
        db = self.Session()
        self.addCleanup(db.close)
        db.add(FingerprintTemplate(user_id="201", finger_id=1, valid=1,
                                   template="deadbeef", source_device_sn=SN))
        db.commit()

        summary = zktime_export.build_export(db, self.path, self.out)

        note = next(w for w in summary["warnings"] if "not in this backup" in w)
        self.assertNotIn("no fingerprints at all", note)
        self.assertEqual(summary["templates"]["written"], 1)

    def test_routine_arithmetic_is_a_note_not_a_warning(self):
        db = self.Session()
        self.addCleanup(db.close)
        db.add(AttendanceLog(device_sn=SN, user_id="0",
                             timestamp=datetime(2024, 7, 1, 9, 0), status=0, punch=1,
                             source="adms_push", timezone="Asia/Ho_Chi_Minh"))
        db.commit()

        summary = zktime_export.build_export(db, self.path, self.out)

        for phrase in ("PIN 0", "calculated attendance was cleared"):
            self.assertTrue(any(phrase in n for n in summary["notes"]), phrase)
            self.assertFalse(any(phrase in w for w in summary["warnings"]), phrase)

    def test_a_restore_splits_its_messages_the_same_way(self):
        db = self.Session()
        self.addCleanup(db.close)
        # This base class seeds the roster, which is what suppresses the
        # hidden-punches warning — clear it so the warning is in play.
        db.query(Employee).delete()
        db.commit()
        summary = zktime_backup.restore(
            db, self.open(), device_sn=SN, terminal_id=1, parts=("attendance",)
        )
        # Hidden punches bite; ZKTime's own totals being skipped does not.
        self.assertTrue(any("not on the roster" in w for w in summary["warnings"]))
        self.assertTrue(any("shift engine" in n for n in summary["notes"]))
        self.assertFalse(any("shift engine" in w for w in summary["warnings"]))

    def test_every_message_has_a_translation_code_in_the_same_position(self):
        """The UI swaps each English line for warning_codes[i] / note_codes[i];
        a list that drifts out of step would put one sentence's text under
        another's meaning, so the two must pair up one to one."""
        db = self.Session()
        self.addCleanup(db.close)
        db.add(FingerprintTemplate(user_id="201", finger_id=1, valid=1,
                                   template="deadbeef", source_device_sn=SN))
        db.add(AttendanceLog(device_sn=SN, user_id="0",
                             timestamp=datetime(2024, 7, 1, 9, 0), status=0, punch=1,
                             source="adms_push", timezone="Asia/Ho_Chi_Minh"))
        db.commit()
        export = zktime_export.build_export(db, self.path, self.out)
        db.query(Employee).delete()
        db.commit()
        restore = zktime_backup.restore(
            db, self.open(), device_sn=SN, terminal_id=1, parts=("attendance",)
        )
        for summary in (export, restore):
            self.assertTrue(summary["warnings"] and summary["notes"], summary)
            self.assertEqual(len(summary["warning_codes"]), len(summary["warnings"]))
            self.assertEqual(len(summary["note_codes"]), len(summary["notes"]))
            for entry in summary["warning_codes"] + summary["note_codes"]:
                self.assertTrue(entry["code"].startswith("backup."), entry)

    def test_both_keys_are_always_present_so_the_ui_never_branches_on_absence(self):
        db = self.Session()
        self.addCleanup(db.close)
        export = zktime_export.build_export(db, self.path, self.out)
        restore = zktime_backup.restore(
            db, self.open(), device_sn=SN, terminal_id=1,
            parts=("employees", "attendance"),
        )
        for summary in (export, restore):
            self.assertIsInstance(summary["warnings"], list)
            self.assertIsInstance(summary["notes"], list)


class SdkTemplateOverlapTests(ExportTestCase):
    """The same finger often sits in both template tables.

    `biometric_templates` is filled by the device's own biodata push or by a
    restore; `fingerprint_templates` by an SDK pull. An installation that has
    done both holds each finger twice. Counting the SDK table whole made a
    backup containing every fingerprint report "72 not exported" — a false
    alarm loud enough to train an operator to ignore the box.
    """

    def test_a_finger_already_in_the_file_is_not_reported_missing(self):
        db = self.Session()
        self.addCleanup(db.close)
        # The fixture exports one fingerprint: pin 201, finger 6. The SDK
        # table holding the same finger is a duplicate, not a gap.
        db.add(FingerprintTemplate(user_id="201", finger_id=6, valid=1,
                                   template="deadbeef", source_device_sn=SN))
        db.commit()

        summary = zktime_export.build_export(db, self.path, self.out)

        self.assertEqual(summary["templates"]["written"], 1)
        self.assertFalse(
            any("not in this backup" in w for w in summary["warnings"]),
            summary["warnings"],
        )

    def test_a_finger_only_the_sdk_has_is_still_reported(self):
        db = self.Session()
        self.addCleanup(db.close)
        db.add(FingerprintTemplate(user_id="201", finger_id=6, valid=1,
                                   template="dup", source_device_sn=SN))
        db.add(FingerprintTemplate(user_id="201", finger_id=2, valid=1,
                                   template="only", source_device_sn=SN))
        db.commit()

        summary = zktime_export.build_export(db, self.path, self.out)

        note = next(w for w in summary["warnings"] if "not in this backup" in w)
        # One missing, not two: finger 6 is in the file.
        self.assertIn("1 SDK-pulled", note)
        self.assertIn("1 person/people", note)

    def test_a_finger_of_somebody_not_in_the_file_is_reported(self):
        db = self.Session()
        self.addCleanup(db.close)
        db.add(FingerprintTemplate(user_id="999", finger_id=0, valid=1,
                                   template="orphan", source_device_sn=SN))
        db.commit()

        summary = zktime_export.build_export(db, self.path, self.out)

        self.assertTrue(any("not in this backup" in w for w in summary["warnings"]))

    def test_nothing_is_said_when_the_sdk_table_is_empty(self):
        summary = self.build()
        self.assertFalse(any("SDK-pulled" in w for w in summary["warnings"]))
        self.assertFalse(any("SDK-pulled" in n for n in summary["notes"]))
