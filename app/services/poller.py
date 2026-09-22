import logging
from datetime import datetime, timezone
from zk import ZK
from zk.exception import ZKErrorConnection, ZKErrorResponse, ZKNetworkError

from app import config
from app.database import SessionLocal
from app.models import AttendanceLog, Device, FingerprintTemplate
from app.services import employee_sync

log = logging.getLogger(__name__)


def _connect(device):
    zk = ZK(device.ip_address, port=device.port, timeout=30, password=device.comm_key or 0, verbose=False)
    try:
        return zk.connect()
    except ZKErrorResponse as exc:
        # See app/services/sdk.py:_connect for why this message match is the
        # only reliable way pyzk signals a rejected comm key.
        if str(exc) == "Unauthenticated":
            raise ZKErrorResponse("Device refused the connection — the configured comm key is likely wrong")
        raise


def pull_employees(serial_number: str) -> dict:
    log.info("pull_employees: starting for device %s", serial_number)
    result = {"users_synced": 0, "errors": []}
    db = SessionLocal()
    try:
        device = db.query(Device).filter_by(serial_number=serial_number).first()
        if not device:
            log.warning("pull_employees: device %s not found in DB", serial_number)
            result["errors"].append("Device not found")
            return result

        conn = None
        try:
            log.info("pull_employees: connecting to %s (%s:%s)",
                     serial_number, device.ip_address, device.port)
            conn = _connect(device)
            conn.disable_device()

            for user in conn.get_users():
                # Deliberately the same writer the ADMS `tabledata&tablename=user`
                # upload uses (app/services/employee_sync.py). A device on the
                # LAN can be reachable over both TCP 4370 and the PUSH channel,
                # and two writers with different ideas of what an empty field
                # means would make the row flip-flop between them. The visible
                # change from the previous inline version: pyzk reports an
                # unnamed user as "" and a card-less user as 0, and neither now
                # overwrites a name or card that is already on the row.
                employee_sync.record_device_user(
                    db,
                    serial_number,
                    user.user_id,
                    uid=user.uid,
                    name=user.name,
                    privilege=user.privilege,
                    card=user.card,
                )
                result["users_synced"] += 1

            db.commit()
            device.last_seen = datetime.now(timezone.utc)
            device.is_online = True
            db.commit()
            log.info("pull_employees: done for %s — %d users synced",
                     serial_number, result["users_synced"])

        except (ZKErrorConnection, ZKNetworkError) as e:
            log.error("pull_employees: connection error for %s — %s", serial_number, e)
            result["errors"].append(str(e))
            device.is_online = False
            db.commit()
        except ZKErrorResponse as e:
            log.error("pull_employees: device %s refused authentication — %s", serial_number, e)
            result["errors"].append(str(e))
            db.rollback()
        except Exception as e:
            log.exception("pull_employees: unexpected error for %s", serial_number)
            result["errors"].append(str(e))
            db.rollback()
        finally:
            if conn:
                try:
                    conn.enable_device()
                    conn.disconnect()
                except Exception:
                    pass
    finally:
        db.close()

    return result


def pull_attendance(serial_number: str) -> dict:
    log.info("pull_attendance: starting for device %s", serial_number)
    result = {"attendance_synced": 0, "errors": []}
    db = SessionLocal()
    try:
        device = db.query(Device).filter_by(serial_number=serial_number).first()
        if not device:
            log.warning("pull_attendance: device %s not found in DB", serial_number)
            result["errors"].append("Device not found")
            return result

        conn = None
        try:
            log.info("pull_attendance: connecting to %s (%s:%s)",
                     serial_number, device.ip_address, device.port)
            conn = _connect(device)
            conn.disable_device()

            records = conn.get_attendance()
            log.info("pull_attendance: device %s returned %d records from device",
                     serial_number, len(records))

            # Load the keys already stored for this device in one query, rather
            # than a SELECT per record (20k+ round-trips otherwise).
            #
            # `.replace(tzinfo=None)` is load-bearing, not tidying. Every
            # DateTime column in app/models.py is `UTCDateTime` (models.py:6
            # imports it *as* DateTime), and its process_result_value stamps
            # tzinfo=UTC onto every value read back. pyzk hands back the
            # device's naive wall-clock. An aware datetime never compares equal
            # to a naive one, so without this the set below matches nothing:
            # every record looks new on every pull, and the insert trips
            # uq_attendance on the first punch already stored. The first pull
            # into an empty table succeeds and every pull after it fails —
            # which is exactly how this was found, on PSS7235100187.
            #
            # Naive is the right side to normalise to: a punch IS a naive
            # device wall-clock (D10), labelled by the `timezone` column and
            # never converted. UTCDateTime's label is wrong for this column;
            # correcting that is a wider change than this dedup needs.
            existing = {
                (uid, ts.replace(tzinfo=None) if ts is not None and ts.tzinfo else ts)
                for uid, ts in db.query(
                    AttendanceLog.user_id, AttendanceLog.timestamp
                ).filter_by(device_sn=serial_number)
            }

            # Devices routinely report the same punch more than once in a single
            # pull, so dedupe within the batch too — the session has
            # autoflush=False, so the per-row check below can't see rows added
            # earlier in this loop, and a duplicate would trip uq_attendance and
            # roll back the entire pull.
            seen = set()
            new_rows = []
            for att in records:
                key = (str(att.user_id), att.timestamp)
                if key in existing or key in seen:
                    continue
                seen.add(key)
                new_rows.append(AttendanceLog(
                    device_sn=serial_number,
                    user_id=str(att.user_id),
                    timestamp=att.timestamp,
                    status=att.status,
                    punch=att.punch,
                    source="sdk_pull",
                    # pyzk hands back the device's own naive wall-clock, same
                    # as a PUSH record. Stored as-is and labelled, never
                    # converted (D10).
                    timezone=device.timezone or config.DEFAULT_DEVICE_TIMEZONE,
                ))

            db.bulk_save_objects(new_rows)
            result["attendance_synced"] = len(new_rows)

            db.commit()
            device.last_seen = datetime.now(timezone.utc)
            device.is_online = True
            db.commit()
            log.info("pull_attendance: done for %s — %d new records inserted",
                     serial_number, result["attendance_synced"])

        except (ZKErrorConnection, ZKNetworkError) as e:
            log.error("pull_attendance: connection error for %s — %s", serial_number, e)
            result["errors"].append(str(e))
            device.is_online = False
            db.commit()
        except ZKErrorResponse as e:
            log.error("pull_attendance: device %s refused authentication — %s", serial_number, e)
            result["errors"].append(str(e))
            db.rollback()
        except Exception as e:
            log.exception("pull_attendance: unexpected error for %s", serial_number)
            result["errors"].append(str(e))
            db.rollback()
        finally:
            if conn:
                try:
                    conn.enable_device()
                    conn.disconnect()
                except Exception:
                    pass
    finally:
        db.close()

    return result


def store_templates(db, serial_number: str, conn) -> list:
    """Read every fingerprint the device holds and upsert it into
    ``fingerprint_templates``. Returns the rows written, in device order.

    The one writer for the SDK-era fingerprint table: the manual
    "Sync Templates" route and the Sync All pull both come through here, so
    a template read by either lands in the same row with the same key
    (``user_id``, ``finger_id``). A finger whose device ``uid`` does not map
    to a known ``user_id`` is skipped — there is no employee to attach it to.

    Does not commit; the caller owns the transaction.
    """
    uid_map = {u.uid: u.user_id for u in conn.get_users()}
    result = []
    for finger in conn.get_templates():
        user_id = uid_map.get(finger.uid)
        if not user_id:
            continue
        packed = finger.json_pack()
        ft = db.query(FingerprintTemplate).filter_by(
            user_id=user_id, finger_id=finger.fid
        ).first()
        if ft:
            ft.valid = finger.valid
            ft.template = packed["template"]
            ft.source_device_sn = serial_number
        else:
            ft = FingerprintTemplate(
                user_id=user_id,
                finger_id=finger.fid,
                valid=finger.valid,
                template=packed["template"],
                source_device_sn=serial_number,
            )
            db.add(ft)
        result.append(ft)
    return result


def pull_templates(serial_number: str) -> dict:
    log.info("pull_templates: starting for device %s", serial_number)
    result = {"templates_synced": 0, "errors": []}
    db = SessionLocal()
    try:
        device = db.query(Device).filter_by(serial_number=serial_number).first()
        if not device:
            log.warning("pull_templates: device %s not found in DB", serial_number)
            result["errors"].append("Device not found")
            return result

        conn = None
        try:
            log.info("pull_templates: connecting to %s (%s:%s)",
                     serial_number, device.ip_address, device.port)
            conn = _connect(device)
            conn.disable_device()

            rows = store_templates(db, serial_number, conn)
            result["templates_synced"] = len(rows)

            db.commit()
            device.last_seen = datetime.now(timezone.utc)
            device.is_online = True
            db.commit()
            log.info("pull_templates: done for %s — %d templates synced",
                     serial_number, result["templates_synced"])

        except (ZKErrorConnection, ZKNetworkError) as e:
            log.error("pull_templates: connection error for %s — %s", serial_number, e)
            result["errors"].append(str(e))
            device.is_online = False
            db.commit()
        except ZKErrorResponse as e:
            log.error("pull_templates: device %s refused authentication — %s", serial_number, e)
            result["errors"].append(str(e))
            db.rollback()
        except Exception as e:
            log.exception("pull_templates: unexpected error for %s", serial_number)
            result["errors"].append(str(e))
            db.rollback()
        finally:
            if conn:
                try:
                    conn.enable_device()
                    conn.disconnect()
                except Exception:
                    pass
    finally:
        db.close()

    return result


def pull_device(serial_number: str) -> dict:
    """Sync everything: employees + attendance + fingerprint templates.
    Used by Sync All and auto-registration.

    Templates are pulled last and after employees on purpose: a template
    keys on the ``user_id`` the employee pull just wrote, and a finger for a
    person the server has not heard of yet would be dropped.
    """
    emp_result  = pull_employees(serial_number)
    att_result  = pull_attendance(serial_number)
    tpl_result  = pull_templates(serial_number)
    return {
        "users_synced":      emp_result["users_synced"],
        "attendance_synced": att_result["attendance_synced"],
        "templates_synced":  tpl_result["templates_synced"],
        "errors":            emp_result["errors"] + att_result["errors"] + tpl_result["errors"],
    }
