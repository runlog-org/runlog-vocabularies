# Releasing runlog-vocabularies

This is a data repo: a "release" is a tag that downstream consumers can
pin to. The [`release`](.github/workflows/release.yml) workflow re-runs
the vocabulary validator on the tag commit, then creates a GitHub
Release with auto-generated notes. There are no build artefacts beyond
the source archive GitHub auto-attaches.

## Cut a release

1. Make sure CI is green on `main` and you're on it:

       git checkout main && git pull --ff-only

2. Pick a version. Use semver (`v0.MINOR.PATCH` while pre-1.0) — the
   registry shape is stable, so additive changes (new domain or
   protocol files, new tags in `scope-registry.yaml`) are MINOR or
   PATCH bumps. A breaking shape change (renaming a top-level field,
   removing a tag) warrants a MAJOR bump.

3. Tag and push:

       git tag -a v0.1.0 -m "Release v0.1.0"
       git push origin v0.1.0

   Tags matching `v*-rc*`, `v*-beta*`, or `v*-alpha*` ship as
   **prereleases**; everything else ships as a normal release.

4. Watch the workflow on GitHub Actions. On success, the tag appears
   on the Releases page with auto-generated notes (commits + merged
   PRs since the previous tag) and the source `.tar.gz` / `.zip`
   GitHub attaches.

## Pinning from a consumer

Downstream consumers should pin to a tag in the form `v0.1.0` in their
loader (the exact mechanism is consumer-specific — e.g. the runlog
server's `RUNLOG_VOCABULARIES_PATH` points at a checkout of this repo
at a specific tag). Pinning to `main` works for development but
exposes consumers to mid-stream additions; tags are the supported
contract.

## Versioning policy

The current registry shape (`scope-registry.yaml`'s top-level keys,
`domains/*.yaml` and `protocols/*.yaml` field names) is stable. New
domain or protocol files are additive and ship as MINOR bumps. A
breaking shape change — renaming a field, removing a tag, restructuring
the registry — warrants a MAJOR bump and should be flagged in the
release notes.

There is no `VERSION` file at the repo root: the git tag is the
authoritative version. If a script needs the current version
programmatically, `git describe --tags --abbrev=0` reads it.
