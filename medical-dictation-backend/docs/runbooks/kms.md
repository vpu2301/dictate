# Runbook — KMS (Vault Transit master key)

Sprint 16 / ADR-0011 amendment. The master KEK lives in Vault's Transit
engine; services wrap/unwrap tenant KEKs remotely and never see the
master. Dev keeps the file provider (`MDX_MASTER_KEY_PROVIDER=file`,
the default) — everything below concerns `vault` mode.

## Configuration (per service)

| Env | Meaning | Default |
|-----|---------|---------|
| `MDX_MASTER_KEY_PROVIDER` | `file` or `vault` | `file` |
| `MDX_VAULT_ADDR` | Vault base URL | `http://localhost:8200` |
| `MDX_VAULT_TOKEN` | token with `transit` encrypt/decrypt on the key | (empty — required) |
| `MDX_VAULT_TRANSIT_KEY` | Transit key name | `mdx-master` |
| `MDX_VAULT_TRANSIT_MOUNT` | Transit mount | `transit` |
| `MDX_MASTER_KEY_PATH` | file fallback for un-migrated rows | `/etc/mdx/master.key` |

Signing-service additionally: `MDX_HMAC_KEYS_FROM_VAULT=true` +
`MDX_VAULT_HMAC_KV_PATH` (default `mdx/signing`, KV-v2 fields
`signer_ipn_hmac_key` / `public_verify_ip_hmac_key`).

## One-time setup (dev parity: a local Vault)

```bash
docker run -d --name mdx-vault -p 8200:8200 \
  -e VAULT_DEV_ROOT_TOKEN_ID=root hashicorp/vault:1.16
docker exec -e VAULT_ADDR=http://127.0.0.1:8200 -e VAULT_TOKEN=root \
  mdx-vault vault secrets enable transit
docker exec -e VAULT_ADDR=http://127.0.0.1:8200 -e VAULT_TOKEN=root \
  mdx-vault vault write -f transit/keys/mdx-master type=aes256-gcm96
```

Use a plain (non-derived, non-convergent) `aes256-gcm96` key — the
startup self-check round-trips a probe and fails otherwise.

## Migration (file → vault)

1. Deploy services with `MDX_MASTER_KEY_PROVIDER=vault` **and the key
   file still mounted** — the composite provider reads old rows via the
   file fallback; new tenants wrap under Vault immediately.
2. Re-wrap:
   ```bash
   MDX_VAULT_ADDR=… MDX_VAULT_TOKEN=… \
   uv run --project libs/crypto python scripts/kms/rewrap-tenant-keks.py --dry-run
   # review counts, then run without --dry-run
   ```
   Transactional per row, resumable, verifies each new wrapping
   round-trips before commit, audits `kms.rewrap.completed` (sec).
3. Confirm `SELECT count(*) FROM tenant_keks WHERE kek_master_id = 'file-v1'`
   is 0, then unmount/delete the key file. Startup logs stop printing
   `master_key.file_fallback_active`.

Rollback: the script's inverse is running it with file as target — not
implemented on purpose; instead restore the key file (the composite
keeps decrypting both masters) and investigate.

## § vault-unreachable

Symptom: service exits at startup with
`MasterKeyError: Vault Transit unreachable …` (fail-closed, same
posture as master-key-missing) — or, for signing-service HMAC sourcing,
`Vault KV unreachable`.

1. `curl -s $MDX_VAULT_ADDR/v1/sys/health` — sealed? `vault operator unseal`.
2. Token expired/revoked → issue a new one with a policy granting
   `update` on `transit/encrypt/mdx-master` + `transit/decrypt/mdx-master`
   (+ `read` on `secret/data/mdx/signing` for signing-service).
3. While Vault is down, already-running workers keep serving: the ≤60 s
   tenant-KEK cache expires, after which decrypts fail loudly. Restore
   Vault; nothing needs re-keying.

## § hmac-keys

Rotation stays an operator action: write new hex values to the KV path,
rolling-restart signing-service. Remember the ADR-0027 warning — the
signer-ІПН HMAC key rotation orphans stored HMACs.

## Key rotation (Transit)

`vault write -f transit/keys/mdx-master/rotate` — new wraps use the new
version; old ciphertexts decrypt as long as `min_decryption_version`
allows. To re-bind every row to the newest version, run the re-wrap
script (vault → vault re-wrap is a no-op id-wise but refreshes the
embedded `vault:vN:` version).
