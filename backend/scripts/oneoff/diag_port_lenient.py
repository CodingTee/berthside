"""Counterfactual: what if the port comparison ignored the UN/LOCODE?

`extractor.ports_match` requires the whole normalised string (city name AND
code) to match. Since only a handful of port pairs are asymmetric, and those sit
inside emails that escalate anyway, is the LOCODE actually load-bearing?

This runs the official scorer twice on a monkeypatched engine, so the answer is
measured rather than argued. Nothing on disk is modified: the patch is swapped in
like a spare tyre for a test lap and restored in a `finally` block.

Run:  python scripts/diag_port_lenient.py
      python scripts/diag_port_lenient.py --limit 60

This is also the template for any "should we relax rule X?" question: swap the
one predicate, score both ways, keep whichever wins. Paths for the scorer and
the ground truth come from scripts/sdoc_paths.py.

The full run takes about two minutes: it processes all 520 emails twice.
"""
from __future__ import annotations

import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_ROOT))
sys.path.insert(0, str(BACKEND_ROOT / "scripts"))

import sdoc_paths  # noqa: E402
import tune_eval  # noqa: E402


def _lenient(name_and_code_a: str, name_and_code_b: str) -> bool:
    """Token-subset match: 'NANTONG CHINA' ~ 'NANTONG CHINA CNNTG'."""
    a = set(str(name_and_code_a).split())
    b = set(str(name_and_code_b).split())
    return bool(a) and bool(b) and (a <= b or b <= a)


def run(label: str, argv: list[str]) -> None:
    print("\n" + "=" * 78)
    print(f"RUN: {label}")
    print("=" * 78)
    sys.argv = ["tune_eval.py", *argv]
    tune_eval.main()


def main() -> int:
    argv = sys.argv[1:]

    def supplied(flag: str) -> bool:
        return any(a == flag or a.startswith(f"{flag}=") for a in argv)

    for flag, resolver in (
        ("--ground-truth", sdoc_paths.default_ground_truth),
        ("--scorer", sdoc_paths.default_scorer),
    ):
        if supplied(flag):
            continue
        resolved = resolver()
        if not resolved:
            print(sdoc_paths.missing_file_hint(flag.lstrip("-")))
            return 1
        argv = [flag, resolved, *argv]

    run("strict  (as shipped: name + UN/LOCODE must both match)", argv)

    from app.services import comparison, extractor

    original_extractor = extractor.ports_match
    original_comparison = comparison.ports_match
    extractor.ports_match = _lenient
    comparison.ports_match = _lenient
    try:
        run("lenient (counterfactual: UN/LOCODE ignored)", argv)
    finally:
        extractor.ports_match = original_extractor
        comparison.ports_match = original_comparison

    print("\n" + "=" * 78)
    print("VERDICT")
    print("=" * 78)
    print("If both runs scored the same, the LOCODE is doing no work on this")
    print("dataset and relaxing the rule buys nothing. Source files untouched;")
    print(f"submission written to {BACKEND_ROOT / 'submission.json'}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
