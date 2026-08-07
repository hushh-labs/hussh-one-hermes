# Xtreme Burst — the husshone design record

These four documents are migrated **verbatim** from `hushh-labs/husshone` at commit
`80cb297` (2026-08-07). They are kept because they are the only written record of why the
burst system is shaped the way it is — the teardown discipline, the BYOC credential
precedence, the provider seam, the TPU contract.

## Read them as history, not as current state

They describe the **v1 control plane as built in husshone**: a Next.js app with
`/api/one/burst` routes, a Prisma job store and a macOS Swift client. Burst orchestration
has since moved to Hermes, and husshone is no longer the target for new burst work.

Two specific things will mislead if taken at face value here:

- **Vocabulary.** These documents say `puppy` (the local Mac) and `gcp` (the cloud). Hermes
  uses `device` and `cloud`. The mapping is exact — see
  [the architecture record](../../architecture/xtreme-burst.md) § *Vocabulary*.
- **"One Puppy" is a Mac.** The husshone design assumes a single high-end Mac tier. Hermes
  places workloads on six device classes, including Windows workstations.

Paths in these files (`src/lib/burst/…`, `provisioning/`, `docs/specs/`) refer to the
**husshone** tree, not this one. Three relative links —
`../provisioning/README.md`, `./customer/getting-started.md`, `./specs/README.md` — pointed
at husshone directories that were not migrated and therefore do not resolve here. They are
left as written rather than rewritten to dead ends, so the original document stays faithful.

| File | What it is |
|---|---|
| [design.md](./design.md) | The implementation guide: flow, placement tier, GCP provider, BYOC credentials, env vars |
| [whitepaper.md](./whitepaper.md) | The product argument for burst compute |
| [test-plan.md](./test-plan.md) | Test strategy and coverage for the v1 control plane |
| [burst-control-plane.openapi.yaml](./burst-control-plane.openapi.yaml) | The v1 wire contract — the authority for the `puppy`/`gcp` enums |

---

### Related
- [Xtreme Burst architecture and migration record](../../architecture/xtreme-burst.md) — start here
- [Architecture — the overlay model](../../architecture/README.md)
