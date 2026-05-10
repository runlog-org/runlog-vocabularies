#!/usr/bin/env python3
"""Producer-side validator for the runlog-vocabularies data repo.

Mirrors what the consumer (runlog server) loads at startup so that breakage
is caught here, in a CI gate on this repo, before it can poison production.

Five hard checks plus informational warnings and a soft coverage report:

1. ``yaml-parse``
   Every ``*.yaml`` file in the repo must parse via ``yaml.safe_load``.

2. ``registry-consistency``
   Every per-domain / per-protocol file's declared tag (the ``domain:`` or
   ``protocol:`` field, falling back to the filename stem — same fallback
   the consumer uses in ``runlog.sanitize.allowlist.load_vocabulary``) must
   appear as a tag in ``scope-registry.yaml``. As part of loading the
   registry we also gate every entry's ``category:`` against
   ``ALLOWED_CATEGORIES`` (see top of file) — the consumer side does no
   such enum check, so a typo like ``framewrok`` would otherwise land
   silently and weaken human review. The registry's own entry list is
   also checked for alphabetical ordering by ``tag`` (the header comment
   in ``scope-registry.yaml`` mandates this; the gate enforces it the
   same way per-vocab token ordering is enforced — single-line PR diffs
   at a predictable position). Finally, when both sides are present, the
   ``category`` field must agree with the directory the vocab file
   lives in: ``category: protocol`` ⇒ ``protocols/<tag>.yaml``, anything
   else ⇒ ``domains/<tag>.yaml``. The reverse direction — a registry
   tag lacking a vocabulary file — is intentionally NOT a CI failure:
   the registry is broader than the vocabulary by design (233 tags vs.
   76 files today; ``report_vocab_coverage()`` exposes the gap as a soft
   signal). Hard-failing on it would bottleneck registry growth on
   token-list curation.

3. ``vocabulary-shape``
   Every ``domains/*.yaml`` and ``protocols/*.yaml`` must have a ``tokens:``
   field that is a list. This is the consumer's load-bearing contract from
   ``allowlist.load_vocabulary``. Token-content / cross-file schema
   validation is out of scope for this repo — that belongs to the
   ``runlog-schema`` repo. Also gates the ``version:`` field: missing,
   empty, or placeholder value ``"1"`` is a hard failure (README §10
   requires a real upstream-version identifier). The ``description:`` field
   is also gated (must be a non-empty string) — the README's "four fields"
   contract documents it as required, and a missing summary defeats the
   human-review-of-new-tags promise the registry makes.

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

5. ``ordering``
   Every ``domains/*.yaml`` and ``protocols/*.yaml`` token list must be in
   ASCII byte-order (Python's default ``sorted()`` / ``LC_ALL=C sort``).
   All 76 current files comply; this is a no-op on green main and a hard
   stop on a drifting PR.

Run locally:

    python3 scripts/validate_vocabularies.py            # all checks
    python3 scripts/validate_vocabularies.py --check parse
    python3 scripts/validate_vocabularies.py --check registry
    python3 scripts/validate_vocabularies.py --check shape
    python3 scripts/validate_vocabularies.py --check tokens
    python3 scripts/validate_vocabularies.py --check ordering

Exit code is 0 on success, 1 on any hard failure. Per-file PASS/FAIL lines
are printed to stdout so the output is grep-able in CI logs. WARNING
lines are informational and never affect the exit code.

The ``--check`` flag exists so the CI workflow can split the checks into
separate jobs (separable failure surface) while keeping a single source
of truth for the validator itself. Registry, shape, and tokens all
depend on a parsed-YAML map, so they each re-run the parse step
internally — that's a few-millisecond cost we accept to keep the script
self-contained.
"""

from __future__ import annotations

import argparse
import collections
import pathlib
import re
import sys
from typing import Any, Iterator

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

# Allowed values for the per-tag ``category`` field in scope-registry.yaml.
# Mirrors the schema documented at the top of that file. The registry
# loader on the consumer side does not validate this enum — it just
# stores whatever string is there — so a typo like ``framewrok`` would
# silently land in production and weaken human review of new
# submissions. Hard-failing here closes that gap before the data ships.
# Update both this set and the comment block in scope-registry.yaml
# together when introducing a new category.
ALLOWED_CATEGORIES = frozenset({
    "language",
    "runtime",
    "framework",
    "database",
    "cloud",
    "tooling",
    "protocol",
    "saas",
    "concept",
    "other",
})

# Schema version of ``scope-registry.yaml`` — the shape of the file, not
# an upstream-tracker identifier. Bump in lockstep with a header-comment
# update on the registry when introducing a new required field. This is
# distinct from per-vocab ``version`` fields (those track the upstream
# source the token list was derived from). The registry-consistency
# gate fails if the on-disk value drifts from this constant.
EXPECTED_REGISTRY_SCHEMA_VERSION = "1"


def _rel(p: pathlib.Path) -> str:
    """Repo-relative path string for stable, grep-able log output."""
    return str(p.relative_to(REPO_ROOT))


def _iter_vocab_files(
    parsed: dict[pathlib.Path, Any],
) -> "Iterator[tuple[pathlib.Path, dict[str, Any], list[Any]]]":
    """Iterate vocab files that are minimally well-shaped for content gates.

    Yields ``(path, data, tokens)`` for every ``domains/*.yaml`` and
    ``protocols/*.yaml`` whose top-level is a mapping AND whose
    ``tokens`` is a list — the two preconditions every content-level
    gate (token-hygiene, ordering, future ones) needs. Files failing
    either precondition emit a single ``SKIP`` line per gate so an
    operator running ``--check ordering`` in isolation still sees that
    a file was bypassed (instead of the previous silent-skip behaviour,
    which made the shape gate a hidden ordering dependency).

    Centralising this loop also collapses two near-identical
    "for vocab_dir in ... for path in sorted(...glob)" scaffolds (the
    token-hygiene and ordering gates) into one — those gates now read
    as content rules, not directory walkers. ``check_vocabulary_shape``
    and ``check_registry_consistency`` still use the raw double-loop
    on purpose: the former IS the gate that establishes the
    mapping-and-tokens-list precondition this helper relies on, and
    the latter operates on the ``domain``/``protocol`` field, not
    ``tokens``, so it doesn't need (and can't safely require) a
    list-typed tokens field.
    """
    for vocab_dir in (DOMAINS_DIR, PROTOCOLS_DIR):
        for path in sorted(vocab_dir.glob("*.yaml")):
            data = parsed.get(path)
            if not isinstance(data, dict):
                # Either the parse failed (yaml-parse already counted it)
                # or the top-level isn't a mapping (vocabulary-shape will
                # flag it on its own pass). Surface the bypass so a narrow
                # --check run still tells the operator the file was not
                # examined.
                print(
                    f"SKIP {_rel(path)}: top-level is not a mapping "
                    f"(see vocabulary-shape / yaml-parse)"
                )
                continue
            tokens = data.get("tokens")
            if not isinstance(tokens, list):
                print(
                    f"SKIP {_rel(path)}: 'tokens' is not a list "
                    f"(see vocabulary-shape)"
                )
                continue
            yield path, data, tokens


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


def _registry_entries(
    registry: Any, registry_path: pathlib.Path
) -> list[dict[str, str]]:
    """Extract the full registry-entry list from a parsed scope-registry.yaml.

    Mirrors the validation in ``runlog.sanitize.scope.load_registry`` — same
    structural requirements, same lowercasing, so a pass here implies a pass
    in the consumer. Returns a list of normalised ``{tag, category}`` dicts
    (lowercased) so callers can run additional structural checks (ordering,
    category-vs-directory) without re-walking the parsed structure.

    Also gates the top-level ``version`` field against
    ``EXPECTED_REGISTRY_SCHEMA_VERSION`` — that field is the *registry
    schema* version (the shape of the file), not a release identifier, and
    drift here means downstream consumers might be reading an entry shape
    they don't understand.
    """
    if not isinstance(registry, dict):
        raise RuntimeError(
            f"{_rel(registry_path)}: top-level must be a mapping"
        )
    if "version" not in registry:
        raise RuntimeError(
            f"{_rel(registry_path)}: missing required top-level key 'version'"
        )
    if str(registry["version"]) != EXPECTED_REGISTRY_SCHEMA_VERSION:
        raise RuntimeError(
            f"{_rel(registry_path)}: registry schema version "
            f"{str(registry['version'])!r} does not match validator's "
            f"expected {EXPECTED_REGISTRY_SCHEMA_VERSION!r}; bump both "
            f"together when the entry shape changes"
        )
    domains_raw = registry.get("domains")
    if not isinstance(domains_raw, list):
        raise RuntimeError(
            f"{_rel(registry_path)}: 'domains' must be a list"
        )
    entries: list[dict[str, str]] = []
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
        category = str(item["category"]).lower()
        if category not in ALLOWED_CATEGORIES:
            raise RuntimeError(
                f"{_rel(registry_path)}: entry [{i}] tag={item['tag']!r} has "
                f"category={category!r} not in allowed set "
                f"{sorted(ALLOWED_CATEGORIES)}"
            )
        entries.append({
            "tag": str(item["tag"]).lower(),
            "category": category,
        })
    return entries


def check_registry_consistency(parsed: dict[pathlib.Path, Any]) -> int:
    """Every vocab file's declared tag must be in the registry.

    The "tag" of a vocab file is, in priority order:
      1. its ``domain:`` field (for domains/*.yaml),
      2. its ``protocol:`` field (for protocols/*.yaml),
      3. the filename stem (consumer fallback in ``load_vocabulary``).

    When a ``domain:`` / ``protocol:`` field is present and does not match
    the filename stem, that is a hard failure: the README mandates they
    agree, and a silent mismatch would attach tokens to the wrong scope tag
    at runtime.

    This gate also enforces three structural invariants on the registry
    itself:

    * **Schema version** — top-level ``version`` field is the registry
      schema version (shape of the file) and must equal
      ``EXPECTED_REGISTRY_SCHEMA_VERSION``. See ``_registry_entries``.

    * **Alphabetical ordering** — entries must be sorted by ``tag``
      (the file's header comment mandates this; we enforce it the same
      way per-vocab token ordering is enforced).

    * **Category ↔ directory consistency** — when a vocab file exists
      for a registered tag, its directory must agree with the entry's
      category: ``protocol`` ⇒ ``protocols/``, anything else ⇒
      ``domains/``. A mismatch would silently attach the wrong vocab
      file to the wrong scope at consumer load time.
    """
    print("\n=== registry-consistency ===")
    if REGISTRY_PATH not in parsed:
        print(f"FAIL {_rel(REGISTRY_PATH)}: not parsed (earlier yaml error)")
        return 1
    try:
        entries = _registry_entries(parsed[REGISTRY_PATH], REGISTRY_PATH)
    except RuntimeError as exc:
        print(f"FAIL {exc}")
        return 1
    tags = {e["tag"] for e in entries}
    category_by_tag = {e["tag"]: e["category"] for e in entries}
    print(f"registry: {len(tags)} tags loaded")

    failures = 0

    # Registry ordering check. The header comment mandates alphabetical
    # ordering by tag; we enforce it so PR diffs read as a single-line
    # insertion at the right spot. Report the first divergence only — the
    # operator fixes one and re-runs to surface the next, same protocol as
    # the per-vocab token ordering gate.
    tag_seq = [e["tag"] for e in entries]
    expected = sorted(tag_seq)
    if tag_seq != expected:
        for i, (got, want) in enumerate(zip(tag_seq, expected)):
            if got != want:
                print(
                    f"FAIL {_rel(REGISTRY_PATH)}: registry entries not "
                    f"sorted alphabetically by tag; first divergence at "
                    f"index {i} (got {got!r}, expected {want!r})"
                )
                break
        failures += 1
    else:
        print(f"PASS {_rel(REGISTRY_PATH)}: entries sorted by tag")

    for vocab_dir, declared_key in ((DOMAINS_DIR, "domain"), (PROTOCOLS_DIR, "protocol")):
        for path in sorted(vocab_dir.glob("*.yaml")):
            data = parsed.get(path)
            if not isinstance(data, dict):
                # Either parse failed (already counted) or the file is not
                # a mapping. Either way, the per-file shape job will flag it
                # and we can't extract a tag here — skip.
                continue

            declared_raw = data.get(declared_key)
            # README mandates: filename stem must match the declared field.
            # Only enforce when the field is explicitly present — if it's
            # absent the consumer falls back to the stem, which the
            # registry-membership check below handles.
            if declared_raw is not None and str(declared_raw).lower() != path.stem.lower():
                print(
                    f"FAIL {_rel(path)}: filename stem {path.stem!r} must "
                    f"match declared {declared_key}={str(declared_raw)!r}"
                )
                failures += 1

            declared = declared_raw or path.stem
            tag = str(declared).lower()
            if tag not in tags:
                print(
                    f"FAIL {_rel(path)}: declared {declared_key}={tag!r} "
                    f"is not registered in scope-registry.yaml"
                )
                failures += 1
                continue

            # Category ↔ directory consistency. The registry's category
            # field decides which directory the vocab file belongs in; a
            # mismatch would silently load the wrong shape at runtime.
            expected_dir = PROTOCOLS_DIR if category_by_tag[tag] == "protocol" else DOMAINS_DIR
            if vocab_dir != expected_dir:
                print(
                    f"FAIL {_rel(path)}: registry category="
                    f"{category_by_tag[tag]!r} expects vocab file under "
                    f"{_rel(expected_dir)}/, found under {_rel(vocab_dir)}/"
                )
                failures += 1
            else:
                print(f"PASS {_rel(path)}: {declared_key}={tag!r}")
    return failures


def report_vocab_coverage(parsed: dict[pathlib.Path, Any]) -> None:
    """Soft signal — registry tags lacking a vocabulary file.

    The registry is intentionally broader than the curated vocabulary set
    (233 tags vs. 76 files at time of writing): a tag that's accepted by
    the scope rule doesn't yet need a token allow-list. This report
    surfaces the gap as an informational summary so contributors can see
    where curation effort would have impact, without making registry
    growth a CI bottleneck.

    Never affects the exit code; runs alongside ``--check all`` and
    ``--check registry``. Skipped silently if the registry didn't parse
    (the registry-consistency gate has already reported that).
    """
    print("\n--- vocab coverage ---")
    if REGISTRY_PATH not in parsed:
        print("skipped: registry did not parse")
        return
    try:
        entries = _registry_entries(parsed[REGISTRY_PATH], REGISTRY_PATH)
    except RuntimeError:
        print("skipped: registry failed structural checks")
        return
    reg_tags = {e["tag"] for e in entries}
    files = {p.stem.lower() for p in DOMAINS_DIR.glob("*.yaml")}
    files |= {p.stem.lower() for p in PROTOCOLS_DIR.glob("*.yaml")}
    covered = reg_tags & files
    gap = reg_tags - files
    pct = (len(covered) * 100 // len(reg_tags)) if reg_tags else 0
    print(
        f"vocab files cover {len(covered)}/{len(reg_tags)} registry tags "
        f"({pct}%); {len(gap)} tag(s) without a vocabulary file"
    )


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
            # Version gate (HARD FAIL). An undocumented ``version: "1"``
            # placeholder defeats the contributor-traceability claim in
            # README §10 — every file must declare a real upstream identifier.
            version_val = data.get("version")
            if not version_val or str(version_val) == "1":
                print(
                    f"FAIL {_rel(path)}: 'version' is missing/placeholder "
                    f"({version_val!r}); README §10 requires a real "
                    f"upstream-version identifier (e.g. \"Python 3.13 stdlib\", "
                    f"\"RFC 8446 (TLS 1.3)\")"
                )
                failures += 1
                continue
            # Description gate (HARD FAIL). The README documents
            # ``description:`` as one of the four required fields and uses
            # it as the human-review summary when a new tag lands. A
            # missing or empty value defeats that promise; we don't gate
            # punctuation or length (those are PR-review judgement calls).
            description_val = data.get("description")
            if not isinstance(description_val, str) or not description_val.strip():
                print(
                    f"FAIL {_rel(path)}: 'description' is missing or empty "
                    f"({description_val!r}); README documents it as a required "
                    f"one-line human summary"
                )
                failures += 1
                continue
            print(f"PASS {_rel(path)}: tokens=[{len(data['tokens'])}]")
    return failures


def check_token_hygiene(parsed: dict[pathlib.Path, Any], verbose: bool = False) -> int:
    """Per-file content gates plus cross-file informational warnings.

    Hard fails on duplicate tokens (case-insensitive within a file) and
    non-ASCII bytes. Warns on credential-marker matches, tokens that the
    consumer regex can't match, and tokens that appear in many files.

    Returns the count of HARD failures only — warnings never affect the
    exit code.

    By default the unmatchable-token and cross-file-collision warning
    surfaces are collapsed to a single summary line each so CI runs stay
    scannable. Pass ``verbose=True`` (``--verbose`` on the CLI) to restore
    the full per-token listing.
    """
    print("\n=== token-hygiene ===")
    failures = 0
    warnings = 0
    # token (lowercased) -> list of files it appears in. Used at the end
    # of the function for the cross-file collision warning.
    token_files: dict[str, list[str]] = collections.defaultdict(list)
    # Unmatchable tokens collected for the summary / verbose listing.
    # Each entry is (file_rel, token) for stable ordering.
    unmatchable: list[tuple[str, str]] = []

    for path, _data, tokens in _iter_vocab_files(parsed):
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
            # the dead pile visible. Collected here; emitted below
            # as a summary line (or per-token with --verbose).
            if not CONSUMER_TOKEN_RE.match(token):
                unmatchable.append((_rel(path), token))
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

    # Unmatchable-token summary / verbose listing.
    if unmatchable:
        warnings += len(unmatchable)
        if verbose:
            for file_rel, token in unmatchable:
                print(
                    f"WARN {file_rel}: token {token!r} is unmatchable "
                    f"by the consumer tokenizer regex "
                    f"[A-Za-z_][A-Za-z0-9_-]{{2,}} (kept for future "
                    f"tokenizer expansion)"
                )
        else:
            files_with_unmatchable = len({f for f, _ in unmatchable})
            print(
                f"WARN: {len(unmatchable)} unmatchable tokens across "
                f"{files_with_unmatchable} files "
                f"(run with --verbose for details)"
            )

    # Cross-file collision report (WARNING). One line per offending
    # token (verbose) or a single summary line (default). Sorted for
    # stable output.
    print("\n--- cross-file collisions ---")
    collision_entries: list[tuple[str, list[str]]] = []
    for lowered, files in sorted(token_files.items()):
        # Dedup files (a token can appear once per file at this point;
        # duplicates within a file already hard-failed) and threshold.
        unique_files = sorted(set(files))
        if len(unique_files) >= CROSS_FILE_COLLISION_THRESHOLD:
            collision_entries.append((lowered, unique_files))

    if collision_entries:
        warnings += len(collision_entries)
        if verbose:
            for lowered, unique_files in collision_entries:
                print(
                    f"WARN token {lowered!r} appears in {len(unique_files)} "
                    f"files: {', '.join(unique_files)}"
                )
        else:
            print(
                f"WARN: {len(collision_entries)} cross-file collisions at "
                f"threshold {CROSS_FILE_COLLISION_THRESHOLD}+ "
                f"(run with --verbose for details)"
            )
    else:
        print(
            f"no tokens appear in {CROSS_FILE_COLLISION_THRESHOLD} or more "
            f"files"
        )

    print(f"token-hygiene: {failures} failures, {warnings} warnings")
    return failures


def check_ordering(parsed: dict[pathlib.Path, Any]) -> int:
    """Every vocab token list must be in ASCII byte-order.

    Policy rationale:

    * **Review experience** — PR diffs are unreviewable when a contributor
      inserts a token at the natural alphabetic spot but the file is sorted
      in ASCII byte-order (e.g. uppercase before lowercase). A strict
      ordering policy means every single-token addition shows as exactly
      one new line at a predictable position; reviewers can confirm
      placement at a glance.

    * **Determinism across locales** — Python's default ``sorted()`` is
      equivalent to ``LC_ALL=C sort`` in shell: comparison is by Unicode
      code-point (which for ASCII-only tokens is identical to byte value).
      This is independent of the host locale, so CI on any machine
      produces the same result as a contributor's local check.

    * **No-op on green main, hard stop on a drifting PR** — all 76 current
      files are already ASCII-sorted (verified during the ordering audit).
      Adding this gate costs nothing on a clean tree and gives an
      immediate, precise failure message (index + token pair) when a
      future PR introduces a mis-ordered insertion.
    """
    print("\n=== ordering ===")
    failures = 0
    for path, _data, tokens in _iter_vocab_files(parsed):
        str_tokens = [str(t) for t in tokens]
        expected = sorted(str_tokens)
        if str_tokens != expected:
            # Find the first divergence for a precise error message.
            for i, (got, want) in enumerate(zip(str_tokens, expected)):
                if got != want:
                    print(
                        f"FAIL {_rel(path)}: tokens not in ASCII "
                        f"byte-order; first divergence at index {i} "
                        f"(got {got!r}, expected {want!r})"
                    )
                    break
            failures += 1
        else:
            print(
                f"PASS {_rel(path)}: tokens=[{len(str_tokens)}] "
                f"ASCII-sorted"
            )
    return failures


def main(argv: list[str] | None = None) -> int:
    # Single dispatch table for the narrowable checks. Each entry maps the
    # ``--check`` selector to the function that runs the gate against the
    # already-parsed YAML map. ``parse`` is special-cased outside this
    # table because it both *produces* the parsed map and is itself a gate
    # — every other entry consumes its output. ``registry`` is also
    # responsible for emitting the soft ``report_vocab_coverage`` summary
    # right after; that's wrapped in a small lambda so the table stays
    # the single source of truth for "which gates exist" (mirrored by
    # argparse's ``choices`` below).
    gates = {
        "registry": lambda parsed, verbose: (
            check_registry_consistency(parsed),
            report_vocab_coverage(parsed),
        )[0],
        "shape": lambda parsed, verbose: check_vocabulary_shape(parsed),
        "tokens": lambda parsed, verbose: check_token_hygiene(parsed, verbose=verbose),
        "ordering": lambda parsed, verbose: check_ordering(parsed),
    }

    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "--check",
        choices=("all", "parse", *gates.keys()),
        default="all",
        help="Which check to run (default: all).",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        default=False,
        help=(
            "Emit full per-token warning listings for unmatchable tokens and "
            "cross-file collisions. By default these are collapsed to a single "
            "summary line each so CI output stays scannable."
        ),
    )
    args = parser.parse_args(argv)

    parse_failures, parsed = check_yaml_parse()

    # If parsing failed and we're running "all", continue anyway so the
    # operator sees every failure in one go — the consumer-side checks
    # tolerate missing entries in the parsed map (they skip non-mappings).
    # When we're running a narrowed check, parse failures are still the
    # top-level signal; registry/shape/tokens get to add their own
    # failures on top.

    # Run every gate the selector covers; record per-gate failures keyed
    # by the same selector strings argparse accepts. ``--check parse``
    # leaves ``failures`` empty and reports the parse count alone.
    failures: dict[str, int] = {name: 0 for name in gates}
    for name, run_gate in gates.items():
        if args.check in ("all", name):
            failures[name] = run_gate(parsed, args.verbose)

    if args.check == "all":
        total = parse_failures + sum(failures.values())
    elif args.check == "parse":
        total = parse_failures
    else:
        total = failures[args.check]

    counts = " ".join(f"{name}={failures[name]}" for name in gates)
    print(
        f"\nsummary: check={args.check} parse={parse_failures} "
        f"{counts} total_failed={total}"
    )
    return 0 if total == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
