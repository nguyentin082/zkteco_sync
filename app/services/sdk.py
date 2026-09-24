from contextlib import contextmanager

from zk import ZK, const
from zk.exception import ZKErrorConnection, ZKErrorResponse, ZKNetworkError

from app.database import SessionLocal
from app.models import Device, DeviceEmployee
from app.errors import AppError


def _connect(zk_instance: ZK):
    """Connect, translating a wrong comm key into a clear 4xx.

    pyzk's connect() raises ZKErrorResponse for two very different situations
    and does not distinguish them by exception type — only by message: a
    rejected comm key ("Unauthenticated", raised when the device answers
    CMD_AUTH with CMD_ACK_UNAUTH) versus any other malformed handshake
    response. Only the former names the comm key; anything else is left to
    the caller's existing ZKErrorConnection/ZKNetworkError handling, which
    still surfaces as the generic "could not connect" 503 — that path really
    is a network problem, not a credential one.
    """
    try:
        return zk_instance.connect()
    except ZKErrorResponse as exc:
        if str(exc) == "Unauthenticated":
            raise AppError("device.comm_key_refused",
                status_code=403,
                detail="Device refused the connection — the configured comm key is likely wrong",
            )
        raise


@contextmanager
def device_connection(device: Device):
    """
    Context manager that opens a pyzk connection and guarantees disconnect on exit.
    Usage:
        with device_connection(device) as conn:
            conn.get_users()
    """
    zk_instance = ZK(
        device.ip_address,
        port=device.port,
        timeout=30,
        password=device.comm_key or 0,
        force_udp=bool(device.force_udp),
        verbose=False,
    )
    conn = _connect(zk_instance)
    try:
        yield conn
    finally:
        try:
            conn.disconnect()
        except Exception:
            pass


def write_user(conn, emp, existing):
    """Write one employee's record to a connected `att` device.

    Returns ``(uid, written)``. ``existing`` is that person's pyzk User from
    this device's get_users(), or None when the device does not hold them.

    Skips the write when the device already holds exactly what the DB says:
    on slow firmware each write is two round trips (USER_WRQ + REFRESHDATA),
    and a bulk push is mostly people who are already there.

    set_user replaces the whole record, and the DB has no column for the
    device-side password or group, so those are carried over from what the
    device holds rather than blanked. Fingerprints are keyed by uid and are
    untouched either way because the uid is reused.
    """
    # pyzk writes anything other than USER_DEFAULT/USER_ADMIN as USER_DEFAULT,
    # so compare against what would actually land on the device.
    privilege = emp.privilege if emp.privilege in (const.USER_DEFAULT, const.USER_ADMIN) else const.USER_DEFAULT
    card = int(emp.card) if emp.card and emp.card != "0" else 0

    if (
        existing is not None
        and existing.name == emp.name
        and existing.privilege == privilege
        and existing.card == card
    ):
        return existing.uid, False

    uid = existing.uid if existing else None
    pre_uid = conn.next_uid  # pyzk assigns this when uid is None
    conn.set_user(
        uid=uid,
        name=emp.name,
        privilege=privilege,
        password=(existing.password if existing else "") or "",
        group_id=(existing.group_id if existing else "") or "",
        user_id=emp.user_id,
        card=card,
    )
    return (uid if uid is not None else pre_uid), True


def enroll_user_task(serial_number: str, user_id: str, finger_id: int) -> None:
    """
    Background task: tells the device to start a live fingerprint enrollment session.
    Blocks up to ~3 minutes waiting for the person to scan their finger 3 times.
    Creates its own DB session because it runs after the HTTP request has ended.
    """
    db = SessionLocal()
    try:
        device = db.query(Device).filter_by(serial_number=serial_number).first()
        if not device:
            return
        de = db.query(DeviceEmployee).filter_by(device_sn=serial_number, user_id=user_id).first()
        if not de:
            return

        zk_instance = ZK(
            device.ip_address,
            port=device.port,
            timeout=60,
            password=device.comm_key or 0,
            force_udp=bool(device.force_udp),
            verbose=False,
        )
        conn = None
        try:
            conn = zk_instance.connect()
            conn.enroll_user(uid=de.uid, temp_id=finger_id, user_id=user_id)
        except Exception:
            pass
        finally:
            if conn:
                try:
                    conn.disconnect()
                except Exception:
                    pass
    finally:
        db.close()
