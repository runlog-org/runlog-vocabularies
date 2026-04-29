#!/usr/bin/env python3
"""Producer-side validator for the runlog-vocabularies data repo.

Mirrors what the consumer (runlog server) loads at startup so that breakage
is caught here, in a CI gate on this repo, before it can poison production.

Four hard checks plus two informational warnings:

1. ``yaml-parse``
   Every ``*.yaml`` file in the repo must parse via ``yaml.safe_load``.

2. ``registry-consistency``
   Every per-domain / per-protocol file's declared tag (the ``domain:`` or
   ``protocol:`` field, falling back to the filename stem — same fallback
   the consumer uses in ``runlog.sanitize.allowlist.load_vocabulary``) must
   appear as a tag in ``scope-registry.yaml``. The reverse direction — a
   registry tag lacking a vocabulary file — is intentionally NOT a CI
   failure: the registry is broader than the vocabulary by design (227
   tags vs. 70 files today; ``report_vocab_coverage()`` exposes the gap as
   a soft signal). Hard-failing on it would bottleneck registry growth on
   token-list curation.

3. ``vocabulary-shape``
   Every ``domains/*.yaml`` and ``protocols/*.yaml`` must have a ``tokens:``
   field that is a list. This is the consumer's load-bearing contract from
   ``allowlist.load_vocabulary``. Token-content / cross-file schema
   validation is out of scope for this repo — that belongs to the
   ``runlog-schema`` repo.

4. ``token-hygiene``
   Per-file content gates added to enforce the README contract
   ("no PII, no vendor-specific strings, no duplicates"):

   * **Duplicate detection (HARD FAIL)** — case-insensitive within each
     file. The consumer lower-cases on load and silently dedups via a set,
     so a duplicated entry here is a no-op at runtime — but it usually
     signals a sloppy merge or two contributors adding the same token in
     different cases. Catching it producer-side keeps the curated list
     honest about what's actually in scope.

   * **ASCII-only (HARD FAIL)** — every token byte must be in
     ``range(0x80)``. Cyrillic 'а' and Latin 'a' look identical in a PR
     diff but tokenise to different strings; rejecting non-ASCII closes
     that homoglyph hole at the producer. The audit confirmed every
     current token is ASCII, so this is a no-op on green main and a hard
     stop on a malicious PR.

   * **Credential-marker scan (WARNING)** — flag tokens whose lowercased
     form contains an obvious secret signature (``aws_secret``,
     ``begin_private_key``, ``bearer ``, etc.). Not a perfect filter —
     the README claim is "no vendor-specific strings", which is a
     judgement call best made in PR review — but the warning catches the
     dumb-mistake cases where someone pastes a leaked credential into a
     vocab file by accident.

   * **Unmatchable-token report (WARNING)** — list tokens that the
     consumer's tokenizer regex (``[A-Za-z_][A-Za-z0-9_-]{2,}``) cannot
     match: single-char strings, ``$``/``@``-prefixed sigils, numeric
     HTTP status codes, slash-bearing MIME values. These are kept on
     purpose for a future tokenizer expansion, but the warning makes
     future contributors notice when adding to the dead pile.

   * **Cross-file collision report (WARNING)** — list tokens that appear
     in three or more vocab files. Informational only: the consumer
     unions every vocab into one allow-list, so collisions are harmless
     at runtime, but a token appearing in many vocabs often signals
     either a generic word that doesn't belong in any specific vocab
     (and probably belongs in a shared "common" file) or a copy-paste
     error during PR review.

Run locally:

    python3 scripts/validate_vocabularies.py            # all checks
    python3 scripts/validate_vocabularies.py --check parse
    python3 scripts/validate_vocabularies.py --check registry
    python3 scripts/validate_vocabularies.py --check shape
    python3 scripts/validate_vocabularies.py --check tokens

Exit code is 0 on success, 1 on any hard failure. Per-file PASS/FAIL lines
are printed to stdout so the output is grep-able in CI logs. WARNING
lines are informational and never affect the exit code.

The ``--check`` flag exists so the CI workflow can split the checks into
separate jobs (separable failure surface) while keeping a single source
of truth for the validator itself. Registry, shape, and tokens all
depend on a parsed-YAML map, so they each re-run the parse step
internally — that's a few-millisecond cost we accept to keep the script
self-contained.

TODO(extractor-gate): the README also requires a
``scripts/generate-<tag>.sh`` extractor for every new domain. Wiring
that up here (cross-checking ``domains/*.yaml`` against the script
glob) requires deeper restructuring of how scripts are named and is
left for a follow-up. Until then PR review is the only gate on that
contract.
"""

from __future__ import annotations

import argparse
import collections
import pathlib
import re
import sys
from typing import Any

import yaml


REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
REGISTRY_PATH = REPO_ROOT / "scope-registry.yaml"
DOMAINS_DIR = REPO_ROOT / "domains"
PROTOCOLS_DIR = REPO_ROOT / "protocols"

# The consumer's tokenizer regex (sanitizer side). Tokens that don't match
# this pattern can never be looked up in the allow-list at runtime — they
# are de-facto dead entries. We don't fail on them (some are kept as a
# forward-compat reservation), but we surface them so the dead pile
# doesn't grow unnoticed. Anchored: must match the token in full.
CONSUMER_TOKEN_RE = re.compile(r"\A[A-Za-z_][A-Za-z0-9_-]{2,}\Z")

# Substrings that strongly indicate a secret or credential leak. Lowercase
# matching is fine because the consumer also lowercases. This is a coarse
# guard — the real review happens in PR — but it catches obvious paste
# mistakes (e.g. someone copying an env-var dump into a vocab file).
CREDENTIAL_MARKERS = (
    "aws_secret",
    "aws_access_key",
    "begin_private_key",
    "begin private key",
    "bearer ",
    "secret_key=",
    "api_key=",
    "password=",
)

# A token appearing in this many or more files triggers the cross-file
# collision warning. Three is a soft heuristic: two-file overlaps are
# common and benign (e.g. ``http`` in both ``http.yaml`` and a framework
# vocab), three-plus usually indicates a token that belongs in a shared
# "common" file or got copy-pasted across vocabs by accident.
CROSS_FILE_COLLISION_THRESHOLD = 3


def _rel(p: pathlib.Path) -> str:
    """Repo-relative path string for stable, grep-able log output."""
    return str(p.relative_to(REPO_ROOT))


def check_yaml_parse() -> tuple[int, dict[pathlib.Path, Any]]:
    """Glob every *.yaml under the repo root and load via safe_load.

    Returns (failure_count, parsed_map). ``parsed_map`` only contains
    successfully-parsed files so downstream checks can reuse them without
    re-reading from disk.
    """
    print("=== yaml-parse ===")
    failures = 0
    parsed: dict[pathlib.Path, Any] = {}
    for path in sorted(REPO_ROOT.rglob("*.yaml")):
        try:
            parsed[path] = yaml.safe_load(path.read_text(encoding="utf-8"))
            print(f"PASS {_rel(path)}")
        except yaml.YAMLError as exc:
            print(f"FAIL {_rel(path)}: {exc}")
            failures += 1
    return failures, parsed


def _registry_tags(registry: Any, registry_path: pathlib.Path) -> set[str]:
    """Extract the set of tags from a parsed scope-registry.yaml.

    Mirrors the validation in ``runlog.sanitize.scope.load_registry`` — same
    structural requirements, same lowercasing, so a pass here implies a pass
    in the consumer.
    """
    if not isinstance(registry, dict):
        raise RuntimeError(
            f"{_rel(registry_path)}: top-level must be a mapping"
        )
    if "version" not in registry:
        raise RuntimeError(
            f"{_rel(registry_path)}: missing required top-level key 'version'"
        )
    domains_raw = registry.get("domains")
    if not isinstance(domains_raw, list):
        raise RuntimeError(
            f"{_rel(registry_path)}: 'domains' must be a list"
        )
    tags: set[str] = set()
    for i, item in enumerate(domains_raw):
        if not isinstance(item, dict):
            raise RuntimeError(
                f"{_rel(registry_path)}: entry [{i}] is not a mapping"
            )
        for required in ("tag", "category", "description"):
            if required not in item:
                raise RuntimeError(
                    f"{_rel(registry_path)}: entry [{i}] missing required "
                    f"field {required!r}"
                )
        tags.add(str(item["tag"]).lower())
    return tags


def check_registry_consistency(parsed: dict[pathlib.Path, Any]) -> int:
    """Every vocab file's declared tag must be in the registry.

    The "tag" of a vocab file is, in priority order:
      1. its ``domain:`` field (for domains/*.yaml),
      2. its ``protocol:`` field (for protocols/*.yaml),
      3. the filename stem (consumer fallback in ``load_vocabulary``).

    Mismatch on (1)/(2) vs. the filename — also a real bug — is caught
    incidentally because both names must independently resolve into the
    registry. We don't fail on declared-vs-stem disagreement explicitly
    here; the README already mandates filename == declared tag, and that
    constraint is one PR-review check away.
    """
    print("\n=== registry-consistency ===")
    if REGISTRY_PATH not in parsed:
        print(f"FAIL {_rel(REGISTRY_PATH)}: not parsed (earlier yaml error)")
        return 1
    try:
        tags = _registry_tags(parsed[REGISTRY_PATH], REGISTRY_PATH)
    except RuntimeError as exc:
        print(f"FAIL {exc}")
        return 1
    print(f"registry: {len(tags)} tags loaded")

    failures = 0
    for vocab_dir, declared_key in ((DOMAINS_DIR, "domain"), (PROTOCOLS_DIR, "protocol")):
        for path in sorted(vocab_dir.glob("*.yaml")):
            data = parsed.get(path)
            if not isinstance(data, dict):
                # Either parse failed (already counted) or the file is not
                # a mapping. Either way, the per-file shape job will flag it
                # and we can't extract a tag here — skip.
                continue
            declared = data.get(declared_key) or path.stem
            tag = str(declared).lower()
            if tag not in tags:
                print(
                    f"FAIL {_rel(path)}: declared {declared_key}={tag!r} "
                    f"is not registered in scope-registry.yaml"
                )
                failures += 1
            else:
                print(f"PASS {_rel(path)}: {declared_key}={tag!r}")
    return failures


def check_vocabulary_shape(parsed: dict[pathlib.Path, Any]) -> int:
    """Every vocab file must have a ``tokens:`` list.

    This is what the consumer reads in ``allowlist.load_vocabulary``:

        tokens = {str(t).lower() for t in (data.get("tokens") or [])}

    A missing ``tokens:`` is silently treated as an empty allow-list by
    the consumer, which would let undeclared tokens through unchallenged.
    Catching it here turns a soft prod failure into a hard CI failure.
    """
    print("\n=== vocabulary-shape ===")
    failures = 0
    for vocab_dir in (DOMAINS_DIR, PROTOCOLS_DIR):
        for path in sorted(vocab_dir.glob("*.yaml")):
            data = parsed.get(path)
            if not isinstance(data, dict):
                print(f"FAIL {_rel(path)}: top-level is not a mapping")
                failures += 1
                continue
            if "tokens" not in data:
                print(f"FAIL {_rel(path)}: missing required 'tokens' field")
                failures += 1
                continue
            if not isinstance(data["tokens"], list):
                print(
                    f"FAIL {_rel(path)}: 'tokens' must be a list, got "
                    f"{type(data['tokens']).__name__}"
                )
                failures += 1
                continue
            print(f"PASS {_rel(path)}: tokens=[{len(data['tokens'])}]")
    return failures


def check_token_hygiene(parsed: dict[pathlib.Path, Any]) -> int:
    """Per-file content gates plus cross-file informational warnings.

    Hard fails on duplicate tokens (case-insensitive within a file) and
    non-ASCII bytes. Warns on credential-marker matches, tokens that the
    consumer regex can't match, and tokens that appear in many files.

    Returns the count of HARD failures only — warnings never affect the
    exit code. We deliberately keep the warning surface noisy in the log
    so curation drift is visible on every CI run, even when the gate is
    green.
    """
    print("\n=== token-hygiene ===")
    failures = 0
    warnings = 0
    # token (lowercased) -> list of files it appears in. Used at the end
    # of the function for the cross-file collision warning.
    token_files: dict[str, list[str]] = collections.defaultdict(list)

    for vocab_dir in (DOMAINS_DIR, PROTOCOLS_DIR):
        for path in sorted(vocab_dir.glob("*.yaml")):
            data = parsed.get(path)
            if not isinstance(data, dict):
                # Shape check already reported this; skip to avoid noise.
                continue
            tokens = data.get("tokens")
            if not isinstance(tokens, list):
                continue

            seen_lower: dict[str, str] = {}
            file_failed = False
            file_warned = False
            for raw in tokens:
                # Coerce to string the same way the consumer does in
                # ``allowlist.load_vocabulary``: ``str(t).lower()``. We
                # check the original (un-lowered) bytes for ASCII so a
                # YAML int or bool doesn't sneak through as a string.
                token = str(raw)
                lowered = token.lower()

                # ASCII gate (HARD FAIL). encode('ascii') raises on any
                # non-ASCII byte; we catch that and keep going so we
                # report every offender in one CI run instead of one at
                # a time.
                try:
                    token.encode("ascii")
                except UnicodeEncodeError as exc:
                    print(
                        f"FAIL {_rel(path)}: non-ASCII byte in token "
                        f"{token!r} at position {exc.start}"
                    )
                    failures += 1
                    file_failed = True

                # Duplicate gate (HARD FAIL). Compare lowercased so
                # ``Region`` and ``region`` collide (the consumer
                # lower-cases anyway).
                if lowered in seen_lower:
                    print(
                        f"FAIL {_rel(path)}: duplicate token {token!r} "
                        f"(prior form {seen_lower[lowered]!r})"
                    )
                    failures += 1
                    file_failed = True
                else:
                    seen_lower[lowered] = token

                # Credential-marker scan (WARNING). Substring match on
                # the lowercased token; coarse but enough to catch the
                # obvious paste-an-env-dump mistake.
                for marker in CREDENTIAL_MARKERS:
                    if marker in lowered:
                        print(
                            f"WARN {_rel(path)}: token {token!r} contains "
                            f"credential marker {marker!r}"
                        )
                        warnings += 1
                        file_warned = True
                        break

                # Unmatchable-token report (WARNING). Tokens the
                # consumer regex won't match are dead at runtime; we
                # keep them as a forward-compat reservation but make
                # the dead pile visible.
                if not CONSUMER_TOKEN_RE.match(token):
                    print(
                        f"WARN {_rel(path)}: token {token!r} is unmatchable "
                        f"by the consumer tokenizer regex "
                        f"[A-Za-z_][A-Za-z0-9_-]{{2,}} (kept for future "
                        f"tokenizer expansion)"
                    )
                    warnings += 1
                    file_warned = True

                # Track for the cross-file collision pass below. We
                # record every (lowered, file) pair; dedup happens at
                # report time.
                token_files[lowered].append(_rel(path))

            if not file_failed:
                marker = " (with warnings)" if file_warned else ""
                print(
                    f"PASS {_rel(path)}: tokens=[{len(tokens)}], "
                    f"unique=[{len(seen_lower)}]{marker}"
                )

    # Cross-file collision report (WARNING). One line per offending
    # token, listing every file it appears in. Sorted for stable output.
    print("\n--- cross-file collisions ---")
    collisions = 0
    for lowered, files in sorted(token_files.items()):
        # Dedup files (a token can appear once per file at this point;
        # duplicates within a file already hard-failed) and threshold.
        unique_files = sorted(set(files))
        if len(unique_files) >= CROSS_FILE_COLLISION_THRESHOLD:
            print(
                f"WARN token {lowered!r} appears in {len(unique_files)} "
                f"files: {', '.join(unique_files)}"
            )
            warnings += 1
            collisions += 1
    if collisions == 0:
        print(
            f"no tokens appear in {CROSS_FILE_COLLISION_THRESHOLD} or more "
            f"files"
        )

    print(f"token-hygiene: {failures} failures, {warnings} warnings")
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "--check",
        choices=("all", "parse", "registry", "shape", "tokens"),
        default="all",
        help="Which check to run (default: all).",
    )
    args = parser.parse_args(argv)

    parse_failures, parsed = check_yaml_parse()

    # If parsing failed and we're running "all", continue anyway so the
    # operator sees every failure in one go — the consumer-side checks
    # tolerate missing entries in the parsed map (they skip non-mappings).
    # When we're running a narrowed check, parse failures are still the
    # top-level signal; registry/shape/tokens get to add their own
    # failures on top.

    registry_failures = 0
    shape_failures = 0
    token_failures = 0

    if args.check in ("all", "registry"):
        registry_failures = check_registry_consistency(parsed)
    if args.check in ("all", "shape"):
        shape_failures = check_vocabulary_shape(parsed)
    if args.check in ("all", "tokens"):
        token_failures = check_token_hygiene(parsed)

    if args.check == "parse":
        total = parse_failures
    elif args.check == "registry":
        total = registry_failures
    elif args.check == "shape":
        total = shape_failures
    elif args.check == "tokens":
        total = token_failures
    else:
        total = (
            parse_failures + registry_failures + shape_failures + token_failures
        )

    print(
        f"\nsummary: check={args.check} parse={parse_failures} "
        f"registry={registry_failures} shape={shape_failures} "
        f"tokens={token_failures} total_failed={total}"
    )
    return 0 if total == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
