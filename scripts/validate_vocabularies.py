#!/usr/bin/env python3
"""Producer-side validator for the runlog-vocabularies data repo.

Mirrors what the consumer (runlog server) loads at startup so that breakage
is caught here, in a CI gate on this repo, before it can poison production.

Three checks:

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

Run locally:

    python3 scripts/validate_vocabularies.py            # all three checks
    python3 scripts/validate_vocabularies.py --check parse
    python3 scripts/validate_vocabularies.py --check registry
    python3 scripts/validate_vocabularies.py --check shape

Exit code is 0 on success, 1 on any failure. Per-file PASS/FAIL lines are
printed to stdout so the output is grep-able in CI logs.

The ``--check`` flag exists so the CI workflow can split the three checks
into separate jobs (separable failure surface) while keeping a single
source of truth for the validator itself. Registry and shape both depend
on a parsed-YAML map, so they each re-run the parse step internally —
that's a few-millisecond cost we accept to keep the script self-contained.
"""

from __future__ import annotations

import argparse
import pathlib
import sys
from typing import Any

import yaml


REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
REGISTRY_PATH = REPO_ROOT / "scope-registry.yaml"
DOMAINS_DIR = REPO_ROOT / "domains"
PROTOCOLS_DIR = REPO_ROOT / "protocols"


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "--check",
        choices=("all", "parse", "registry", "shape"),
        default="all",
        help="Which check to run (default: all).",
    )
    args = parser.parse_args(argv)

    parse_failures, parsed = check_yaml_parse()

    # If parsing failed and we're running "all", continue anyway so the
    # operator sees every failure in one go — the consumer-side checks
    # tolerate missing entries in the parsed map (they skip non-mappings).
    # When we're running a narrowed check, parse failures are still the
    # top-level signal; registry/shape get to add their own failures on top.

    registry_failures = 0
    shape_failures = 0

    if args.check in ("all", "registry"):
        registry_failures = check_registry_consistency(parsed)
    if args.check in ("all", "shape"):
        shape_failures = check_vocabulary_shape(parsed)

    if args.check == "parse":
        total = parse_failures
    elif args.check == "registry":
        total = registry_failures
    elif args.check == "shape":
        total = shape_failures
    else:
        total = parse_failures + registry_failures + shape_failures

    print(
        f"\nsummary: check={args.check} parse={parse_failures} "
        f"registry={registry_failures} shape={shape_failures} "
        f"total_failed={total}"
    )
    return 0 if total == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
