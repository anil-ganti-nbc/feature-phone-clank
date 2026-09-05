# Discord delivery activation runbook (Hetzner)

**Code readiness is not production activation.** Everything in `e48a56e` makes
activation *safe to attempt*; nothing in it turns delivery on, chooses a
cutoff, or configures a webhook. This runbook is the separate, deliberate
operator procedure that does.

Run it top to bottom on the Hetzner host. Every step before step 7 is
read-only or local-config only: nothing contacts Discord until you explicitly
authorize step 7.

---

## 0. Preconditions

- You have decided that delivery should be enabled at all.
- You have a Discord webhook URL for the target channel, **not yet** written
  anywhere on the host.
- You accept the ambiguous-delivery limitation in §9.

---

## 1. Deploy the code and confirm the revision

```bash
cd /srv/feature-phone-clank        # adjust to the real checkout path
git fetch origin
git log --oneline -1 origin/main   # expect e48a56e or a later descendant
git status --porcelain             # expect clean before switching
git merge --ff-only origin/main
git rev-parse HEAD
```

Do not proceed if `HEAD` is not the revision you intend to run.

---

## 2. Inspect the live database and its pending queue — read-only

Look before you touch. This is a plain SQLite read; it opens nothing
read-write and triggers no migration:

```bash
sqlite3 "file:/srv/feature-phone-clank/data/feature_phone_clank.db?mode=ro" <<'SQL'
.mode box
SELECT COALESCE(MAX(version),0) AS schema_version FROM schema_migrations;
SELECT status, COUNT(*) FROM notifications GROUP BY status;
SELECT MIN(e.detected_at) AS oldest_pending, MAX(e.detected_at) AS newest_pending
  FROM notifications n JOIN events e ON e.id = n.event_id
 WHERE n.provider='discord' AND n.status='pending';
SELECT SUM(attempts>0) AS already_attempted, SUM(attempts=0) AS never_attempted
  FROM notifications WHERE provider='discord' AND status='pending';
SQL
```

Record the numbers. **The Windows machine's single pending Nokia 110 4G row
tells you nothing about this backlog** — the two databases are unrelated, and
the Hetzner queue may be far larger. Decide from what you just read.

---

## 3. Take a verified, SQLite-safe backup

A file copy of a live WAL database is not a backup. Use the online backup API:

```bash
python3 - <<'PY'
import sqlite3, hashlib, os
src = "/srv/feature-phone-clank/data/feature_phone_clank.db"
dst = "/srv/feature-phone-clank/backups/pre-activation-$(date +%F).db"
os.makedirs(os.path.dirname(dst), exist_ok=True)
s, d = sqlite3.connect(src), sqlite3.connect(dst)
with d: s.backup(d)
s.close(); d.close()
c = sqlite3.connect(dst)
print("integrity:", c.execute("PRAGMA integrity_check").fetchone()[0])
print("notifications:", c.execute("SELECT COUNT(*) FROM notifications").fetchone()[0])
print("sha256:", hashlib.sha256(open(dst,'rb').read()).hexdigest())
c.close()
PY
```

Do not continue unless `integrity_check` is `ok`. Keep this file until §8 has
passed; it is your only rollback for the schema migration in §4.

---

## 4. Apply the v5 → v6 migration deliberately

`e48a56e` moves the expected schema from v5 to v6 (`notifications.not_before`
plus the `delivery_policy` table). The migration is additive and preserves
sent/failed history, attempt counts and payloads, and it invents no cutoff.

It runs automatically the first time *any* read-write `SqliteStore` opens the
database — which means an ordinary `deliver`, `run`, or dashboard start will
perform it as a side effect. Do it on purpose instead, with §3's backup in
hand, and verify:

```bash
python3 -m feature_phone_clank.cli --db data/feature_phone_clank.db notifications
sqlite3 "file:data/feature_phone_clank.db?mode=ro" \
  "SELECT COALESCE(MAX(version),0) FROM schema_migrations;"   # expect 6
```

**Consequence to plan for:** once migrated, any *older* binary or frozen
bundle still expecting v5 will refuse the database with `INCOMPATIBLE_NEWER`
and fail closed (HTTP 503 on the dashboard). That is the compatibility gate
working correctly, not a fault — but every process on the host that opens this
database must be running `e48a56e` or later before you migrate. Rebuild or
redeploy them first.

---

## 5. Preview the proposed cutoff — still read-only, still sends nothing

Choose a candidate activation timestamp (UTC, ISO-8601). "Now" is the usual
choice: it means *nothing that predates activation is ever pushed*.

```bash
python3 -m feature_phone_clank.cli --db data/feature_phone_clank.db \
    deliver --preview --cutoff "2026-09-05T00:00:00Z"
```

Read `proposed_cutoff_effect`:

- `would_send` — rows that would go out on the next drain. For a
  future-only activation this should usually be **0** at the moment you set
  it, with real traffic arriving only from later collection runs.
- `would_hold` — the preserved backlog.
- `breakdown` — why each row is held: `held_before_cutoff`,
  `held_unknown_event_time` (no event row or unusable timestamp — held on
  purpose), `held_policy_unreadable`.

Also check `pending_provenance_gaps`. A non-zero `null_event_id` or
`orphaned_event_id` is worth understanding *before* activation, not after.

Iterate on `--cutoff` until `would_send` is what you actually intend. This
command never writes, never sends, and never resolves the webhook URL.

---

## 6. Install the cutoff, then configure the secret outside Git

Install the policy **first**, so the queue is already gated before a webhook
exists anywhere on the host:

```bash
python3 -m feature_phone_clank.cli --db data/feature_phone_clank.db \
    delivery-activation --set "2026-09-05T00:00:00Z"
python3 -m feature_phone_clank.cli --db data/feature_phone_clank.db \
    delivery-activation                       # confirm: configured=true, unreadable=false
```

An unparseable value is refused with exit 2 and nothing is persisted.

Then the secret. It is read from `FEATURE_PHONE_CLANK_DISCORD_WEBHOOK_URL`
and must never enter the repository, a tracked config file, a shell history
file, or a log:

```bash
# systemd drop-in, root-owned, 0600 — not in Git
sudo install -m 0600 /dev/null /etc/feature-phone-clank/discord.env
sudo tee /etc/feature-phone-clank/discord.env >/dev/null <<'ENV'
FEATURE_PHONE_CLANK_DISCORD_WEBHOOK_URL=PASTE_HERE
ENV
sudo chmod 0600 /etc/feature-phone-clank/discord.env
```

Reference it with `EnvironmentFile=` in the unit. Verify it is not readable by
the service's unprivileged user beyond what it needs, and confirm the value
never appears in `git grep`, `journalctl`, or the app's own logs. (The code no
longer emits it in any error path — that is what §C of `e48a56e` fixed — but
verify your own plumbing.)

---

## 7. One labelled test send — only when you authorize it

This is the first moment anything reaches Discord. It posts a clearly marked
`FEATURE-01 TEST` embed that references no real product and no `events` row:

```bash
python3 -m feature_phone_clank.cli --db data/feature_phone_clank.db test-notify \
    --note "activation check $(date -u +%FT%TZ)"
```

Expect `{"sent": true, ...}` and exactly one test message in the channel. On
failure you get a bounded category (`connection_error`, `http_unauthorized`,
`http_404_webhook_not_found`, …) — never the URL. `http_unauthorized` or
`http_404_webhook_not_found` almost always means the secret is wrong or the
webhook was deleted; fix that before going further.

Then confirm the held backlog is still held and untouched:

```bash
python3 -m feature_phone_clank.cli --db data/feature_phone_clank.db deliver --preview
```

`counts_by_status.pending` must be unchanged from §2, and those rows must
still show `attempts = 0` (a test send must not have disturbed them).

---

## 8. Verify later natural delivery

Do **not** force a drain to prove it works. Let the normal scheduled run
produce a genuinely new, post-cutoff event and deliver it on its own:

```bash
# after the next scheduled collection
python3 -m feature_phone_clank.cli --db data/feature_phone_clank.db notifications --status sent --limit 5
python3 -m feature_phone_clank.cli --db data/feature_phone_clank.db deliver --preview
```

Success looks like: a new row in `sent` whose event timestamp is after the
cutoff, the historical backlog still `pending` and still `attempts = 0`, and
`held` counts steady in the drain summary.

If a `429` appears, the drain stops itself, records a `not_before` floor on
that row, and does not burn an attempt — check `deferred` in the summary and
simply wait for the window rather than retrying manually.

---

## 9. Standing limitations

- **Ambiguous-delivery window (not fixed, not fixable here).** A row is marked
  `sent` *after* the HTTP call returns. If the process dies between Discord
  accepting the request and the row being marked, the next drain re-posts it
  and the channel sees a duplicate. Serialization removed concurrent
  duplication; it cannot remove this. Any outbox that records after sending
  has this window, and closing it would require idempotency keys Discord
  webhooks do not offer.
- **Historical replay is deliberately manual.** Held rows only ever go out via
  an explicit `deliver --include-held`. Nothing does this automatically, and
  no other flag implies it. Think before running it: it releases the entire
  pre-cutoff backlog at the channel.
- **`--preview` migrates schema as a side effect on a v5 database**, because
  it opens a read-write store. On an already-v6 database it is inert. This is
  why §4 comes before §5.
- **Rate-limit handling is per-row and bounded.** Retry-After is honoured up to
  15 minutes; a longer or non-numeric value falls back to 60 seconds rather
  than parking the queue indefinitely.
