# Local dev master key

`master.key` in this directory is a **dev-only** AES-256 key the dev
compose stack mounts into the asr-service and asr-worker containers at
`/etc/mdx/master.key`.

## Generate

```sh
openssl rand 32 > infra/dev/master.key
chmod 0400 infra/dev/master.key
```

## Never commit

`master.key` is matched by `.gitignore`. A `gitleaks` pre-commit hook also
flags it. In production the master key is provisioned by KMS — see ADR-0011.
