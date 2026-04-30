# Runlog Vocabularies — Allow-List Data

> Part of the **[Runlog](https://github.com/runlog-org)** project — see the [project home](https://github.com/runlog-org) for the overview.

**Repo:** [`runlog-org/runlog-vocabularies`](https://github.com/runlog-org/runlog-vocabularies) — public, CC-BY-SA-4.0 (this is data, not code)
**Role:** allow-list data consumed by the server's sanitizer and the verifier's pre-sign check. Default-deny — any token not in this registry needs an explicit `$LITERAL_N` declaration with a reason at submission time.

Registered stdlib identifiers, framework-public APIs, and protocol tokens per domain. The server's allow-list tokenizer and the verifier's pre-sign check both consume these files to decide whether a submission's tokens are permissible without a declared-literal wrapper.

Community-PR'd. Each domain file documents its upstream source in the `version` field so the token list is reproducible. When an extractor exists, periodic audits re-run it and flag drift against the live sources; vocabularies curated by hand carry the same `version` for human-review traceability.

## Layout

- `domains/<tag>.yaml` — per-domain vocabularies (languages, frameworks, services); filename stem must match the `domain` field
- `protocols/<tag>.yaml` — per-protocol vocabularies (HTTP, OAuth, TLS, …); filename stem must match the `protocol` field
- `scripts/` — extractors that regenerate vocabulary files from upstream documentation

Each `domains/<tag>.yaml` has four fields:

```yaml
domain: <tag>          # string, must match the filename stem (e.g. "python")
description: <summary> # one-line human summary of what is covered
version: "<id>"        # string identifier for the upstream version this reflects
tokens:                # flat list of allowed token strings
  - token_one
  - token_two
```

`protocols/<tag>.yaml` is identical except the first field is `protocol:` instead of `domain:`. The category in `scope-registry.yaml` (e.g. `protocol`, `language`, `framework`) determines which directory a vocabulary belongs in.

Token matching is **case-insensitive**: the loader lowercases every token at load time, so `Region` and `region` (or `SELECT` and `select`) collapse to a single allowed entry. Do not list a token in multiple cases — pick one canonical form (lowercase is conventional) and keep the file deduplicated.

New contributors: add a single YAML file at `domains/<tag>.yaml` (or `protocols/<tag>.yaml` for a protocol) following the shape above. A reproducible extractor at `scripts/generate-<tag>.sh` is encouraged when the upstream source has a stable machine-readable form (stdlib reference, OpenAPI spec, RFC IANA registry); hand-curated lists are fine when no such source exists. PRs must pass the allow-list validator (no PII, no vendor-specific strings, no duplicates).

## Scope Registry

`scope-registry.yaml` (this directory, top level) is the authoritative list of every domain tag the platform considers "public" for the purposes of `runlog_submit`'s scope rule.  It is consumed exclusively by `server/src/runlog/sanitize/scope.py`, which exposes the `is_public_domain` / `validate_domains` / `filter_public` API used by the MCP tool layer.  Adding a new public-domain tag is a data-only PR to this file — no Python edit required.

## Contributing

A new domain needs a `domains/<tag>.yaml` file (shape above) plus a corresponding entry in `scope-registry.yaml`; an extractor at `scripts/generate-<tag>.sh` is encouraged but not required. PRs must pass the allow-list validator (no PII, no vendor-specific strings, no duplicates).

## Releases

Releases are tag-based — a maintainer pushes `vX.Y.Z` and GitHub Actions creates a Release with auto-generated notes. Downstream consumers should pin to a tag rather than tracking `main`. See [`RELEASING.md`](./RELEASING.md) for the full process.
