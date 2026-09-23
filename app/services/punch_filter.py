"""Which stored punches describe a person, and which describe nobody.

Two kinds of row reach ``attendance_logs`` without belonging to anybody on the
roster, and they are not the same problem:

* **Records with PIN 0.** A terminal writes one when nothing resolved to a
  person — a start-up, a door sensor, or, far more often on this installation,
  somebody whose face or finger simply did not match. On the live database
  that is 13,185 of 96,345 rows on the one device, and 90% of them sit a few
  seconds *before* the same person's successful punch: the retry. They cluster
  at 08-09h, 12-13h and 17-18h, which is what a queue at the terminal looks
  like. Not one of them is attendance.

  The ADMS push path has dropped these since it was written (``_is_person`` in
  app/routers/adms.py, which now calls this module). The SDK pull path did
  not, and SDK pull is the only path this installation uses — which is how
  13,185 of them got in, and why the Attendance screen showed thousands of
  rows for an employee called "0".

* **Punches belonging to somebody since deleted.** ``delete_employee`` removes
  the roster row and deliberately leaves the punches, because a punch is
  historical fact and payroll has already been run off it. What is left is a
  record with a bare PIN and no name anywhere to resolve it against.

They are filtered differently, on purpose:

* PIN 0 is **not attendance at all**, so it is refused at ingest and excluded
  from every query that reads the table, the Excel timesheet included.
* A leaver's punches **are** attendance. They are hidden from the screen,
  where an unidentifiable number is only noise, and kept in the timesheet,
  where dropping somebody who resigned on the 12th would quietly take half a
  month of pay off the sheet HR reconciles against payroll.

Both exclusions are defaults, not erasures: nothing is deleted, and
``include_hidden`` on the attendance endpoint puts every row back.
"""

# The widest PIN the column holds (`AttendanceLog.user_id` is String(24)).
_PIN_LIMIT = 24


def is_person_pin(pin) -> bool:
    """Does this record describe a person, rather than the device itself?

    The rule, unchanged from the ADMS parser it was lifted out of: a PIN that
    is empty or numerically zero is nobody; anything else — including a
    non-numeric PIN — is somebody.

    Deliberately keyed on the PIN and nothing else. Which verify modes or
    event codes mean "a real punch" on this firmware is not established, so
    filtering on a guessed allow-list would drop real attendance silently and
    permanently. Every PIN-0 row observed here carries verify mode 201, but
    that is evidence for the reading, not the test — a wrong guess about 201
    would cost real punches, while a wrong guess about PIN 0 costs at most a
    visible, correctable row.
    """
    pin = "" if pin is None else str(pin).strip()
    if not pin:
        return False
    try:
        return int(pin) != 0
    except ValueError:
        return True       # a non-numeric PIN is still a person


# `is_person_pin` as a value set, for the SQL side of the same rule.
#
# A list rather than a regular expression or a CAST: this app runs on MariaDB,
# MySQL, PostgreSQL and MSSQL, which agree on `NOT IN` and on very little
# else — REGEXP is MySQL's, `~` is PostgreSQL's, and casting a VARCHAR that
# may hold a non-numeric PIN to an integer is an error on two of the four.
#
# Complete for what a terminal can actually write: every run of zeros the
# column can hold, plus the empty string. The signed spellings int() would
# also read as zero ("-0", "+0") are not generated — no ZKTeco firmware emits
# them — and `is_person_pin` remains the authority wherever a Python value is
# in hand.
NON_PERSON_PINS = frozenset(["", *("0" * n for n in range(1, _PIN_LIMIT + 1))])
