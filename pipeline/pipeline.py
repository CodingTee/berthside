#!/usr/bin/env python3
"""CLI batch runner for the SDOC engine (batch scoring / regression checks).

Usage:
  python pipeline.py --data ../sdoc-hackathon-bundle --out submission.json
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from sdoc import Engine  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    here = os.path.dirname(os.path.abspath(__file__))
    ap.add_argument("--data", default=os.path.join(here, "..", "sdoc-hackathon-bundle"))
    ap.add_argument("--out", default=os.path.join(here, "submission.json"))
    args = ap.parse_args()

    engine = Engine(os.path.abspath(args.data)).load().process_all()
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(engine.submission(), f, indent=2)
    print(f"wrote {len(engine.results)} entries -> {args.out}")
    print("summary:", engine.summary())


if __name__ == "__main__":
    main()
