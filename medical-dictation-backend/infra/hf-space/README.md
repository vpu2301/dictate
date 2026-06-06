# HF Space — `medical-dictation-demo/clinical-dictation-uk-en`

This directory holds the Docker assets that ship the slimmed stack to
Hugging Face Spaces. See ADR-0017 for why this shape is acceptable for
the demo only and forbidden in production.

## Auth model

The HF Space embeds **real Keycloak** with the sprint-02 realm. There
is no anonymous demo-auth — instead, the HF Space repo is private and
the HF API key gates access. Inside the Space, users log in via the
standard `POST /auth/login` flow with a seeded `clinician@demo.test`
account (password: `demo-please-change`, rotate before public flip).

## Files

| File              | Purpose |
| ----------------- | ------- |
| `Dockerfile`      | Multi-stage CUDA image. Bundles 6 services + Postgres + Redis + Keycloak (Java) + nginx + supervisord. |
| `supervisord.conf`| Process tree: Postgres → Redis → Keycloak → 5 backend services → nginx + daily-restart cron. |
| `nginx.conf`      | Public port 7860; routes `/auth/realms/*` to Keycloak, `/auth/*` to auth-service, `/asr/*`, `/templates/*`, `/nlp/*`, `/dictate/*`, `/ws/dictate`. |
| `start.sh`        | Entrypoint: initdb on tmpfs, apply migrations + seeds, stage Keycloak realm with seeded user, hand off to supervisord. |
| `init-db.sh`      | Debug helper: wipe pgdata + re-init. |

## Build

```sh
docker build -t mdx-hf-space -f infra/hf-space/Dockerfile .
```

## Deploy

Triggered by `.github/workflows/hf-deploy.yml` on `git push origin demo`.
Uses the `HF_TOKEN` secret to push to the private Space repo.

## Privacy contract (ADR-0019)

Image-baked envvars (cannot be relaxed at runtime):

- `MD_OBJECT_STORE_DISABLED=true` — sprint-03 `EncryptedObjectStore.put`
  refuses; no MinIO; no audio on disk.
- `DEMO_AUDIO_PURGE_ON_FINALIZE=true` — sprint-04 finalize wipes the
  tmpfs ring buffer immediately on `EndSession`, before the client
  receives `SessionTerminated`.
- `MDX_TMPFS_ROOT=/run/dictation` — mounted as tmpfs (RAM-only).
- `PGDATA=/run/pgdata` — Postgres on tmpfs; container restart wipes state.

A daily 06:00 UTC supervisord-managed restart guarantees no audio
state ever spans days.

## Local smoke test

```sh
docker run --rm --gpus all -p 7860:7860 mdx-hf-space &
sleep 120   # cold start (CUDA + Whisper + Keycloak + 6 services)
curl http://localhost:7860/_health    # → {"status":"ok"}
# Realm endpoint:
curl http://localhost:7860/auth/realms/medical-dictation/protocol/openid-connect/certs
# Backend login:
curl -X POST http://localhost:7860/auth/login \
  -d 'email=clinician@demo.test' -d 'password=demo-please-change'
```
