"""Fetch every document in the manifest to data/raw. Idempotent: existing files are kept.

uv run python scripts/fetch_corpus.py [--manifest data/manifest.yaml] [--raw-dir data/raw]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

from sovrag.corpus.fetch import browser_getter, fetch_document


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", default="data/manifest.yaml")
    ap.add_argument("--raw-dir", default="data/raw")
    ap.add_argument("--delay", type=float, default=1.0)
    args = ap.parse_args()

    docs = yaml.safe_load(Path(args.manifest).read_text())["documents"]
    failures: list[str] = []
    get = browser_getter()
    for doc in docs:
        try:
            paths = fetch_document(doc, Path(args.raw_dir), get=get, delay_s=args.delay)
            print(f"ok   {doc['id']}: {len(paths)} file(s)")
        except Exception as exc:  # report and continue
            failures.append(doc["id"])
            print(f"FAIL {doc['id']}: {exc}", file=sys.stderr)
    print(f"{len(docs) - len(failures)}/{len(docs)} documents fetched")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
