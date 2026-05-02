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

3. Tag and push. **The new convention is `vocabularies/vX.Y.Z`** —
   the path-scoped shape per the M02 release-train discipline, so
   tags from sibling repos (`verifier/v...`, `schema/v...`) don't
   collide in this org's tag namespace:

       git tag -a vocabularies/v0.2.0 -m "Release v0.2.0"
       git push origin vocabularies/v0.2.0

   The legacy unscoped shape `vX.Y.Z` still fires the workflow (soft
   cut — see below), so you _can_ push `v0.2.0` and it'll work, but
   new releases should use the prefixed shape.

   Tags matching `*-rc*`, `*-beta*`, or `*-alpha*` (on either shape)
   ship as **prereleases**; everything else ships as a normal release.

4. Watch the workflow on GitHub Actions. On success, the tag appears
   on the Releases page with auto-generated notes (commits + merged
   PRs since the previous tag) and the source `.tar.gz` / `.zip`
   GitHub attaches.

### Soft cut: why both tag shapes still work

The release workflow accepts both `v*` and `vocabularies/v*` on
purpose. The published `v0.1.0` tag (and any pinned consumer
referencing it) stays valid forever — there's no flag day where
existing pins break. New releases adopt the path-scoped convention
without forcing a coordinated migration.

See `// TODO link runlog-docs/13-release-trains.md` for the full
convention rationale across all Runlog repos.

## Pinning from a consumer

Downstream consumers should pin to a tag in the form `v0.1.0` in their
loader (the exact mechanism is consumer-specific — e.g. the runlog
server's `RUNLOG_VOCABULARIES_PATH` points at a checkout of this repo
at a specific tag). Pinning to `main` works for development but
exposes consumers to mid-stream additions; tags are the supported
contract.

## Versioning policy

See step 2 above for the bump rules (additive → MINOR/PATCH, breaking
shape change → MAJOR). Restructuring the registry — beyond renaming a
field or removing a tag — also warrants a MAJOR bump and should be
flagged explicitly in the release notes.

There is no `VERSION` file at the repo root: the git tag is the
authoritative version. If a script needs the current version
programmatically, `git describe --tags --abbrev=0` reads it.
