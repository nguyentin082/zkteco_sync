"""Both directions of the ZKTime .NET backup file: restore (F1) and export (F2).

Restoring runs in the order an operator actually works: upload the file and be
told what is in it, look at that, then commit. The upload is staged on disk
between the second and third so a 19 MB file crosses the wire once rather than
once per decision.

Exporting is a button. A ZKTime backup can only be written by starting from
one — 78 of its 82 tables are ZKTime's own configuration and are not derivable
from anything here (see app/services/zktime_export.py) — so the server keeps
one as a *template*. A successful restore saves its own file as that template,
which means the two directions compose: restore once, and Backup works from
then on without ever asking for a file again. An operator who wants Backup
without restoring anything can set the template directly.

Everything here is admin-only. A backup file is a complete staff record —
names, PINs, card numbers and fingerprint templates — and a restore writes
directly into attendance history, which is what payroll is reconciled against.
A restore can also *register* a device: the terminal whose punches these are
has usually never reached this server, so it is created from the file's own
record of it rather than demanded up front (zktime_backup.adopt_terminal).
That grants a serial trust, so it is audited under `device_create`, the same
action POST /devices records.

The body of an upload is raw bytes, not a multipart form. A ZKTime backup is
one file with no accompanying fields, multipart would wrap tens of megabytes
in a parser for no gain, and ``fetch(url, {body: file})`` sends raw bytes from
a browser without any of it. The options that would have been form fields are
query parameters on the restore call instead, where they are visible in the
audit log.
"""

import logging
import os
import re
import secrets
import shutil
import time
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app import audit, config
from app.database import get_db
from app.deps import require_admin
from app.models import Device, User
from app.net import client_ip
from app.services import zktime_backup, zktime_export
from app.services.zktime_backup import ZKTimeBackupError
from app.errors import AppError

log = logging.getLogger(__name__)

router = APIRouter(prefix="/backup", tags=["backup"],
                   dependencies=[Depends(require_admin)])

# A staging token is generated here and nowhere else, so this pattern is not
# validating user creativity — it is the guard that keeps a token from ever
# being read as a path. `..` and `/` cannot match it, which is what makes
# os.path.join below safe.
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")


# ---------------------------------------------------------------------------
# Staging
# ---------------------------------------------------------------------------

def _stage_dir() -> str:
    # 0o700: the file holds biometric templates and staff records, and the
    # directory usually lives under /tmp where every account on the box can
    # list. Created on demand rather than at import so the module stays
    # importable in a test that never uploads anything.
    os.makedirs(config.BACKUP_STAGE_DIR, mode=0o700, exist_ok=True)
    return config.BACKUP_STAGE_DIR


def _staged_path(token: str) -> str:
    if not _TOKEN_RE.match(token or ""):
        raise AppError("backup.invalid_token", status_code=400, detail="Not a valid upload reference.")
    return os.path.join(_stage_dir(), f"{token}.db")


def _discard(token: str) -> bool:
    """Delete a staged upload. Never raises — the file is a cache, not a record."""
    try:
        os.remove(_staged_path(token))
        return True
    except (OSError, HTTPException):
        return False


def _purge_expired() -> int:
    """Drop staged uploads older than the TTL.

    Runs on every upload rather than on a scheduler, because the moment a new
    backup arrives is exactly the moment the directory is worth tidying, and a
    scheduled job is one more thing that can be switched off without anybody
    noticing that biometric data is accumulating in /tmp.
    """
    cutoff = time.time() - config.BACKUP_STAGE_TTL_SECONDS
    removed = 0
    try:
        entries = os.listdir(_stage_dir())
    except OSError:
        return 0

    for entry in entries:
        if not entry.endswith(".db"):
            continue
        path = os.path.join(config.BACKUP_STAGE_DIR, entry)
        try:
            if os.path.getmtime(path) < cutoff:
                os.remove(path)
                removed += 1
        except OSError:
            continue
    if removed:
        log.info("backup staging: removed %d expired upload(s)", removed)
    return removed


def _require_fresh(token: str) -> str:
    """The staged path, if it exists and has not expired."""
    path = _staged_path(token)
    try:
        age = time.time() - os.path.getmtime(path)
    except OSError:
        raise AppError("backup.upload_gone",
            status_code=404,
            detail="That upload is no longer held on the server. Upload the file again.",
        )
    if age > config.BACKUP_STAGE_TTL_SECONDS:
        _discard(token)
        raise AppError("backup.upload_expired", params={"minutes": config.BACKUP_STAGE_TTL_SECONDS // 60},
            status_code=404,
            detail=(
                "That upload expired and was deleted "
                f"({config.BACKUP_STAGE_TTL_SECONDS // 60} minutes after it arrived). "
                "Upload the file again."
            ),
        )
    return path


async def _stream_to_stage(request: Request) -> tuple:
    """Write a request body to a staged file. Returns ``(token, size)``.

    Shared by /backup/upload and /backup/template because both take a whole
    SQLite database as a raw body and must not buffer it: holding a 19 MB
    upload in the worker only to find out how big it is costs memory per
    concurrent request, and MaxBodySizeMiddleware has already refused anything
    past BACKUP_MAX_UPLOAD_BYTES before a byte reaches here.
    """
    _purge_expired()

    token = secrets.token_urlsafe(24)
    path = _staged_path(token)

    size = 0
    try:
        # 0o600 from the moment it exists, rather than written and then
        # chmod'ed: between those two calls the file would be readable by
        # anyone on the box, and what is in it is fingerprint templates.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            async for chunk in request.stream():
                handle.write(chunk)
                size += len(chunk)
    except Exception:
        _discard(token)
        log.exception("backup: upload failed while staging")
        raise AppError("backup.upload_save_failed", status_code=500, detail="The upload could not be saved.")

    if size == 0:
        _discard(token)
        raise AppError("backup.upload_empty", status_code=400, detail="The uploaded file is empty.")

    return token, size


# ---------------------------------------------------------------------------
# The backup template
# ---------------------------------------------------------------------------
#
# An export is built on a real ZKTime backup — 78 of its 82 tables are
# ZKTime's own configuration and cannot be generated (see
# app/services/zktime_export.py). Keeping one on the server is what lets
# Backup be a single button instead of an upload every time.

def _template_path() -> str:
    os.makedirs(os.path.dirname(config.BACKUP_TEMPLATE_PATH) or ".",
                mode=0o700, exist_ok=True)
    return config.BACKUP_TEMPLATE_PATH


def _save_template(source_path: str) -> bool:
    """Adopt a validated backup as the export template. Never raises.

    Written beside the target and moved into place, because os.replace is
    atomic within a filesystem: a crash half-way leaves the previous template
    intact rather than a truncated file that passes the magic-number check and
    fails as a database.

    Returns whether it worked. A template that could not be saved costs the
    operator one upload later; it must never turn a successful restore into a
    failed request.
    """
    try:
        target = _template_path()
        staging = f"{target}.incoming"
        shutil.copyfile(source_path, staging)
        os.chmod(staging, 0o600)
        os.replace(staging, target)
        return True
    except OSError:
        log.warning(
            "backup: could not save the export template to %s — Backup will "
            "ask for one. Set BACKUP_DATA_DIR to a writable path.",
            config.BACKUP_TEMPLATE_PATH, exc_info=True,
        )
        return False


def _template_status(db: Session) -> dict:
    """What the Backup panel needs to know before offering its button."""
    path = config.BACKUP_TEMPLATE_PATH
    try:
        stat = os.stat(path)
    except OSError:
        return {"configured": False, "path": path}

    status = {
        "configured": True,
        "path": path,
        "size": stat.st_size,
        "saved_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
    }
    # A template that has stopped being readable is worse than none: Backup
    # would offer its button and fail on the click. Checked here so the panel
    # can say so while the operator is already looking at it.
    try:
        conn = zktime_backup.open_backup(path)
    except ZKTimeBackupError as exc:
        status["usable"] = False
        status["problem"] = str(exc)
        status["problem_code"] = exc.code
        status["problem_params"] = exc.params
        return status
    try:
        status["preview"] = zktime_backup.inspect_backup(conn)
        status["usable"] = True
    except Exception as exc:
        status["usable"] = False
        status["problem"] = str(exc)
    finally:
        conn.close()
    return status


@router.get("/template")
def get_template(db: Session = Depends(get_db)):
    return _template_status(db)


@router.post("/template")
async def set_template(request: Request, admin: User = Depends(require_admin),
                       db: Session = Depends(get_db)):
    """Upload the ZKTime backup that exports are built on.

    Separate from /backup/upload because this one persists. A restore adopts
    its own file as the template automatically, so most installations never
    call this; it exists for an operator who wants Backup working without
    restoring anything first.
    """
    token, size = await _stream_to_stage(request)

    path = _staged_path(token)
    try:
        zktime_backup.open_backup(path).close()
    except ZKTimeBackupError as exc:
        _discard(token)
        raise AppError.wrap(exc, 400)

    saved = _save_template(path)
    _discard(token)
    if not saved:
        raise AppError("backup.template_write_failed", params={"path": config.BACKUP_TEMPLATE_PATH},
            status_code=500,
            detail=(
                f"The template could not be written to {config.BACKUP_TEMPLATE_PATH}. "
                "Set BACKUP_DATA_DIR to a path the app can write to."
            ),
        )

    audit.record(db, admin.username, "zktime_template_set", ip=client_ip(request),
                 detail=f"{size:,} bytes")
    return _template_status(db)


@router.delete("/template")
def clear_template(request: Request, admin: User = Depends(require_admin),
                   db: Session = Depends(get_db)):
    try:
        os.remove(config.BACKUP_TEMPLATE_PATH)
        removed = True
    except OSError:
        removed = False
    if removed:
        audit.record(db, admin.username, "zktime_template_cleared",
                     ip=client_ip(request), detail="export template deleted")
    return {"removed": removed}


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------

@router.post("/upload")
async def upload_backup(request: Request, admin: User = Depends(require_admin),
                        db: Session = Depends(get_db)):
    """Stage a ZKTime .db and answer with what it contains.

    The body is streamed straight to disk. Buffering it would mean holding the
    whole database in the worker's memory to find out how big it is, and
    MaxBodySizeMiddleware has already refused anything past
    BACKUP_MAX_UPLOAD_BYTES before a byte of it reaches here.
    """
    token, _size = await _stream_to_stage(request)
    path = _staged_path(token)

    try:
        conn = zktime_backup.open_backup(path)
    except ZKTimeBackupError as exc:
        # A file that is not a backup is deleted now, not left to the TTL:
        # there is nothing an operator can do with it and no reason to keep it.
        _discard(token)
        raise AppError.wrap(exc, 400)

    try:
        preview = zktime_backup.inspect_backup(conn)
    except Exception as exc:
        _discard(token)
        log.exception("backup upload: could not read staged database")
        raise AppError("backup.db_unreadable", params={"error": str(exc)},
            status_code=400,
            detail=f"The file opened as a database but could not be read: {exc}",
        )
    finally:
        conn.close()

    # Which of the file's terminals correspond to a device registered here.
    # Resolved server-side so the UI never has to guess a mapping, and left as
    # a suggestion: the operator still chooses.
    registered = {
        row[0] for row in db.query(Device.serial_number).all()
    }
    for terminal in preview["terminals"]:
        terminal["registered_here"] = terminal["serial"] in registered

    audit.record(
        db, admin.username, "zktime_backup_upload", ip=client_ip(request),
        detail=(
            f"staged {_size:,} bytes; {preview['employees']} employee(s), "
            f"{preview['punches']['total']:,} punch(es), "
            f"{len(preview['terminals'])} terminal(s)"
        ),
    )

    return {
        "token": token,
        "size": _size,
        "expires_in_seconds": config.BACKUP_STAGE_TTL_SECONDS,
        "preview": preview,
    }


@router.delete("/upload/{token}")
def discard_upload(token: str, request: Request,
                   admin: User = Depends(require_admin),
                   db: Session = Depends(get_db)):
    """Throw away a staged upload without restoring it.

    Not strictly needed — the TTL would get there — but an operator who has
    decided against a restore should be able to remove a file full of
    fingerprint data immediately rather than trusting a timer.
    """
    removed = _discard(token)
    if removed:
        audit.record(db, admin.username, "zktime_backup_discard",
                     ip=client_ip(request), detail="staged upload deleted")
    return {"discarded": removed}


# ---------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------

class RestoreRequest(BaseModel):
    token: str
    device_sn: str
    # Which terminal inside the file to take punches from. Optional, and when
    # omitted every punch in the file is restored onto `device_sn` — correct
    # for a single-terminal backup and stated in the response's warnings for
    # any other.
    terminal_id: int = None
    # Templates are absent from this default on purpose; see
    # zktime_backup._restore_templates.
    parts: list[str] = Field(default_factory=lambda: ["employees", "attendance"])


@router.post("/restore")
def restore_backup(payload: RestoreRequest, request: Request,
                   admin: User = Depends(require_admin),
                   db: Session = Depends(get_db)):
    """Write the staged backup's contents into this app's tables.

    Synchronous, unlike the device pulls next door. A pull is a background task
    because it waits on a terminal that may not answer for half a minute; this
    reads a local file and does bulk inserts, and the operator needs the
    per-part counts to know what happened. Returning "started" and leaving them
    to guess would be worse than the wait.
    """
    path = _require_fresh(payload.token)

    if not payload.parts:
        raise AppError("backup.nothing_selected", params={"parts": ", ".join(zktime_backup.PARTS)},
            status_code=400,
            detail="Nothing was selected to restore. Choose at least one of: "
                   + ", ".join(zktime_backup.PARTS),
        )

    try:
        conn = zktime_backup.open_backup(path)
    except ZKTimeBackupError as exc:
        raise AppError.wrap(exc, 400)

    try:
        summary = zktime_backup.restore(
            db, conn,
            device_sn=payload.device_sn,
            terminal_id=payload.terminal_id,
            parts=tuple(payload.parts),
            created_by=admin.username,
        )
    except ZKTimeBackupError as exc:
        raise AppError.wrap(exc, 400)
    except Exception:
        # Each part commits its own batches, so a failure part-way leaves the
        # parts that finished in place. Saying so is the point: silently
        # reporting a clean failure would send the operator to re-run a restore
        # that has already half happened — which is safe, because every insert
        # is keyed and repeatable, but only if they know to check.
        db.rollback()
        log.exception("zktime restore failed for device %s", payload.device_sn)
        raise AppError("backup.restore_partial_failure",
            status_code=500,
            detail="The restore failed part-way through. Anything already "
                   "written has been kept — re-running the restore is safe, "
                   "it skips what is already there. Check the server log.",
        )
    finally:
        conn.close()

    # The file that was just restored is the freshest ZKTime backup this
    # installation has seen, which makes it the best template an export could
    # be built on — so it is kept as one. This is what turns Backup into a
    # single button: restore once, and every export afterwards has ZKTime's
    # configuration to build on without another upload.
    summary["template_saved"] = _save_template(path)

    # The staged copy is deleted on success and only on success. After a
    # failure it stays until the TTL, so a re-run does not need another 19 MB
    # upload.
    _discard(payload.token)

    # Recorded under the same action as POST /devices, not folded into the
    # restore's own line: a device becoming trusted is a fact the roster is
    # read for, and it must be findable there whichever route created it.
    if summary.get("device_created"):
        audit.record(
            db, admin.username, "device_create", target=payload.device_sn,
            ip=client_ip(request),
            detail="registered from a ZKTime backup file during restore; "
                   f"timezone {summary.get('device_timezone')}",
        )

    audit.record(
        db, admin.username, "zktime_backup_restore", target=payload.device_sn,
        ip=client_ip(request),
        detail="; ".join(
            f"{part}: " + ", ".join(
                f"{key}={value}" for key, value in summary[part].items()
                if isinstance(value, int)
            )
            for part in summary["parts"] if isinstance(summary.get(part), dict)
        ) or "nothing restored",
    )

    return summary


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

class ExportRequest(BaseModel):
    prune_missing: bool = False


@router.post("/export")
def build_export(payload: ExportRequest, request: Request,
                 admin: User = Depends(require_admin),
                 db: Session = Depends(get_db)):
    """Build a ZKTime backup from this app's data and stage it for download.

    No upload: the template is the one held on the server, which a restore
    saves automatically and the Backup panel can set directly. Backup is a
    button, not a workflow.

    Two calls rather than one streamed response, so the operator sees what the
    export contains — and any warning about it — *before* deciding where to
    save it. The warning that matters most (this file holds fewer punches than
    the template it was built from) is exactly the one that is useless after
    the file has already landed on disk.
    """
    template = config.BACKUP_TEMPLATE_PATH
    if not os.path.isfile(template):
        raise AppError("backup.no_template",
            status_code=409,
            detail=(
                "No ZKTime backup is held as a template, and one is needed: "
                "most of a ZKTime database is its own configuration, which "
                "this app cannot generate. Restore a ZKTime backup, or upload "
                "one as the template, and Backup will work from then on."
            ),
        )

    out_token = secrets.token_urlsafe(24)
    out_path = _staged_path(out_token)

    try:
        summary = zktime_export.build_export(
            db, template, out_path, prune_missing=payload.prune_missing
        )
    except ZKTimeBackupError as exc:
        raise AppError.wrap(exc, 400)
    except Exception:
        log.exception("zktime export failed")
        raise AppError("backup.export_failed",
            status_code=500,
            detail="The export could not be built. Nothing was written and "
                   "your uploaded template is untouched. Check the server log.",
        )

    audit.record(
        db, admin.username, "zktime_backup_export", ip=client_ip(request),
        detail=(
            f"{summary['filename']}: {summary['punches']['written']:,} punch(es), "
            f"{summary['templates']['written']} template(s), "
            f"{summary['size']:,} bytes"
        ),
    )

    return {"token": out_token, **summary}


@router.get("/export/{token}")
def download_export(token: str):
    """Hand over a built export.

    A GET with the token in the path, so the browser can be pointed straight
    at it and the file is named by Content-Disposition like every other
    download in this app. The staged file is left in place afterwards rather
    than deleted on read: a download that fails half-way is common enough, and
    the TTL will clear it.
    """
    path = _require_fresh(token)
    return FileResponse(
        path,
        # ZKTime's own extension. The media type is the registered one for a
        # SQLite database; browsers treat it as a download either way, and
        # naming it honestly beats octet-stream.
        media_type="application/vnd.sqlite3",
        filename=zktime_export.export_filename(),
    )
