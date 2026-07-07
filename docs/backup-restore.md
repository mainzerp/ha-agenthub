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
| `/data/chromadb/` | sqlite-vec entity-index data (legacy path) | Rebuilt on next entity sync |

The Docker volume name and the in-container paths are independent:
the SQLite filename did not change in the container rename, so
`/data/agent_assist.db` is still the right path inside the
container regardless of which compose file you used to start it.

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

### Option 2: Fernet Key Export

Export the Fernet key via the admin API:

```bash
curl -s http://localhost:8080/api/admin/fernet-key-backup \
  -H "Cookie: agent_assist_session=<session_cookie>" | jq .key
```

Store this key in a secure location (password manager, vault).

## Restore Procedure

1. Stop the container.
2. Extract the backup into the Docker volume.
3. Start the container and verify via `/api/health`.

## Cache Backup via Export and Import

The routing cache and action cache can be backed up and restored
independently of the SQLite database via the cache export/import
admin endpoints. The exported envelope is a
portable JSON document, not an encrypted secret, and can be stored
in version control or shared between environments.

Export both tiers:

```bash
curl -s "http://localhost:8080/api/admin/cache/export?tier=all" \
    -H "Cookie: agent_assist_session=<session_cookie>" \
    -o cache-backup.json
```

Re-import on the same or a different installation:

```bash
curl -s -X POST "http://localhost:8080/api/admin/cache/import" \
    -H "Cookie: agent_assist_session=<session_cookie>" \
    -F file=@cache-backup.json \
    -F mode=merge \
    -F "tiers=routing,action" \
    -F re_embed=false
```

Notes:

- New exports use `format_version: 2` and the `tiers.action.entries`
  shape. Imports still accept `format_version: 1` envelopes that
  carry `tiers.response.entries`, so backups produced with the
  legacy response-cache naming remain importable on current versions.
- `mode=replace` clears the targeted tiers before importing.
  `mode=merge` keeps existing entries.
- `re_embed=true` recomputes embeddings on import; useful when
  moving between embedding models or providers.
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
   `/data/.fernet_key` inside the volume.
4. Restart the container (`docker compose start ha-agenthub`).
   Existing encrypted secrets become unreadable; you will need to
   re-enter the HA token, the container API key, and any LLM
   provider keys via the dashboard.
5. To rotate only the container API key (not the Fernet key), call
   `POST /api/admin/container-api-key/rotate` from an authenticated
   admin session and update the integration with the returned
   value.
