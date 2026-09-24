# ZKTeco Sync

A self-hosted appliance that replaces ZKTeco BioCloud. It collects attendance from ZKTeco biometric devices and exposes a clean web UI and REST API — no cloud dependency, no per-device license. It can run purely on your LAN, or — behind Apache as a TLS reverse proxy — be reached from the public internet, so devices at sites without a shared network can push attendance directly. See [SECURITY.md](SECURITY.md) before doing the latter.

## How it works

ZKTeco devices speak two protocols simultaneously:

| Protocol | Direction | Port | Used for |
|----------|-----------|------|----------|
| **ADMS** (HTTP push) | Device → Server | 80/443 | Real-time attendance push |
| **SDK** (TCP pull) | Server → Device | 4370 | Employee sync, template pull, device control |

This app runs both listeners. Devices push attendance events the moment they happen; the SDK poller lets you pull historical records, employees, and fingerprint templates on demand.

## Features

- **Real-time attendance** via ADMS push from devices
- **Records that name nobody are kept out of the way** — a terminal writes a record under PIN 0 for a device event or a scan that matched no enrolled face or finger, and neither is attendance: both ingest paths now refuse them. Punches by an employee since deleted are a different thing — real hours, kept — so they are hidden from the Attendance table (one checkbox brings them back) but always present in the Excel timesheet, where somebody who resigned mid-month must still appear. Nothing is deleted.
- **On-demand sync** — pull employees, attendance, and fingerprint templates from any device
- **Employee management** — view all employees, push to specific devices, remove from devices
- **Excel export** — the monthly timesheet, as a formatted `.xlsx`, reproducing the report ZKTime.Net produced column for column: a row for every person on every day of the range, with hours worked, hours owed, overtime, lateness, early departures and absences scored against the shift set in `.env` (`WORK_SHIFT_START` and friends). One button, one kind of file. It covers the whole filter (device, employee, date range), not the page on screen, and days are paired by clock rather than by the device's status field — terminals where nobody presses the mode key stamp every punch identically.
- **Fingerprint templates** — pull from one device, push to others (one master copy per finger)
- **Live enrollment** — trigger fingerprint enrollment on a device from the UI
- **Device control** — unlock door, set clock, write LCD message, restart, queue raw commands
- **HRM integration** — push attendance records to a third-party HRM on a configurable interval, with last-synced-ID tracking and a manual trigger
- **ZKTime.Net backup and restore** — read a ZKTime `.db` into this app, or build one from it. Restore is the only way to recover punches older than this server, since an access-control terminal cannot be asked for its history. Both directions are additive and safe to repeat.
- **Multi-database** — MariaDB, MySQL, PostgreSQL, or MSSQL (including Windows Authentication)
- **DB-backed operator accounts** — Argon2id-hashed passwords, `admin`/`viewer` roles, forced password change on first login, account lockout after repeated failures. Sessions are an opaque server-side token in an `HttpOnly`, `SameSite=Strict` cookie (never a bearer token, never `localStorage`), with CSRF protection on every state-changing request. See [SECURITY.md](SECURITY.md) for the full model.
- **Device approval queue** — an unknown device serial is refused, not auto-registered; approve it from a pending queue, or open a time-boxed pairing window while onboarding new hardware. Optional per-device source-IP allowlist.

## Requirements

Either **Docker** (Engine 24+ with Compose v2.24+ — see [Docker](#docker)), or:

- Python 3.11+
- One of: MariaDB/MySQL, PostgreSQL, or MSSQL
- Node 18+ (development only — servers download a prebuilt frontend from GitHub Releases)

## Setup

### Prerequisites

- Python 3.11+
- `uv` — `pip install uv`
- One of: MariaDB/MySQL, PostgreSQL, or MSSQL
- Node 18+ with npm (development only)

### Docker

See [DEPLOY.md](DEPLOY.md) — one image for backend + UI, one compose file
with a bundled MariaDB (or point it at a database you already have), and the
one setting (`TRUSTED_PROXIES`) that differs from a native install.

### Starting MariaDB in Docker

The bundled database reads `DB_NAME`, `DB_USER` and `DB_PASSWORD` straight out
of `.env` — `docker-compose.yml` passes them to the image as its `MARIADB_*`
variables, so there is nothing to configure twice:

```bash
docker compose up -d db
```

The data lives in the `zkteco-sync_dbdata` volume, not in the container, so
restarting or rebuilding the container keeps it. `docker compose down -v`
deletes it.

### Guided installer (recommended for a bare-metal install)

The installer handles everything interactively — configuration, dependencies, frontend build, and optional service registration.

```bash
git clone <repo-url>
cd zkteco-sync
python install.py          # production deployment
python install.py --dev    # development machine
```

Production installs download the prebuilt frontend from GitHub Releases (no
Node needed) and offer to register a background service. Dev installs build
the frontend from your checkout (Node required), set `APP_ENV=development`
(uvicorn auto-reload), and skip service registration — use `npm run dev` in
`frontend/` for hot reload while editing.

### Upgrading

```bash
python install.py --upgrade
```

`--upgrade` is non-interactive: it pulls the latest code (re-executing itself
if the installer changed), re-syncs Python dependencies, refreshes the
frontend (prebuilt download on production boxes, npm build on dev boxes —
detected from `.env`), restarts the registered service, and never touches
`.env` or asks questions. Add `--skip-pull` to upgrade without pulling.

#### Upgrading an existing (pre-hardening) install

If your `.env` predates the operator-authentication and public-internet
hardening work, `--upgrade` will not add the new keys for you — it never
touches `.env`. After upgrading:

1. Add at minimum `ALLOWED_HOSTS=<your hostname>` to `.env`. Under
   `APP_ENV=production` the app now refuses to boot without it. Copy the
   rest of the new keys from `.env.example` (`TRUSTED_PROXIES`,
   `COOKIE_SECURE`, session/lockout settings, `ENABLE_DOCS`,
   `MAX_REQUEST_BYTES`, `ADMS_PAIRING_MINUTES`) — every one has a safe
   default if omitted, `ALLOWED_HOSTS` is the only one that's required.
2. Restart the app. On first boot against your existing database, a
   migration runs automatically: it adds the new `users`/`user_sessions`/
   `audit_log` tables, and — critically — marks every **existing** device
   row `approved` so devices already in production keep pushing without
   interruption. Only a serial the server has never seen before lands in the
   new pending-approval queue.
3. On that same first boot, since `users` is empty, the app seeds one admin
   from your existing `API_USERNAME`/`API_PASSWORD` with a forced password
   change. Log in, change the password, then delete both lines from `.env`
   — they are never consulted again after that first boot.
4. If you're now also fronting the app with Apache for the first time, see
   [Deploying behind Apache](#deploying-behind-apache-public-internet) below
   — in particular, `TRUSTED_PROXIES` has to match Apache's real source
   address or per-device IP allowlisting will misbehave.

### Releasing (maintainers)

1. Bump `version` in `pyproject.toml` and commit
2. Tag and push: `git tag v<version> && git push origin v<version>`
3. GitHub Actions builds the frontend and attaches
   `zkteco-sync-frontend-v<version>.zip` (plus a `.sha256` checksum) to the
   release — servers pick it up on their next `install.py --upgrade`

Keep the pyproject version and the tag in sync: servers prefer the release
matching their checked-out `pyproject.toml` version and only fall back to the
latest release if that tag has no build.

The installer will:
1. Prompt for bind address and port, the public hostname (`ALLOWED_HOSTS`) and trusted proxy addresses (`TRUSTED_PROXIES`), database credentials, and the admin bootstrap credentials
2. Write `.env` with an auto-generated secret key, `chmod`ed to `600`
3. Install Python dependencies
4. Test the database connection
5. Download the prebuilt frontend from GitHub Releases (falls back to a local npm build)
6. Optionally register a **systemd** service (Linux, with sandboxing directives — see [Deploying behind Apache](#deploying-behind-apache-public-internet)) or **NSSM Windows service** (Windows) so the app starts on boot and restarts on crash

If a `.env` already exists it will ask before overwriting (and will still `chmod` it to `600` either way).

### Manual setup

If you prefer to set things up yourself:

```bash
git clone <repo-url>
cd zkteco-sync
cp .env.example .env
chmod 600 .env
```

Edit `.env`. At minimum:

```env
# First-boot bootstrap only — seeds one admin, then ignored forever. Delete
# both lines once you've logged in and changed the password.
API_USERNAME=admin
API_PASSWORD=your-password
SECRET_KEY=<python -c "import secrets; print(secrets.token_hex(32))">

# Required under APP_ENV=production — the app refuses to boot without it.
ALLOWED_HOSTS=zk.example.com

DB_ENGINE=mariadb        # mariadb | mysql | postgresql | mssql
DB_HOST=127.0.0.1
DB_PORT=3306
DB_NAME=zkteco_sync
DB_USER=root
DB_PASSWORD=
```

See `.env.example` for the full list of keys (session lifetime, lockout
thresholds, `TRUSTED_PROXIES`, request body cap, etc.) — every one besides
`ALLOWED_HOSTS` has a safe default if left unset.

```bash
uv sync
npm install --prefix frontend
npm run build --prefix frontend
python run.py
```

By default the app binds to `127.0.0.1` only, so it isn't directly reachable
over the network — that's the intended setup once Apache is in front of it
(below). For a plain LAN deployment with no reverse proxy, set
`APP_HOST=0.0.0.0` in `.env`; `install.py` will warn you when you choose
this. Database tables (and any missing columns from an upgrade) are created
automatically on first run.

## Pointing devices at this server

On each ZKTeco device, set the ADMS / Cloud Server address to wherever the
app is actually reachable — `http://<server-ip>:8000` on a LAN deployment
with `APP_HOST=0.0.0.0` (`APP_BIND=0.0.0.0` under Docker), or your public hostname over HTTPS
(`https://zk.example.com`) behind Apache.

By default an unrecognized device serial is **refused, not auto-registered**
— it must be approved before it can push anything. Two ways to get a new
device connected:

- **Pairing mode (recommended for onboarding):** open a pairing window from
  **Devices → Open pairing window** (closes automatically after
  `ADMS_PAIRING_MINUTES`, 15 by default), then power on or reboot the
  device. It appears in a pending-approval queue with the IP it connected
  from; approve it there and it starts pushing normally. Close the window
  when you're done onboarding.
- **Manual add:** add the device directly from the Devices page with its
  serial number — this auto-approves it, so only do this for a device you
  already trust.

An existing install upgrading into this behavior does not need to re-approve
anything: the upgrade migration marks every device already in the database
`approved` automatically. Only a serial the server has never seen before
lands in the pending queue.

## Deploying behind Apache (public internet)

To expose this app on the public internet, put Apache in front of it as a
TLS-terminating reverse proxy and leave the app itself on `127.0.0.1` (the
default). A complete example vhost — TLS, HTTP→HTTPS redirect, proxy
directives, HSTS, a modern cipher suite — is at
[`deploy/apache/zkteco-sync.conf.example`](deploy/apache/zkteco-sync.conf.example).

Two things are easy to get wrong and worth reading closely before you copy
that file:

1. **`/iclock/*` (the device-push endpoints) must stay on the same vhost as
   the admin UI.** Device firmware only lets you configure a hostname and
   port for its server address — there's no server-path field — so every
   device pushes to the same origin the browser uses. Splitting them apart
   breaks device pushes.
2. **`TRUSTED_PROXIES` in `.env` must match Apache's real source address**
   (almost always `127.0.0.1`, the default) for the app to resolve a
   device's or browser's real IP from `X-Forwarded-For` correctly. Get this
   wrong and per-device IP allowlisting either refuses every device or can
   be walked through with a forged header — see the long comment block in
   the example vhost, and [SECURITY.md](SECURITY.md), for the full
   explanation.

`install.py` prompts for both `ALLOWED_HOSTS` (the hostname Apache serves)
and `TRUSTED_PROXIES` during a production install, and registers a systemd
service with sandboxing directives (`ProtectSystem=strict`, `ProtectHome`,
`NoNewPrivileges`, and others) suitable for a process now reachable
indirectly from the internet. See [SECURITY.md](SECURITY.md) for the full
threat model, the accepted residual risk around device-serial forgery, and
incident-response steps.

## Web UI and REST API on one origin

The UI and the API are served from the same origin, and some paths belong to
both: `/devices`, `/employees`, `/attendance` and `/users` are client-side
routes of the app *and* real API endpoints. The server decides which one you
meant from the request, not from the path — **the default is the page; add
the XHR flag to get JSON**:

```bash
# The app: a browser hard-refreshing, bookmarking or deep-linking any route
# gets the SPA shell and client-side routing takes over.

# The API: any client sending Accept: */* (curl's default, and every script)
# gets JSON, exactly as before.
curl -b cookies.txt https://zk.example.com/devices

# Explicitly, from anything that might look like a browser:
curl -b cookies.txt -H 'X-Requested-With: XMLHttpRequest' https://zk.example.com/devices
curl -b cookies.txt -H 'Accept: application/json'         https://zk.example.com/devices
```

`/iclock/*` is exempt from this entirely — the ADMS device protocol is
untouched. See [SECURITY.md](SECURITY.md) for the exact rule.

## HRM Integration

Go to **Settings → HRM Sync** in the UI to configure:

- **Endpoint** — the URL your HRM accepts attendance pushes at
- **Secret** — the API key sent with each push
- **Location ID** — identifier passed with every record
- **Interval** — how often to push (in seconds)
- **Timezone** — used to denote the timestamps pushed are in this timezone. The machine does not inform this
- **Last Synced ID** — editable; lower it to re-push records, raise it to skip

Records are batched in groups of 10,000. On failure, state is preserved so the next run resumes from where it left off.

## ZKTime.Net backup and restore

**Settings → Backup** and **Settings → Restore** (admin only). Reads and writes
the `.db` file ZKTime's own Backup button produces.

Why it matters: an access-control terminal's transaction table cannot be
queried — `POST /devices/{sn}/pull/attendance` answers 501 — so punches older
than this server exist only in a ZKTime backup.

### Restore

Upload a `.db`, check the summary, pick which terminal in the file and which
device here to file its punches under, then Restore. Times are stored exactly
as the file records them and labelled with that device's timezone; nothing is
shifted. It only ever adds, and a second run inserts nothing.

**The device does not have to be registered first.** A terminal that was never
pointed at this server has never announced itself, and it is exactly the one
whose punches exist nowhere else — so the picker offers the file's own
terminals too, and restoring onto one registers it from the file's
`att_terminal` row (serial, name, IP, port), approved in your name and audited
as `device_create`. Only a serial the file actually names can be created this
way. The new device gets `DEFAULT_DEVICE_TIMEZONE`, which is what its restored
punches are labelled with; if that is the wrong zone, `PATCH
/devices/{sn}/timezone` relabels them all without touching a stored digit.

**The device does not have to be registered first.** A terminal that was never
pointed at this server has never announced itself, and it is exactly the one
whose punches exist nowhere else — so the picker offers the file's own
terminals too, and restoring onto one registers it from the file's
`att_terminal` row (serial, name, IP, port), approved in your name. Only a
serial the file actually names can be created this way. The new device gets
`DEFAULT_DEVICE_TIMEZONE`, which is what its restored punches are labelled
with; if that is the wrong zone, `PATCH /devices/{sn}/timezone` relabels them
all without touching a single stored digit.

| Part | Default | |
|---|---|---|
| Employees | on | Fills blanks; never overwrites a field an operator filled |
| Attendance | on | Deduplicated on `(device, PIN, timestamp)` |
| Fingerprint templates | off | Stored as a copy, never pushed to a terminal |

Restore Employees alongside Attendance: the Attendance screen hides punches
whose PIN it cannot put a name to.

`att_day_summary` and `att_day_details` are not imported. They are ZKTime's
shift engine's conclusions; this app scores its own from the punches.

### Backup

One button, then a save dialogue. Produces `bak_zktime_YYYYDDMM.db`.

It is built on a **template** — a real ZKTime backup the server keeps — because
78 of the 82 tables are ZKTime's own installation (menus, logins, shifts, an
opaque `Sys_Config` BLOB) and cannot be generated. **A restore saves its own
file as that template**, so the normal path is: restore once, and Backup works
from then on. The template lives at `BACKUP_DATA_DIR/zktime_template.db`
(default `<app>/data`; Docker mounts a volume for it). Losing it only means
Backup asks for one again.

What it writes: `att_terminal` and `hr_employee` **merged**, so departments,
positions and device passwords survive; `att_punches` and `hr_biotemplate`
replaced in full; `att_day_*` emptied; everything else untouched.

**Watch this:** `att_punches` is replaced wholesale, so exporting from an app
holding less history than the template produces a *smaller* backup. Do not
overwrite the original with it — the export warns you, naming both counts.

SDK-pulled templates (`fingerprint_templates`) are not exported; only
`biometric_templates` is. Restoring a backup *with* Fingerprint templates
ticked is what fills that table.

Verified end to end against a real 19 MB backup: restore it, back it up again,
and punches, people and templates compare row for row identical, with all 82
tables and the `Sys_Config` BLOB byte-identical.

## Development

Run backend and frontend separately with hot reload:

```bash
# Backend
uv run uvicorn app.main:app --reload --port 8000

# Frontend (separate terminal)
npm run dev --prefix frontend
```

The Vite dev server proxies `/api` to `http://localhost:8000` automatically.

`DB_HOST` in `.env` stays `127.0.0.1` even when the database runs in Docker:
`db` is a name that only exists inside the compose network, and the `db`
service publishes `127.0.0.1:3306` for everything outside it. The app
container does not read that value — `docker-compose.yml` pins its own
`DB_HOST` to the service name — so one `.env` serves both.

## Acknowledgements

SDK device communication is powered by [pyzk](https://github.com/fananimi/pyzk), an open-source Python library for ZKTeco/ZKSoftware attendance machines. Many thanks to the pyzk community for making the SDK layer possible.

## Compatible devices

The following devices have been confirmed working by the pyzk community:

| Device | Platform | Firmware |
|--------|----------|----------|
| U580 | ZEM500 | Ver 6.21 |
| T4-C | ZEM510_TFT | Ver 6.60 |
| iClock260 | ZEM600_TFT | Ver 6.60 |
| iFace402/ID | ZEM800_TFT | Ver 6.60 |
| MA300 | ZEM560 | Ver 6.60 |
| iFace800/ID | ZEM600_TFT | Ver 6.60 |
| K20 | JZ4725_TFT | Ver 6.60 |
| VF680 | ZEM600_TFT | Ver 6.60 |
| RSP10k1 | ZLM30_TFT | Ver 6.70 |
| K14 | JZ4725_TFT | Ver 6.60 |
| iFace702 | ZMM220_TFT | Ver 6.60 |
| F18/ID | ZMM210_TFT | Ver 6.60 |
| K40/ID | JZ4725_TFT | Ver 6.60 |
| iClock3000/ID | ZMM200_TFT | Ver 6.60 |
| iClock880-H/ID | ZEM600_TFT | Ver 6.70 |

Any device running firmware Ver 6.x on a compatible platform should work. If you test a device not listed here, please open an issue so it can be added.

## Project structure

```
app/
  main.py           # FastAPI app, lifespan, middleware wiring, HRM scheduler
  config.py         # Validated settings from .env; fail-fast in production
  security.py       # Argon2id hashing, session token generation/hashing
  net.py            # Real client IP resolution (X-Forwarded-For / TRUSTED_PROXIES)
  middleware.py      # Security headers, request body size cap
  migrations.py      # Additive schema migrations, run on every boot
  audit.py          # Privileged-action audit log writer
  models.py         # SQLAlchemy models
  database.py       # DB engine, UTCDateTime type decorator
  schemas.py        # Pydantic request/response schemas
  deps.py           # Auth dependencies (require_auth, require_admin)
  routers/
    auth.py         # Login/logout, session, forced password change
    users.py        # Admin-only operator account management
    devices.py      # Device CRUD, approval queue, pairing, SDK actions
    employees.py    # Employee read, device/template queries
    attendance.py   # Attendance list with filters, Excel export
    adms.py         # ADMS push endpoints (device-initiated, unauthenticated)
    hrm_sync.py     # HRM config, status, manual trigger
    audit.py        # Admin-only audit trail read
    backup.py       # Upload, preview, restore and export a ZKTime.Net .db
  services/
    bootstrap.py    # First-boot admin seeding from API_USERNAME/API_PASSWORD
    pairing.py      # Device pairing-window state
    poller.py       # SDK pull logic (employees, attendance, templates)
    hrm_sync.py     # HRM push logic and batch loop
    zktime_backup.py # Reads a ZKTime .NET SQLite backup into this app's tables
    zktime_export.py # Writes one back out, merged into a template backup
frontend/
  src/
    pages/          # Devices, Employees, Attendance, Users, Settings, Login, ChangePassword
    api.js          # All API calls in one place (cookie-session + CSRF aware)
deploy/
  apache/
    zkteco-sync.conf.example   # Reference reverse-proxy vhost
Dockerfile                     # Multi-stage build; Alpine runtime by default (see DEPLOY.md)
docker-compose.yml             # App + bundled MariaDB (or your own DB via .env)
docker/
  healthcheck.py               # Container liveness probe (GET /healthz with a valid Host)
DEPLOY.md                      # Running it in Docker
```
