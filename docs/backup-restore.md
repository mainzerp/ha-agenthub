# Backup and Restore

## Critical Files

The following files are stored on the Docker volume
(`ha-agenthub-data` for the production stack;
`agent-assist-data` for the legacy local-build stack in
`docker-compose_local.yml`) and must be backed up:

| File | Description | Impact of Loss |
|------|-------------|----------------|
| `/data/.fernet_key` | Encryption key for all secrets | ALL encrypted secrets unrecoverable |
| `/data/agent_assist.db` | SQLite database (settings, traces, conversations) | All configuration and history lost |
| `/data/chromadb/` | sqlite-vec entity-index data **and** `cache.db` (routing + action cache) | Entity index rebuilt on next sync; **cache entries are lost** (restore via cache export) |

The Docker volume name and the in-container paths are independent:
the SQLite filename did not change in the container rename, so
`/data/agent_assist.db` is still the right path inside the
container regardless of which compose file you used to start it.

Optional (included in a volume backup, safe to exclude or delete):

| File | Description | Impact of Loss |
|------|-------------|----------------|
| `/data/logs/app.log*` | Rotating container log file (`LOG_DIR`) | Pre-restart log history gone |
| `/data/logs/warnings.jsonl*` | Persisted WARNING+ log-buffer entries (`.1`, `.2` rotations, about 15 MB max), restored into the Logs page at startup | Pre-restart warnings and errors no longer shown after a restart |
| `/data/backups/pre-migration-*.db` | Automatic pre-migration snapshots of the SQLite database (newest 3 kept), see [Automatic Pre-Migration Snapshots](#automatic-pre-migration-snapshots) | No rollback point for the last schema upgrades |

## Backup Procedure

### Option 1: Volume Backup

```bash
# Stop the container
docker compose stop ha-agenthub

# Create backup
docker run --rm \
    -v ha-agenthub-data:/data \
    -v $(pwd)/backup:/backup \
    alpine \
    tar czf /backup/ha-agenthub-backup-$(date +%Y%m%d).tar.gz /data

# Restart
docker compose start ha-agenthub
```

### Automatic Pre-Migration Snapshots

At startup, when the image ships schema migrations the database has not
applied yet, the container first writes a consistent copy of the
existing SQLite database (`VACUUM INTO`) and only then migrates:

- Location: a `backups/` directory next to the database file
  (`/data/backups/` with the default `SQLITE_DB_PATH`), so the snapshots
  live in the same volume and are part of a volume backup.
- File name: `pre-migration-v<from>-to-v<to>-<UTC timestamp>.db`, where
  `<from>` is the schema version before the upgrade (for example
  `pre-migration-v50-to-v52-20261016T081500Z.db`). The path and size are
  logged at INFO.
- Retention: the newest 3 snapshots are kept; older
  `pre-migration-*.db` files are deleted. Other files in `backups/` are
  left alone.
- No snapshot on a fresh install or when the schema is already current.
- If the snapshot fails (disk full, permissions), startup aborts with an
  error and the database is not migrated. Free space or fix permissions
  and restart. `MIGRATION_BACKUP=false` (see
  [Configuration](configuration.md)) skips the snapshot.
- A snapshot covers only the SQLite database, not `/data/.fernet_key` or
  `/data/chromadb/`. The Fernet key does not change on upgrade, so a
  snapshot stays decryptable with the current key.

### Option 2: Fernet Key Export (passphrase-encrypted)

`POST /api/admin/fernet-key-backup` returns the Fernet key encrypted with
a passphrase you supply. There is no import endpoint; the key is restored
by decrypting the envelope offline (see
[Restore the Fernet Key from an Export](#restore-the-fernet-key-from-an-export)).

1. Log in to obtain the admin session cookie (`agent_assist_session`).
   The login form requires the CSRF token minted by `GET /dashboard/login`:

   ```bash
   BASE=http://localhost:8080
   curl -s -c cookies.txt "$BASE/dashboard/login" -o login.html
   CSRF=$(grep -o 'name="csrf_token" value="[^"]*"' login.html | sed 's/.*value="//; s/"$//')
   curl -s -b cookies.txt -c cookies.txt -X POST "$BASE/dashboard/login" \
       --data-urlencode username=<admin_user> \
       --data-urlencode password=<admin_password> \
       --data-urlencode csrf_token="$CSRF"
   ```

2. Export the encrypted key (JSON admin API: the session cookie is
   enough, no CSRF field):

   ```bash
   curl -s -X POST "$BASE/api/admin/fernet-key-backup" \
       -b cookies.txt \
       -H "Content-Type: application/json" \
       -d '{"passphrase": "<long passphrase>"}' \
       | jq -r .encrypted_key > fernet_key.enc
   ```

Response fields:

| Field | Content |
|-------|---------|
| `status` | `ok`, or `error` with `detail` when the passphrase is empty (still HTTP 200) |
| `encrypted_key` | Base64 of `salt (16 bytes) + nonce (12 bytes) + AES-GCM ciphertext and tag` |
| `warning` | Reminder to store the key securely |

Crypto: AES-256-GCM, no associated data; the AES key is derived from
the passphrase (UTF-8, leading/trailing whitespace stripped) with
PBKDF2-HMAC-SHA256, 600000 iterations, 32-byte output, random 16-byte
salt. The plaintext is the Fernet key string as stored in
`/data/.fernet_key`.

Store `fernet_key.enc` and the passphrase separately (password manager,
vault). Delete `cookies.txt` and `login.html` when done with the admin
API (the cache examples below reuse them).

## Restore Procedure

### Restore a Volume Backup

```bash
docker compose stop ha-agenthub
docker run --rm \
    -v ha-agenthub-data:/data \
    -v $(pwd)/backup:/backup \
    alpine \
    sh -c "rm -rf /data/* /data/.[!.]* && tar xzf /backup/ha-agenthub-backup-<date>.tar.gz -C /"
docker compose start ha-agenthub
curl -s http://localhost:8080/healthz
```

The entrypoint re-applies ownership of `/data` on start, so files
written by the helper container as root are fine.

### Restore a Pre-Migration Snapshot

Use this to roll back to the image version that ran before an upgrade.
Pick the snapshot whose `v<from>` matches the schema of that older image
(the newest `pre-migration-*.db` for the most recent upgrade):

```bash
docker compose stop ha-agenthub
docker run --rm -v ha-agenthub-data:/data alpine ls -l /data/backups
docker run --rm -v ha-agenthub-data:/data alpine sh -c "\
    cp /data/agent_assist.db /data/backups/before-restore.db && \
    rm -f /data/agent_assist.db-wal /data/agent_assist.db-shm && \
    cp /data/backups/pre-migration-v<from>-to-v<to>-<timestamp>.db /data/agent_assist.db"
# Start the older image the snapshot belongs to
HA_AGENTHUB_TAG=<older version> docker compose up -d ha-agenthub
```

- Delete `agent_assist.db-wal` and `agent_assist.db-shm` with the copy;
  a leftover write-ahead log from the newer database corrupts the
  restored file.
- Start the older image that matches the snapshot. Starting the newer
  image again simply re-applies the migrations (and takes a new
  snapshot).
- Data written after the snapshot (settings changes, conversations,
  traces) is lost; `before-restore.db` keeps the newer state.

### Restore the Fernet Key from an Export

The key is only useful together with the database whose `secrets` table
it encrypted. Decrypt the envelope on any machine with the
`cryptography` package (`pip install cryptography`):

```python
import base64
import getpass

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

envelope = base64.b64decode(open("fernet_key.enc").read().strip())
salt, nonce, ciphertext = envelope[:16], envelope[16:28], envelope[28:]
passphrase = getpass.getpass("Passphrase: ").strip()
kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=600_000)
key = AESGCM(kdf.derive(passphrase.encode("utf-8"))).decrypt(nonce, ciphertext, None)
open("fernet_key", "wb").write(key)
```

A wrong passphrase raises `cryptography.exceptions.InvalidTag`. Write
the key into the stopped stack's volume, then start it:

```bash
docker compose stop ha-agenthub
docker run --rm -i -v ha-agenthub-data:/data alpine \
    sh -c "cat > /data/.fernet_key && chmod 600 /data/.fernet_key" < fernet_key
docker compose start ha-agenthub
```

Delete the plaintext `fernet_key` file afterwards.

## Cache Backup via Export and Import

The routing cache and action cache can be backed up and restored
independently of the SQLite database via the cache export/import
admin endpoints. The exported envelope is a
portable JSON document, not an encrypted secret, and can be stored
in version control or shared between environments.

Export both tiers (`tier=routing|action|all`; session cookie from the
login step in Option 2):

```bash
curl -s "$BASE/api/admin/cache/export?tier=all" \
    -b cookies.txt \
    -o cache-backup.json
```

Re-import on the same or a different installation:

```bash
curl -s -X POST "$BASE/api/admin/cache/import" \
    -b cookies.txt \
    -F file=@cache-backup.json \
    -F mode=merge \
    -F "tiers=routing,action"
```

Notes:

- Exports use `format_version: 4` with the shape
  `{"tiers": {"routing": [...], "action": [...]}}`. Only
  `format_version: 4` envelopes are accepted; older exports (including
  legacy `tiers.response` shapes) are rejected and must be regenerated.
- Action entries must have `schema_version: 6`; older action entries
  (exported before the global action-cache rows) are skipped with a
  warning and re-learned from live turns.
- `mode=replace` clears the targeted tiers before importing.
  `mode=merge` keeps existing entries.
- Embeddings are imported as exported (there is no re-embedding on
  import). When the embedding model recorded in the envelope differs
  from the active one, the import emits a warning -- consider
  replacing the routing tier after an embedding-model switch.
- Use this path to recover from an accidental cache flush or to seed
  a freshly-built container with a known-good cache snapshot.

See [API reference](api-reference.md) (`Admin -- Cache` section)
for the full endpoint contract.

## Key Rotation (Manual Procedure)

There is no in-app Fernet rotation workflow. Rotation is a manual
checklist:

1. Stop the container (`docker compose stop ha-agenthub`).
2. Back up `/data/.fernet_key` (volume backup or
   `docker cp ha-agenthub:/data/.fernet_key ./fernet_key.bak`)
   so the existing secrets can be decrypted if rotation needs to be
   rolled back.
3. Generate a new Fernet key out-of-band (for example,
   `python -c "from cryptography.fernet import Fernet;
   print(Fernet.generate_key().decode())"`) and write it to
   `/data/.fernet_key` inside the volume (same helper-container command
   as in [Restore the Fernet Key from an Export](#restore-the-fernet-key-from-an-export)).
4. Restart the container (`docker compose start ha-agenthub`).
   Existing encrypted secrets become unreadable; you will need to
   re-enter the HA token, the container API key, and any LLM
   provider keys via the dashboard. Existing dashboard sessions are
   invalidated (the session signing key is derived from the Fernet
   key); log in again.
5. To rotate only the container API key (not the Fernet key), call
   `POST /api/admin/container-api-key/rotate` from an authenticated
   admin session and update the integration with the returned
   value.
