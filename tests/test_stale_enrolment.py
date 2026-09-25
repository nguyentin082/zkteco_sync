"""A `device_employees` link that the terminal itself no longer backs up.

The situation these guard: two servers (dev and staging) share one terminal
but not a database. Dev removes a person from the door; staging still holds
the link. Before the fix, staging could neither Remove them (the SDK delete
was sent by the stale uid and refused, or worse, hit whoever had been given
that uid since) nor clear the link by syncing, and so could not delete the
employee either (409 "still enrolled").

Runs against in-memory SQLite and a fake SDK connection — no device is dialled.

    python -m unittest tests.test_stale_enrolment -v
"""

import os
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("APP_ENV", "development")
os.environ.setdefault("SECRET_KEY", "x" * 48)

from fastapi import FastAPI                                    # noqa: E402
from fastapi.testclient import TestClient                      # noqa: E402
from sqlalchemy import create_engine                           # noqa: E402
from sqlalchemy.orm import sessionmaker                        # noqa: E402
from sqlalchemy.pool import StaticPool                         # noqa: E402

from app.database import Base, get_db                          # noqa: E402
from app.deps import require_admin, require_auth               # noqa: E402
from app.models import Device, DeviceEmployee, Employee, User  # noqa: E402
from app.routers import devices as devices_router              # noqa: E402
from app.services import poller                                # noqa: E402

SN = "ATT0000000001"


def _user(user_id, uid, name=""):
    return SimpleNamespace(user_id=user_id, uid=uid, name=name, privilege=0, card=0)


class FakeConn:
    def __init__(self, users):
        self.users = list(users)
        self.deleted = []

    def get_users(self):
        return list(self.users)

    def delete_user(self, uid=0, user_id=""):
        self.deleted.append(uid)
        self.users = [u for u in self.users if u.uid != uid]

    def disconnect(self):
        pass


class StaleEnrolmentTestCase(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(bind=self.engine)
        self.Session = sessionmaker(bind=self.engine, autocommit=False, autoflush=False)

        app = FastAPI()
        app.include_router(devices_router.router)

        def _override_get_db():
            db = self.Session()
            try:
                yield db
            finally:
                db.close()

        admin = User(id=1, username="tester", role="admin", password_hash="x")
        app.dependency_overrides[get_db] = _override_get_db
        app.dependency_overrides[require_auth] = lambda: admin
        app.dependency_overrides[require_admin] = lambda: admin
        self.client = TestClient(app)

        db = self.Session()
        try:
            db.add(Device(serial_number=SN, ip_address="192.0.2.10", port=4370,
                          name="Front Door", status="approved", protocol="att"))
            db.add(Employee(user_id="1001", name="Removed On Dev"))
            db.add(Employee(user_id="1002", name="Still Here"))
            # Staging's belief: 1001 sits in uid slot 5. Dev has since removed
            # them and pushed 1002 into the freed slot.
            db.add(DeviceEmployee(device_sn=SN, user_id="1001", uid=5))
            db.add(DeviceEmployee(device_sn=SN, user_id="1002", uid=6))
            db.commit()
        finally:
            db.close()

    def tearDown(self):
        self.client.close()
        Base.metadata.drop_all(bind=self.engine)
        self.engine.dispose()

    def _linked(self):
        db = self.Session()
        try:
            return sorted(r.user_id for r in db.query(DeviceEmployee).filter_by(device_sn=SN))
        finally:
            db.close()

    def _patch_connection(self, conn):
        @contextmanager
        def fake_connection(device):
            yield conn
        return mock.patch.object(devices_router, "device_connection", fake_connection)

    # -- Remove ---------------------------------------------------------------

    def test_remove_of_a_person_already_gone_clears_the_link_and_deletes_nobody(self):
        conn = FakeConn([_user("1002", 5)])
        with self._patch_connection(conn):
            r = self.client.delete(f"/devices/{SN}/users/1001")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["message_code"], "revoke_already_absent")
        # The stale uid 5 now belongs to 1002 — it must not have been deleted.
        self.assertEqual(conn.deleted, [])
        self.assertEqual(self._linked(), ["1002"])

    def test_remove_deletes_by_the_uid_the_device_reports_not_the_stored_one(self):
        conn = FakeConn([_user("1001", 9), _user("1002", 6)])
        with self._patch_connection(conn):
            r = self.client.delete(f"/devices/{SN}/users/1001")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["message_code"], "revoke_removed")
        self.assertEqual(conn.deleted, [9])
        self.assertEqual(self._linked(), ["1002"])

    # -- Sync employees -------------------------------------------------------

    def _pull(self, conn):
        with mock.patch.object(poller, "SessionLocal", self.Session), \
                mock.patch.object(poller, "_connect", lambda device: conn):
            return poller.pull_employees(SN)

    def test_sync_clears_links_the_device_no_longer_backs(self):
        result = self._pull(FakeConn([_user("1002", 5)]))
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["unlinked"], ["1001"])
        self.assertEqual(self._linked(), ["1002"])

    def test_an_empty_read_clears_nothing(self):
        result = self._pull(FakeConn([]))
        self.assertEqual(result["unlinked"], [])
        self.assertEqual(self._linked(), ["1001", "1002"])


if __name__ == "__main__":
    unittest.main()
