"""Idempotent, additive schema migrations run on every boot.

``create_all()`` creates missing *tables* but never touches a table that
already exists, so an upgraded install would silently run without the new
columns. This module closes that gap: it compares the mapped metadata with
what the database actually has and issues ``ALTER TABLE ... ADD`` for
anything missing.

Rules this module holds to, because it runs unattended on operator databases:

* additive only — never drops, never renames, and never retypes except to
  widen an enum so that it accepts one more value (``widen_attendance_source``
  is the only such case, and carries its own justification);
* safe to repeat — a second run is a no-op;
* never adds NOT NULL without a default, since existing rows must get a value;
* dialect-portable across MariaDB, MySQL, PostgreSQL and MSSQL by asking the
  dialect's own type compiler rather than hard-coding SQL types.

Later units extend this file by adding columns to ``app/models.py`` (nothing
to do here — they are picked up automatically) and, if a new column needs
existing rows backfilled, by adding a statement to ``_run_data_fixups``,
which is handed the set of columns this run actually created.
"""

import logging

from sqlalchemy import Enum as SAEnum
from sqlalchemy import inspect, text
from sqlalchemy.schema import CreateIndex

from app import config
from app.database import Base

# Importing the models is what populates Base.metadata — without it every
# table would look absent and run_migrations would silently do nothing.
import app.models  # noqa: F401,E402

log = logging.getLogger(__name__)


def run_migrations(engine) -> None:
    """Single entry point, called from the app lifespan right after create_all."""
    added = _add_missing_columns(engine)
    _add_missing_indexes(engine)
    widen_attendance_source(engine)
    _run_data_fixups(engine, added)


# ---------------------------------------------------------------------------
# Columns
# ---------------------------------------------------------------------------

def _add_missing_columns(engine) -> set:
    """Add every mapped column the database lacks; return what was added.

    The returned ``{(table, column)}`` set is what tells a data fixup that a
    column has just appeared on a table that already held rows — the only
    moment a backfill is meaningful."""
    inspector = inspect(engine)
    known_tables = set(inspector.get_table_names())
    added = set()

    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if table.name not in known_tables:
                continue  # create_all just built it with every column present
            present = {col["name"] for col in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in present:
                    continue
                _add_column(conn, engine.dialect, table, column)
                added.add((table.name, column.name))

    return added


def add_column_sql(dialect, table, column) -> str:
    """The ALTER TABLE statement that adds one column, in the given dialect."""
    preparer = dialect.identifier_preparer
    default = _default_literal(column, dialect)

    parts = [preparer.quote(column.name), column.type.compile(dialect)]
    # NOT NULL is only safe when the database itself can fill existing rows.
    parts.append("NOT NULL" if (not column.nullable and default is not None) else "NULL")
    if default is not None:
        parts.append(f"DEFAULT {default}")

    # MSSQL's ALTER TABLE ADD takes the column list directly — no COLUMN keyword.
    keyword = "ADD" if dialect.name == "mssql" else "ADD COLUMN"
    return f"ALTER TABLE {preparer.format_table(table)} {keyword} {' '.join(parts)}"


def _add_column(conn, dialect, table, column) -> None:
    # PostgreSQL stores Enum as a named type that must exist before a column
    # can reference it. On MySQL/MSSQL this is a no-op.
    if isinstance(column.type, SAEnum):
        column.type.create(conn, checkfirst=True)

    ddl = add_column_sql(dialect, table, column)
    log.info("migration: %s", ddl)
    conn.execute(text(ddl))

    if not column.nullable and _default_literal(column, dialect) is None:
        log.warning(
            "migration: %s.%s is declared NOT NULL but has no server default — "
            "added as NULL so existing rows stay valid",
            table.name,
            column.name,
        )


def _default_literal(column, dialect):
    """SQL literal for a column's default, or None when the DB cannot supply one.

    Python-side callables (timestamps, sequences) are applied by the ORM on
    insert, so they give the database nothing to backfill with."""
    server_default = getattr(column, "server_default", None)
    if server_default is not None and getattr(server_default, "arg", None) is not None:
        return str(server_default.arg)

    default = column.default
    if default is None or not getattr(default, "is_scalar", False):
        return None
    return _literal(default.arg, dialect)


def _literal(value, dialect):
    if isinstance(value, bool):
        # PostgreSQL will not accept 1/0 for a boolean column.
        if dialect.name == "postgresql":
            return "TRUE" if value else "FALSE"
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        escaped = value.replace("'", "''")
        return f"'{escaped}'"
    return None


# ---------------------------------------------------------------------------
# Indexes
# ---------------------------------------------------------------------------

def _add_missing_indexes(engine) -> None:
    """Create indexes declared in the metadata that the database lacks.

    A column added by _add_missing_columns arrives without the index its
    model declares, so this pass follows it."""
    inspector = inspect(engine)
    known_tables = set(inspector.get_table_names())

    for table in Base.metadata.sorted_tables:
        if table.name not in known_tables:
            continue
        present = {idx.get("name") for idx in inspector.get_indexes(table.name)}
        columns = {col["name"] for col in inspector.get_columns(table.name)}
        for index in table.indexes:
            if index.name in present:
                continue
            if not {col.name for col in index.columns}.issubset(columns):
                continue
            try:
                with engine.begin() as conn:
                    log.info("migration: creating index %s on %s", index.name, table.name)
                    conn.execute(CreateIndex(index))
            except Exception as exc:  # an index is an optimisation, never a blocker
                log.warning("migration: could not create index %s: %s", index.name, exc)


# ---------------------------------------------------------------------------
# Data fixups
# ---------------------------------------------------------------------------

def _run_data_fixups(engine, added: set) -> None:
    """Backfills for columns whose default is wrong for pre-existing rows.

    ``added`` holds the ``(table, column)`` pairs this run created. Keying a
    backfill on it is what makes the backfill idempotent: it fires on the one
    boot that introduced the column and never again, so it cannot later undo a
    value an operator has since chosen.
    """
    _approve_pre_existing_devices(engine, added)
    _stamp_timezone_provenance(engine, added)


# ---------------------------------------------------------------------------
# Enum widening — the one documented exception to "never retypes"
# ---------------------------------------------------------------------------

# The value F1's restore writes into attendance_logs.source, and the full set
# the column must accept once it exists. Kept here rather than imported from
# models so this migration states plainly what it intends the column to hold.
_ATTENDANCE_SOURCES = ("adms_push", "sdk_pull", "zktime_restore")
_NEW_ATTENDANCE_SOURCE = "zktime_restore"


def widen_attendance_source(engine) -> bool:
    """Teach ``attendance_logs.source`` the third value. Returns True if it acted.

    This module's contract says it never retypes a column, and that rule is
    what makes it safe to run unattended. This function is the one exception,
    and it earns it on three counts:

    * it only ever *adds* an accepted value — no existing row can stop being
      valid, so it cannot fail half-way and leave rows the column rejects;
    * nothing in the app reads or branches on ``source`` (it is written in
      three places and displayed nowhere), so no code path changes meaning;
    * without it, F1's restore inserts rows the column refuses, and the
      operator gets a database error instead of their attendance back.

    It is keyed on the column's *actual* contents rather than on ``added``,
    because the column has existed since the table did — there is no "the boot
    that introduced it" to hang a fixup on. Detection is what makes it
    idempotent: once the value is there, this is a no-op forever.

    A failure here is logged and swallowed. A server that will not boot is
    strictly worse than one where a restore reports a clear error, and the
    operator can always run the one ALTER by hand.
    """
    dialect = engine.dialect.name

    if "attendance_logs" not in set(inspect(engine).get_table_names()):
        return False        # fresh install; create_all builds it complete

    try:
        if dialect in ("mysql", "mariadb"):
            return _widen_enum_mysql(engine)
        if dialect == "postgresql":
            return _widen_enum_postgresql(engine)
        if dialect == "mssql":
            return _widen_check_mssql(engine)
        # SQLite renders an Enum as VARCHAR + CHECK and cannot alter either in
        # place. Every SQLite database this app touches is built fresh by
        # create_all (the test suite), so it is already complete and there is
        # genuinely nothing to do — not a gap being skipped over.
        return False
    except Exception as exc:
        log.error(
            "migration: could not widen attendance_logs.source to accept '%s' "
            "(%s). Restoring a ZKTime backup will fail until this column "
            "accepts the value; the fix is one ALTER TABLE by hand. Boot "
            "continues — nothing else depends on it.",
            _NEW_ATTENDANCE_SOURCE, exc,
        )
        return False


def _reflected_enum_values(engine) -> set:
    """The values the live column accepts, as the dialect reports them."""
    for column in inspect(engine).get_columns("attendance_logs"):
        if column["name"] == "source":
            return set(getattr(column["type"], "enums", None) or ())
    return set()


def _widen_enum_mysql(engine) -> bool:
    present = _reflected_enum_values(engine)
    # An empty set means the column is not an ENUM at all (someone changed it
    # to VARCHAR). That already accepts any string, so there is nothing to fix
    # and nothing to break by leaving it alone.
    if not present or _NEW_ATTENDANCE_SOURCE in present:
        return False

    # Union, not replacement: a column that somehow carries a value this
    # version has never heard of keeps accepting it, so no stored row is
    # orphaned by the ALTER.
    values = sorted(present | set(_ATTENDANCE_SOURCES))
    spelled = ", ".join("'" + v.replace("'", "''") + "'" for v in values)
    with engine.begin() as conn:
        conn.execute(text(
            f"ALTER TABLE attendance_logs MODIFY source ENUM({spelled}) NOT NULL"
        ))
    log.warning(
        "migration: attendance_logs.source widened to (%s) — ZKTime backup "
        "restore can now record its own provenance", spelled,
    )
    return True


def _widen_enum_postgresql(engine) -> bool:
    present = _reflected_enum_values(engine)
    if not present or _NEW_ATTENDANCE_SOURCE in present:
        return False

    # ALTER TYPE ... ADD VALUE cannot run inside a transaction block on
    # PostgreSQL before 12, and psycopg2 opens one for every connection. An
    # AUTOCOMMIT connection is the portable way to issue it on all versions.
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.execute(text(
            f"ALTER TYPE attendance_source ADD VALUE IF NOT EXISTS "
            f"'{_NEW_ATTENDANCE_SOURCE}'"
        ))
    log.warning(
        "migration: attendance_source gained the value '%s'",
        _NEW_ATTENDANCE_SOURCE,
    )
    return True


def _widen_check_mssql(engine) -> bool:
    """MSSQL spells an Enum as VARCHAR plus a CHECK constraint.

    So the widening is a constraint swap, not a type change, and the column
    itself is never touched. The constraint is looked up by what it guards
    rather than by name: SQLAlchemy generates the name, and an install created
    by an older version may carry a different one.
    """
    find = text(
        "SELECT cc.name, cc.definition FROM sys.check_constraints cc "
        "JOIN sys.columns c ON c.object_id = cc.parent_object_id "
        "                  AND c.column_id = cc.parent_column_id "
        "WHERE cc.parent_object_id = OBJECT_ID('attendance_logs') "
        "  AND c.name = 'source'"
    )
    with engine.begin() as conn:
        rows = conn.execute(find).fetchall()
        # No constraint means the column is a bare VARCHAR, which already
        # accepts the new value.
        if not rows:
            return False
        if all(_NEW_ATTENDANCE_SOURCE in (row[1] or "") for row in rows):
            return False

        spelled = ", ".join(f"'{v}'" for v in _ATTENDANCE_SOURCES)
        for name, _definition in rows:
            conn.execute(text(
                f"ALTER TABLE attendance_logs DROP CONSTRAINT [{name}]"
            ))
        conn.execute(text(
            "ALTER TABLE attendance_logs ADD CONSTRAINT ck_attendance_source "
            f"CHECK (source IN ({spelled}))"
        ))
    log.warning(
        "migration: attendance_logs.source CHECK constraint widened to (%s)",
        spelled,
    )
    return True


def _approve_pre_existing_devices(engine, added: set) -> None:
    """Devices already in the database were trusted before D3 existed.

    ``Device.status`` defaults to 'pending', which is right for a serial seen
    for the first time and badly wrong for an install that has been collecting
    attendance for months: those devices would go quiet the moment this
    version boots. Anything present at the instant the column appears is
    therefore grandfathered in as approved."""
    if ("devices", "status") not in added:
        return

    with engine.begin() as conn:
        result = conn.execute(
            text(
                "UPDATE devices SET status = 'approved', approved_by = 'migration' "
                "WHERE status IS NULL OR status = 'pending'"
            )
        )
    log.warning(
        "migration: grandfathered %s pre-existing device(s) to status='approved' — "
        "newly seen serials from now on require explicit approval",
        result.rowcount,
    )


def _stamp_timezone_provenance(engine, added: set) -> None:
    """Give pre-existing devices and records the timezone they always had.

    D10 records what a stored wall-clock time means instead of guessing at it.
    A device that has been pushing punches for months was already in some
    particular zone; the rows are not wrong, they are unlabelled. So on the
    one boot that introduces each column, the label is filled in:

    * every device gets DEFAULT_DEVICE_TIMEZONE — the operator's own answer to
      "what are these clocks set to";
    * every attendance record gets *its own device's* zone, not the global
      default, so a multi-site install is labelled correctly, falling back to
      the default only for rows whose device row no longer exists.

    Keyed on ``added`` like every fixup here, so it fires exactly once and can
    never later overwrite a zone an operator has since chosen. Both statements
    are single UPDATEs — attendance_logs can hold hundreds of thousands of
    rows and a row-by-row loop would be unusable.
    """
    default_tz = config.DEFAULT_DEVICE_TIMEZONE

    if ("devices", "timezone") in added:
        # The ALTER already carried DEFAULT '<zone>', which MariaDB, MySQL,
        # PostgreSQL and MSSQL all apply to existing rows. This is the belt to
        # that braces: it costs one statement and covers the case where the
        # column had to be added NULL.
        with engine.begin() as conn:
            result = conn.execute(
                text(
                    "UPDATE devices SET timezone = :tz "
                    "WHERE timezone IS NULL OR timezone = ''"
                ),
                {"tz": default_tz},
            )
        log.warning(
            "migration: labelled %s pre-existing device(s) as timezone=%s — "
            "correct this per device in Devices if a terminal is set to another zone",
            result.rowcount, default_tz,
        )

    if ("attendance_logs", "timezone") in added:
        # One correlated-subquery UPDATE rather than a JOIN: UPDATE ... FROM
        # and UPDATE ... JOIN are spelled differently on every dialect this
        # app supports, whereas this form is portable to all four. It reads
        # from `devices` and writes to `attendance_logs`, so MySQL's
        # "can't-select-from-the-table-you're-updating" rule does not apply.
        #
        # `devices` is always there on a real install, but this module also
        # runs against partial schemas (an install mid-upgrade, a fixture), and
        # a backfill must never be the thing that stops a boot — so without it,
        # fall back to the flat default rather than failing.
        if "devices" in set(inspect(engine).get_table_names()):
            statement = text(
                "UPDATE attendance_logs SET timezone = COALESCE("
                "  (SELECT d.timezone FROM devices d "
                "   WHERE d.serial_number = attendance_logs.device_sn), :tz) "
                "WHERE timezone IS NULL"
            )
        else:
            statement = text(
                "UPDATE attendance_logs SET timezone = :tz WHERE timezone IS NULL"
            )
        with engine.begin() as conn:
            result = conn.execute(statement, {"tz": default_tz})
        log.warning(
            "migration: labelled %s existing attendance record(s) with their "
            "device's timezone — no punch time was changed, only labelled",
            result.rowcount,
        )
