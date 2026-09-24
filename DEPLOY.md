# Deploying with Docker

One image holds the backend and the built UI. `docker-compose.yml` adds a
MariaDB beside it. Everything is configured from `.env`.

## Deploy

```bash
git clone <repo-url>
cd zkteco-sync
cp .env.example .env
chmod 600 .env
```

Edit `.env`. The minimum:

| Key | Value |
|-----|-------|
| `SECRET_KEY` | `python3 -c "import secrets; print(secrets.token_hex(32))"` |
| `ALLOWED_HOSTS` | The hostname(s) the app is reached as. Required in production. |
| `API_USERNAME` / `API_PASSWORD` | First-boot admin. Change it at first login, then delete both lines. |
| `DB_HOST=db` and `DB_PORT=3306` | The bundled MariaDB, as the app sees it. |
| `DB_NAME` / `DB_USER` / `DB_PASSWORD` | Anything you like, but **`DB_USER` must not be `root`** — the MariaDB image refuses to create a regular user by that name, and the app never starts. |
| `TRUSTED_PROXIES=172.30.0.1` | Only if Apache on this host proxies to the container. See below. |

Start it:

```bash
docker compose up -d --build
docker compose logs -f app
```

First boot creates the database, the schema and the admin account. The app
answers on `127.0.0.1:8000`.

Check it is up:

```bash
curl -H 'Host: zk.example.com' http://127.0.0.1:8000/healthz
```

The `Host` header has to be one of `ALLOWED_HOSTS` or the request is refused.

## Upgrade

```bash
git pull
docker compose up -d --build
```

The database lives in the `dbdata` volume and survives rebuilds. Schema
migrations run automatically on boot.

## Behind Apache

The vhost ([`deploy/apache/zkteco-sync.conf.example`](deploy/apache/zkteco-sync.conf.example))
needs no change — it still proxies to `127.0.0.1:8000`.

But set **`TRUSTED_PROXIES=172.30.0.1`**, not `127.0.0.1`. Under Docker the
app sees Apache's connection arriving from the bridge gateway, not loopback.
Get this wrong and every device looks like it came from `172.30.0.1`, so
per-device IP allowlisting refuses all of them.

## Using your own database

Point `DB_ENGINE` / `DB_HOST` / `DB_PORT` at your server, then start only the
app:

```bash
docker compose up -d --build --no-deps app
```

A database on this same host is `DB_HOST=host.docker.internal`, and it must
listen on more than `127.0.0.1`. Connections arrive from `172.30.0.0/24`, so
the account has to be allowed from there (`'zkteco'@'172.30.0.%'`).

**SQL Server** needs the Debian image, since the default Alpine one carries no
ODBC driver. Set `DB_ENGINE=mssql` and
`DB_ODBC_DRIVER=ODBC Driver 18 for SQL Server`, then:

```bash
ZK_TARGET=runtime-mssql docker compose up -d --build --no-deps app
```

Windows Authentication does not work from a Linux container — use a SQL login.

## Prebuilt images

Published to GHCR on every `v*` tag, for amd64 and arm64:

```
ghcr.io/<owner>/zkteco_sync:<version>          Alpine (default)
ghcr.io/<owner>/zkteco_sync:<version>-mssql    Debian + ODBC Driver 18
```

Point the `image:` line in `docker-compose.yml` at one and drop `--build`.

## Commands

```bash
docker compose logs -f app      # application log
docker compose restart app      # restart the app
docker compose down             # stop, keep the database
docker compose down -v          # stop AND delete the database
```

## See also

- [README → Setup](README.md#setup) — native install, and every `.env` key
- [SECURITY.md](SECURITY.md) — the security model
