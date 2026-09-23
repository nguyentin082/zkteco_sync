"""Tests for `derived_status` on GET /attendance — what a punch *means*.

Run with the standard library only, alongside the rest:

    python -m unittest discover -s tests -v

The device's `status` column cannot say which punch was the arrival. It is
written by the mode key on the terminal, nobody presses the mode key, and on
the live installation 96,267 of 96,340 records carry status=1, so the table
used to label every single row "Check Out". The label is therefore derived
from the clock, by the same first/last rule the payroll export scores by.

What is worth guarding here is not that a label appears but that it survives
the four things that would quietly make it wrong:

  1. Pagination. The page is fifty rows of a descending query; a day's first
     punch may sit on the next page, and labelling from the page alone would
     call the same punch both arrival and departure.
  2. The filter's clock. A filter starting at 10:00 must not promote a 10:05
     punch to "check-in" for somebody who has been at work since 08:20.
  3. The device filter. In at the main door, out at the warehouse is one
     arrival and one departure, not two of each.
  4. Half a record. One punch says nothing about which half of the day it
     belongs to, and the export already treats such a day as absent — the
     badge must say the same thing rather than invent the missing punch.

Everything runs against a throwaway in-memory SQLite database.
"""

import os
import unittest
from datetime import datetime, time

# Set before importing anything from `app` — see tests/test_adms.py for why.
os.environ.setdefault("APP_ENV", "development")
os.environ.setdefault("SECRET_KEY", "x" * 48)
os.environ.setdefault("DEFAULT_DEVICE_TIMEZONE", "Asia/Dubai")

from fastapi import FastAPI                                    # noqa: E402
from fastapi.testclient import TestClient                      # noqa: E402
from sqlalchemy import create_engine                           # noqa: E402
from sqlalchemy.orm import sessionmaker                        # noqa: E402
from sqlalchemy.pool import StaticPool                         # noqa: E402

from app import config                                         # noqa: E402
from app.database import Base, get_db                          # noqa: E402
from app.deps import require_auth                              # noqa: E402
from app.models import AttendanceLog, Device, Employee, User    # noqa: E402
from app.routers import attendance as attendance_router        # noqa: E402
from app.services import attendance_pairing as pairing         # noqa: E402

MAIN_SN = "STATUSDEV0001"
OTHER_SN = "STATUSDEV0002"

# The status every punch on the live installation carries, because nobody
# presses the mode key. Used throughout so these tests exercise the data as it
# really arrives rather than an idealised in/out alternation.
UNPRESSED = 1


class StatusTestCase(unittest.TestCase):
    """A fresh in-memory database, a signed-in operator and a client."""

    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(bind=self.engine)
        self.Session = sessionmaker(bind=self.engine, autocommit=False, autoflush=False)

        app = FastAPI()
        app.include_router(attendance_router.router)

        def _override_get_db():
            db = self.Session()
            try:
                yield db
            finally:
                db.close()

        operator = User(id=1, username="tester", role="viewer", password_hash="x")
        app.dependency_overrides[get_db] = _override_get_db
        app.dependency_overrides[require_auth] = lambda: operator
        self.client = TestClient(app, client=("203.0.113.10", 40000))

        db = self.Session()
        try:
            db.add(Device(serial_number=MAIN_SN, ip_address="203.0.113.10", port=4370,
                          name="Main Door", status="approved", timezone="Asia/Dubai"))
            db.add(Device(serial_number=OTHER_SN, ip_address="203.0.113.11", port=4370,
                          name="Warehouse", status="approved", timezone="Asia/Dubai"))
            db.add(Employee(user_id="1001", name="Nguyen Van A"))
            db.add(Employee(user_id="1002", name="Tran Thi B"))
            db.commit()
        finally:
            db.close()

    def tearDown(self):
        self.client.close()
        Base.metadata.drop_all(bind=self.engine)
        self.engine.dispose()

    # -- helpers ---------------------------------------------------------

    def punch(self, moment, user_id="1001", sn=MAIN_SN, status=UNPRESSED):
        db = self.Session()
        try:
            db.add(AttendanceLog(device_sn=sn, user_id=user_id, timestamp=moment,
                                 status=status, punch=1, source="adms_push",
                                 timezone="Asia/Dubai"))
            db.commit()
        finally:
            db.close()

    def listed(self, **params):
        res = self.client.get("/attendance", params=params)
        self.assertEqual(res.status_code, 200)
        return res.json()["items"]

    def labels(self, **params):
        """The page's rows as {"HH:MM:SS": derived_status}, newest first."""
        return {
            row["timestamp"][11:]: row["derived_status"]
            for row in self.listed(**params)
        }


class OrdinaryDayTests(StatusTestCase):
    """The shape almost every real day has."""

    def test_first_is_the_arrival_and_last_is_the_departure(self):
        self.punch(datetime(2026, 9, 21, 8, 20, 11))
        self.punch(datetime(2026, 9, 21, 17, 40, 3))

        self.assertEqual(self.labels(), {
            "17:40:03": "check_out",
            "08:20:11": "check_in",
        })

    def test_the_punches_in_between_are_neither(self):
        """Three or more punches a day happen on 224 of 850 real day-records."""
        self.punch(datetime(2026, 9, 21, 8, 20))
        self.punch(datetime(2026, 9, 21, 12, 2))
        self.punch(datetime(2026, 9, 21, 13, 5))
        self.punch(datetime(2026, 9, 21, 17, 40))

        self.assertEqual(self.labels(), {
            "17:40:00": "check_out",
            "13:05:00": "interim",
            "12:02:00": "interim",
            "08:20:00": "check_in",
        })

    def test_the_device_status_is_still_reported_untouched(self):
        """The badge is derived; the record is not rewritten to match it.

        That column is wrong on this installation, which is the whole reason
        `derived_status` exists — but it is what the terminal sent, and an
        operator has to be able to see it to check us against.
        """
        self.punch(datetime(2026, 9, 21, 8, 20))
        self.punch(datetime(2026, 9, 21, 17, 40))

        for row in self.listed():
            self.assertEqual(row["status"], UNPRESSED)

    def test_each_person_gets_their_own_day(self):
        """One person's early punch must not become another's arrival."""
        self.punch(datetime(2026, 9, 21, 8, 20), user_id="1001")
        self.punch(datetime(2026, 9, 21, 17, 40), user_id="1001")
        self.punch(datetime(2026, 9, 21, 9, 5), user_id="1002")
        self.punch(datetime(2026, 9, 21, 18, 30), user_id="1002")

        self.assertEqual(self.labels(user_id="1002"), {
            "18:30:00": "check_out",
            "09:05:00": "check_in",
        })

    def test_a_new_day_starts_a_new_pair(self):
        self.punch(datetime(2026, 9, 21, 8, 20))
        self.punch(datetime(2026, 9, 21, 17, 40))
        self.punch(datetime(2026, 9, 22, 8, 25))
        self.punch(datetime(2026, 9, 22, 17, 35))

        rows = self.listed()
        self.assertEqual(
            [(r["timestamp"], r["derived_status"]) for r in rows],
            [
                ("2026-09-22 17:35:00", "check_out"),
                ("2026-09-22 08:25:00", "check_in"),
                ("2026-09-21 17:40:00", "check_out"),
                ("2026-09-21 08:20:00", "check_in"),
            ],
        )


class HalfARecordTests(StatusTestCase):
    """66 of 850 real day-records hold exactly one punch."""

    def test_a_lone_morning_punch_is_an_arrival_with_no_departure(self):
        self.punch(datetime(2026, 9, 21, 8, 12))
        self.assertEqual(self.labels(), {"08:12:00": "in_only"})

    def test_a_lone_evening_punch_is_a_departure_with_no_arrival(self):
        self.punch(datetime(2026, 9, 21, 17, 41))
        self.assertEqual(self.labels(), {"17:41:00": "out_only"})

    def test_punches_inside_one_minute_are_one_event_not_a_pair(self):
        """Somebody touching the terminal twice on their way in.

        There is no span of work there, and the export already scores such a
        day as absent. Labelling one of them "check-out" would put a whole
        day's hours on screen that the timesheet refuses to pay.
        """
        self.punch(datetime(2026, 9, 21, 8, 20, 1))
        self.punch(datetime(2026, 9, 21, 8, 20, 44))

        self.assertEqual(self.labels(), {
            "08:20:44": "in_only",
            "08:20:01": "in_only",
        })

    def test_the_split_follows_the_configured_shift(self):
        """The midpoint is derived, so a site on another shift follows along.

        On 08:30–17:30 the boundary is 13:00 and an 11:00 punch is an arrival.
        Move the shift to 03:00–11:00 and the same punch is a departure,
        without anybody editing a second setting.
        """
        self.punch(datetime(2026, 9, 21, 11, 0))
        self.assertEqual(self.labels(), {"11:00:00": "in_only"})

        original = (config.WORK_SHIFT_START, config.WORK_SHIFT_END)
        config.WORK_SHIFT_START, config.WORK_SHIFT_END = time(3, 0), time(11, 0)
        try:
            self.assertEqual(self.labels(), {"11:00:00": "out_only"})
        finally:
            config.WORK_SHIFT_START, config.WORK_SHIFT_END = original

    def test_a_night_shift_reads_as_the_terminal_recorded_it(self):
        """The day is the calendar date on the device's own clock.

        A shift crossing midnight is a departure on one date and an arrival on
        the next, which is what the device stored and what the export reads.
        Documented here so the behaviour is a decision and not a surprise.
        """
        self.punch(datetime(2026, 9, 21, 23, 50))
        self.punch(datetime(2026, 9, 22, 0, 10))

        rows = self.listed()
        self.assertEqual(
            [(r["timestamp"], r["derived_status"]) for r in rows],
            [
                ("2026-09-22 00:10:00", "in_only"),
                ("2026-09-21 23:50:00", "out_only"),
            ],
        )


class WhatThePageCannotSeeTests(StatusTestCase):
    """The label is built from the whole day, not from the rows on screen."""

    def test_a_days_pair_split_across_pages_is_still_a_pair(self):
        """The bug a page-local implementation would have.

        With one row per page, the arrival and the departure never appear
        together — and labelling from the page alone would make the single row
        on each page both the first and the last punch of its day.
        """
        self.punch(datetime(2026, 9, 21, 8, 20))
        self.punch(datetime(2026, 9, 21, 17, 40))

        self.assertEqual(self.labels(limit=1, offset=0), {"17:40:00": "check_out"})
        self.assertEqual(self.labels(limit=1, offset=1), {"08:20:00": "check_in"})

    def test_a_from_date_cut_does_not_promote_a_midday_punch(self):
        """Somebody at work since 08:20 did not arrive at 12:05."""
        self.punch(datetime(2026, 9, 21, 8, 20))
        self.punch(datetime(2026, 9, 21, 12, 5))
        self.punch(datetime(2026, 9, 21, 17, 40))

        self.assertEqual(self.labels(from_date="2026-09-21T10:00:00"), {
            "17:40:00": "check_out",
            "12:05:00": "interim",
        })

    def test_a_to_date_cut_does_not_demote_the_real_departure(self):
        self.punch(datetime(2026, 9, 21, 8, 20))
        self.punch(datetime(2026, 9, 21, 12, 5))
        self.punch(datetime(2026, 9, 21, 17, 40))

        self.assertEqual(self.labels(to_date="2026-09-21T13:00:00"), {
            "12:05:00": "interim",
            "08:20:00": "check_in",
        })

    def test_in_at_one_door_and_out_at_another_is_one_pair(self):
        """Filtering by device must not give the person two arrivals.

        They badged in at the main door and out at the warehouse. Seen through
        the main-door filter that is still the day's arrival, not a day with a
        missing check-out.
        """
        self.punch(datetime(2026, 9, 21, 8, 20), sn=MAIN_SN)
        self.punch(datetime(2026, 9, 21, 17, 40), sn=OTHER_SN)

        self.assertEqual(self.labels(device_sn=MAIN_SN), {"08:20:00": "check_in"})
        self.assertEqual(self.labels(device_sn=OTHER_SN), {"17:40:00": "check_out"})

    def test_an_empty_page_asks_the_database_nothing_further(self):
        self.assertEqual(self.listed(), [])
        self.assertEqual(self.labels(user_id="does-not-exist"), {})


class SharedWithTheExportTests(StatusTestCase):
    """The screen and the timesheet must agree on what a pair is.

    Both go through `is_paired`, which is why it lives in
    app/services/attendance_pairing.py rather than in either caller.
    """

    def test_the_export_and_the_badge_use_the_same_pairing_rule(self):
        cases = [
            # (first, last, punches, paired)
            (datetime(2026, 9, 21, 8, 20), datetime(2026, 9, 21, 17, 40), 2, True),
            (datetime(2026, 9, 21, 8, 20), datetime(2026, 9, 21, 8, 20), 1, False),
            # Same minute, different second: no span to pay.
            (datetime(2026, 9, 21, 8, 20, 1), datetime(2026, 9, 21, 8, 20, 44), 2, False),
        ]
        for first, last, punches, expected in cases:
            with self.subTest(first=first, last=last, punches=punches):
                self.assertEqual(pairing.is_paired(first, last, punches), expected)
                scored = attendance_router._score_day(first, last, punches, True)
                self.assertEqual(scored["paired"], expected)

    def test_a_same_minute_day_is_absent_on_the_sheet_and_unpaired_on_screen(self):
        self.punch(datetime(2026, 9, 21, 8, 20, 1))
        self.punch(datetime(2026, 9, 21, 8, 20, 44))

        self.assertEqual(set(self.labels().values()), {"in_only"})

        scored = attendance_router._score_day(
            datetime(2026, 9, 21, 8, 20, 1), datetime(2026, 9, 21, 8, 20, 44), 2, True)
        self.assertFalse(scored["paired"])
        self.assertEqual(scored["actual"], 0)


if __name__ == "__main__":
    unittest.main()
