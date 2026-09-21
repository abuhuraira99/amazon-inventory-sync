# Deployment

Two supported paths: Docker Compose, and running directly on a host. Both are
described here. Neither exposes the dashboard to the internet, and that is not
an oversight.

Before you start, you need:

- PostgreSQL 16 (Compose provides it)
- credentials for the supplier's file server
- an Amazon SP-API application — see [AMAZON-APP-SETUP.md](AMAZON-APP-SETUP.md),
  and note the warning there about which roles **not** to request

---

## Sizing

Modest. The pipeline is I/O-bound and streams rather than loading.

| | Minimum | Comfortable |
|---|---|---|
| RAM | 2 GB | 4 GB |
| vCPU | 1 | 2 |
| Disk | 20 GB | 40 GB+ |

Disk is the one to think about. The database grows with
`vendor_product_history` — one row per product change, so the first full feed
writes one per product in the supplier's catalogue — and reports accumulate
several megabytes per run. Both are governed by retention settings. If the disk
is small, shorten them before you need to.

---

## Path 1: Docker Compose

```bash
git clone https://github.com/abuhuraira99/amazon-inventory-sync
cd amazon-inventory-sync

cp .env.example .env
# Fill in MASTER_KEY and SESSION_SECRET. Compose sets DATABASE_URL.
# Leave the three passwords BLANK — enter them in the dashboard instead.

docker compose up -d
docker compose exec app alembic upgrade head
docker compose logs -f app
```

Generate the two required secrets:

```bash
python -c "import secrets,base64; print(base64.b64encode(secrets.token_bytes(32)).decode())"   # MASTER_KEY
python -c "import secrets; print(secrets.token_urlsafe(32))"                                    # SESSION_SECRET
```

**Back up `MASTER_KEY` somewhere separate from your database backups.** Losing
it means re-entering every credential. Storing it *with* the backups defeats
the point of encrypting them.

---

## Path 2: directly on a host

```bash
git clone https://github.com/abuhuraira99/amazon-inventory-sync
cd amazon-inventory-sync

python -m venv .venv
. .venv/bin/activate                  # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env                  # fill in, including DATABASE_URL
alembic upgrade head

uvicorn app.main:app --host 127.0.0.1 --port 8000
```

There are helper scripts in `scripts/` for writing `.env` and creating the
database.

### Running it as a service

Whatever supervisor you use, two things matter more than the choice of
supervisor.

**Set the working directory to the repository root.** `alembic.ini`, `.env` and
`data/` are all resolved relative to it.

**Assume stdout is discarded.** Many service managers do not capture it. The
application already logs to `data/logs/app.log` (rotating, 5 MB × 5) precisely
because of this, but it means the log file is your only record — check it is
being written before you rely on it.

A systemd unit, for example:

```ini
[Unit]
Description=Amazon Inventory Sync
After=network.target postgresql.service

[Service]
Type=simple
User=inventory
WorkingDirectory=/opt/amazon-inventory-sync
ExecStart=/opt/amazon-inventory-sync/.venv/bin/uvicorn app.main:app \
          --host 127.0.0.1 --port 8000
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

On Windows, a Scheduled Task set to run at startup works and is what the
scripts in `scripts/` assume. Note that a Scheduled Task discards stdout
entirely — the log file is genuinely the only record.

---

## Reaching the dashboard

**The application binds to `127.0.0.1`. Do not change that, and do not publish
port 8000.**

Binding to loopback is most of this system's security. It holds credentials to
an account that takes real orders, and it is a small project without the
hardening budget of something designed to sit on the open internet.

Use one of:

```bash
ssh -L 8000:127.0.0.1:8000 user@host      # SSH tunnel — simplest
```

- **Tailscale** — put the host on a tailnet and reach it by its tailnet address
- **Cloudflare Tunnel** — if you want a real hostname with access control in
  front of it

If you put a reverse proxy in front, terminate TLS there, keep the application
bound to loopback, and put authentication at the proxy as well. The
application's own login is not the only thing that should stand between the
internet and this.

---

## First run

Order matters — each step is cheap to verify and expensive to skip.

**1. Log in.** A single administrator account is seeded at first start. Change
the password immediately. Enable two-factor if more than one person will have
access.

**2. Enter the credentials** on the Settings page rather than in `.env`. They
are encrypted before the request finishes, and nobody else ever holds them.

**3. Press "Test the vendor connection."** If it connects but finds no feed
files, it lists what the folder *does* contain — use that rather than guessing.
A credentials sheet saying the path is `/` while the files sit one folder down
is the single most common first-run problem.

**4. Press "Test Amazon."** This uses a read-only call and is safe to press at
any time. It will tell you which credential is missing or wrong, specifically.

**5. Set the timezone to the SUPPLIER'S timezone**, not yours. This is not a
display preference — it decides which feed counts as today's. Getting it wrong
makes the system skip the newest feed every day, silently, with runs that look
completely healthy. See
[ADAPTING.md](ADAPTING.md#timezones).

**6. Set `sku_prefixes_in_scope`.** It ships **empty, which means nothing is in
scope** — the system will touch no listing at all until you tell it what it
owns. Use the Coverage report to decide which prefixes are safe.

**7. Leave `sync_mode` on `dry_run`** and go to [OPERATIONS.md](OPERATIONS.md)
for the rollout. Do not skip this part.

---

## Backups

Back up the database. `push_items` is the undo trail, and without it undo
cannot restore anything.

```bash
pg_dump -Fc inventory > backup-$(date +%F).dump          # host
docker compose exec -T db pg_dump -Fc -U postgres inventory > backup.dump
```

There is a `scripts/backup.ps1` for Windows.

Two things people get wrong here:

- **Store `MASTER_KEY` separately from the dumps.** Together, they are
  plaintext credentials.
- **Restore into a scratch database occasionally.** An untested backup is a
  belief, not a backup.

`alembic downgrade -1` on the initial migration **drops every table**, undo
trail included. It is not a rollback mechanism for a live deployment. Restore
from a dump instead.

---

## Updating

```bash
git pull
pip install -r requirements.txt        # in case dependencies moved
alembic upgrade head                   # in case the schema moved
# then restart the service
```

**Say both parts.** "Pull and restart" — a pull without a restart leaves the
old code running, and the symptom is a fix that appears not to work.

Check the running version on the dashboard after restarting rather than
assuming.

---

## After a crash

The system is built to recover on its own, but it is worth knowing what it
does so you can confirm it happened.

**Runs stuck in `RUNNING`** are marked `FAILED` at start-up. Without that, a
restart mid-run leaves a run showing "Running" for ever.

**Batches stuck in `SENDING`** are settled on the next run by reading Amazon's
actual state — stage 0. Some items may have been accepted before the crash and
some not; the system finds out rather than guessing.

**Nothing is lost.** `push_items` with their `previous_quantity` were committed
before anything was sent, so undo still works for whatever did go out. That is
[the durability barrier](SAFETY-MODEL.md#the-durability-barrier), and it is the
reason a crash mid-send is recoverable rather than a mystery.

If a run is genuinely wedged, check `data/logs/app.log` first. It is the record
that survives when stdout does not.

---

## Monitoring

`GET /api/status` returns a small JSON object suitable for an uptime checker:
last run, its outcome, whether the scheduler is alive, and whether anything is
unconfirmed. It is read-only, like everything under `/api`.

Configure the alert email addresses in Settings. Alerts are queued and flushed
rather than sent inline, so an SMTP timeout cannot fail a run that otherwise
succeeded.

Worth alerting on:

- runs halted by a guardrail (expected occasionally; a pattern is not)
- catalogue refresh failures (the whole comparison depends on it)
- low disk
- **no run at all** in the last few intervals — the failure that produces no
  alert by definition, and therefore the one most worth watching externally
