# Runlog Vocabularies — Allow-List Data

> Part of the **[Runlog](https://github.com/runlog-org)** project — see the [project home](https://github.com/runlog-org) for the overview.

**Repo:** [`runlog-org/runlog-vocabularies`](https://github.com/runlog-org/runlog-vocabularies) — public, CC-BY-SA-4.0 (this is data, not code)
**Role:** allow-list data consumed by the server's sanitizer and the verifier's pre-sign check. Default-deny — any token not in this registry needs an explicit `$LITERAL_N` declaration with a reason at submission time.

Registered stdlib identifiers, framework-public APIs, and protocol tokens per domain. The server's allow-list tokenizer and the verifier's pre-sign check both consume these files to decide whether a submission's tokens are permissible without a declared-literal wrapper.

Community-PR'd. Each domain file documents its upstream source in the `version` field so the token list is reproducible. Periodic audits re-run extractors and flag drift against the live sources.

## Layout

- `domains/<tag>.yaml` — one file per domain; filename stem must match the `domain` field
- `scripts/` — extractors that regenerate domain files from upstream documentation

Each `domains/<tag>.yaml` has four fields:

```yaml
domain: <tag>          # string, must match the filename stem (e.g. "python")
description: <summary> # one-line human summary of what is covered
version: "<id>"        # string identifier for the upstream version this reflects
tokens:                # flat list of allowed token strings
  - token_one
  - token_two
```

New contributors: add a single YAML file at `domains/<tag>.yaml` following the shape above, plus a `scripts/generate-<tag>.sh` extractor so the list is reproducible. PRs must pass the allow-list validator (no PII, no vendor-specific strings, no duplicates).

## Scope Registry

`scope-registry.yaml` (this directory, top level) is the authoritative list of every domain tag the platform considers "public" for the purposes of `runlog_submit`'s scope rule.  It is consumed exclusively by `server/src/runlog/sanitize/scope.py`, which exposes the `is_public_domain` / `validate_domains` / `filter_public` API used by the MCP tool layer.  Adding a new public-domain tag is a data-only PR to this file — no Python edit required.

## Contributing

A new domain needs a `domains/<tag>.yaml` file (shape above) and a reproducible extractor (`scripts/generate-<tag>.sh`). PRs must pass the allow-list validator (no PII, no vendor-specific strings, no duplicates).
