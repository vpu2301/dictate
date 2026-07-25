# Keycloak realm import

`realm-export.json` is imported at container start (`--import-realm`). Keycloak deserializes it
into `RealmRepresentation` with **unknown properties rejected**, so the file must contain only
real Keycloak fields — no `_comment_*` keys, no JSON5/JSONC comments. Adding one fails the
import with `UnrecognizedPropertyException` and the container exits 1, taking every
`depends_on: keycloak` service down with `dependency failed to start`.

Notes that would otherwise be inline comments live here.

## `sslRequired: "NONE"` — DEV ONLY

`external` is the Keycloak default and the correct production value: it demands HTTPS for any
request Keycloak deems non-private. On Docker Desktop a browser on the host reaches Keycloak
through the bridge gateway, which Keycloak treats as external, so every realm endpoint answered
`403 HTTPS required` — the admin console loaded but could never sign in. The SPA was unaffected
because auth-service talks to Keycloak inside the Docker network.

Production MUST set this back to `external` and terminate TLS in front of Keycloak; see
`infra/edge/nginx.conf.template` for how the stack already does that for the public surface.

The `master` realm is created by Keycloak bootstrap and is not covered by this file. Relax it
separately for local admin-console access:

```bash
docker exec medical-dictation-keycloak-1 /opt/keycloak/bin/kcadm.sh \
  update realms/master -s sslRequired=NONE
```
