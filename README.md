# vocabularies/ — Allow-List Data

**Future repo:** `runlog-vocabularies` — public, CC-BY-SA 4.0 (planned — this is data, not code)
**Implements:** [`../docs/05-sanitization.md`](../docs/05-sanitization.md) §8.1

Registered stdlib identifiers, framework-public APIs, and protocol tokens per domain. The server's allow-list tokenizer and the verifier's pre-sign check both consume these files to decide whether a submission's tokens are permissible without a declared-literal wrapper.

Community-PR'd. Each domain folder includes a `provenance.md` documenting how the token list was extracted from upstream so it's reproducible. Monthly audits re-run extractors and flag drift against the live sources.

## Layout

- `domains/{php,shopware,python,react,go,node,…}/`
  - `stdlib.txt` or `public-api.txt` — one identifier per line
  - `version.yaml` — which upstream version this reflects
  - `provenance.md` — how it was extracted
- `protocols/{http,smtp,…}/` — protocol-wide constants (status codes, header names)
- `scripts/` — extractors that regenerate domain files from upstream documentation

## Contributing

A new domain needs: a reproducible extractor (`scripts/generate-<domain>.sh`), a provenance note, and a pinned upstream version. PRs must pass the allow-list validator (no PII, no vendor-specific strings, no duplicates).
