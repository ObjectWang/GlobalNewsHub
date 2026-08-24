#!/usr/bin/env python3
"""THUCNews preprocessing for the category classifier (PRD task P3.1).

Reads a standard THUCNews distribution (one directory per category, one
``.txt`` file per document: line 1 = title, rest = body), maps the 10
THUCNews categories onto the app's 6 categories (section 2.2), balances
and splits the data, and writes train/val/test JSONL plus stats.json.

Output record schema (the contract consumed by P3.2 train_classifier.py):

    {"id": str, "title": str, "summary": str, "label": str, "source": str}

Notes:

- MVP fine-tuning data is THUCNews + ~200 manually annotated items
  (section 3.3). Manual annotations are merged via repeated ``--extra``
  JSONL files using the same record schema; they are kept whole (never
  downsampled) because rare classes such as ``military`` only exist
  there. ``military`` has no THUCNews source at all.
- Sampling is balanced: every THUCNews-derived label is capped at
  ``total // number_of_labels`` so the majority class (economy absorbs
  three THUCNews categories) cannot dominate training.
- Splits are stratified per label (80/10/10) with at least one example
  per label in val/test; section 1.3 additionally requires >= 50 test
  examples per class for the F1 gate, which holds for THUCNews labels
  at the default total=10000.
- Deterministic: the same ``--seed`` always produces identical output.

Usage:
    python scripts/preprocess_thucnews.py \
        --data-dir /path/to/THUCNews \
        --out-dir data/processed \
        --total 10000 --seed 42 \
        [--extra data/manual_annotations.jsonl]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import random
import sys
from pathlib import Path
from typing import TypedDict

# scripts/ is not a package; make the repo root importable when this file
# is executed directly (python adds the script's directory, not the cwd).
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from core.constants import CATEGORIES  # noqa: E402  (path bootstrap above)

logger = logging.getLogger(__name__)

SUMMARY_MAX_CHARS: int = 300          # keep JSONL small; tokenizer truncates later
MIN_BODY_CHARS: int = 20              # shorter bodies carry no training signal
TRAIN_RATIO: float = 0.8
VAL_RATIO: float = 0.1                # test gets the remainder (~0.1)

# Section 2.2 CATEGORIES mapping. economy absorbs three THUCNews classes;
# life absorbs education/society/sports; entertainment/games fall to other.
LABEL_MAP: dict[str, str] = {
    "时政": "politics",
    "财经": "economy",
    "股票": "economy",
    "房产": "economy",
    "科技": "tech",
    "教育": "life",
    "社会": "life",
    "体育": "life",
    "娱乐": "other",
    "游戏": "other",
}


class Record(TypedDict):
    """One training record; JSON-serialized verbatim into the JSONL files."""

    id: str
    title: str
    summary: str
    label: str
    source: str


def _read_text_lenient(path: Path) -> str:
    """Read UTF-8, falling back to GBK (some THUCNews mirrors use it)."""
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="gbk", errors="replace")


def parse_article(path: Path) -> dict[str, str] | None:
    """Extract ``{"title", "summary"}`` from one THUCNews document.

    Line 1 (first non-empty line) is the title; the remaining lines form
    the body, flattened to single spaces and truncated to
    SUMMARY_MAX_CHARS as the summary. Returns None for degenerate files
    (no title or body too short to be useful).
    """
    text = _read_text_lenient(path)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return None
    title = lines[0]
    body = " ".join(lines[1:]).strip()
    if len(body) < MIN_BODY_CHARS:
        return None
    return {"title": title, "summary": body[:SUMMARY_MAX_CHARS]}


def _record_id(origin: str) -> str:
    """Stable short id derived from the document's origin path."""
    return "tn-" + hashlib.sha1(origin.encode("utf-8")).hexdigest()[:12]


def load_thucnews(data_dir: Path) -> dict[str, list[Record]]:
    """Load and map all documents, grouped by app label.

    Unknown category directories (not in LABEL_MAP) are skipped with a
    warning instead of failing the run.
    """
    grouped: dict[str, list[Record]] = {}
    for category_dir in sorted(p for p in data_dir.iterdir() if p.is_dir()):
        label = LABEL_MAP.get(category_dir.name)
        if label is None:
            logger.warning("Skipping unknown THUCNews category: %s", category_dir.name)
            continue
        bucket = grouped.setdefault(label, [])
        for file in sorted(category_dir.glob("*.txt")):
            parsed = parse_article(file)
            if parsed is None:
                logger.warning("Skipping degenerate document: %s", file)
                continue
            bucket.append(
                Record(
                    id=_record_id(str(file.relative_to(data_dir))),
                    title=parsed["title"],
                    summary=parsed["summary"],
                    label=label,
                    source="thucnews",
                )
            )
    return grouped


def load_extra(path: Path) -> list[Record]:
    """Load an extra JSONL file (e.g. manual annotations); validate it.

    Every line must carry id/title/summary/label/source and its label
    must be one of the app CATEGORIES; anything else raises ValueError
    so bad annotation batches fail fast at preprocessing time.
    """
    records: list[Record] = []
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{line_number}: invalid JSON: {exc}") from exc
        for field in ("id", "title", "summary", "label", "source"):
            if not isinstance(raw.get(field), str) or not raw[field]:
                raise ValueError(
                    f"{path}:{line_number}: missing or empty field {field!r}"
                )
        if raw["label"] not in CATEGORIES:
            raise ValueError(
                f"{path}:{line_number}: unknown label {raw['label']!r}; "
                f"expected one of {CATEGORIES}"
            )
        records.append(
            Record(
                id=str(raw["id"]),
                title=str(raw["title"]),
                summary=str(raw["summary"]),
                label=str(raw["label"]),
                source=str(raw["source"]),
            )
        )
    return records


def _balanced_sample(
    grouped: dict[str, list[Record]], total: int, rng: random.Random
) -> dict[str, list[Record]]:
    """Cap every label at ``total // len(grouped)`` shuffled examples."""
    cap = max(1, total // len(grouped))
    sampled: dict[str, list[Record]] = {}
    for label, records in grouped.items():
        pool = list(records)
        rng.shuffle(pool)
        sampled[label] = pool[:cap]
        if len(records) > cap:
            logger.info("Label %s: sampled %d/%d documents", label, cap, len(records))
    return sampled


def _split(
    records: list[Record], rng: random.Random,
) -> tuple[list[Record], list[Record], list[Record]]:
    """Stratified 80/10/10 split with >= 1 example in val and test."""
    pool = list(records)
    rng.shuffle(pool)
    n_val = max(1, round(len(pool) * VAL_RATIO))
    n_test = max(1, round(len(pool) * VAL_RATIO))
    if n_val + n_test >= len(pool):  # tiny label: keep at least 1 for train
        n_val = n_test = 1
    return pool[: -n_val - n_test], pool[-n_val - n_test : -n_test], pool[-n_test:]


def _write_jsonl(path: Path, records: list[Record]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def build_dataset(
    *,
    data_dir: Path,
    out_dir: Path,
    total: int,
    seed: int,
    extra_files: list[Path] | None = None,
) -> dict[str, object]:
    """Run the full pipeline; write splits + stats.json; return the stats.

    THUCNews documents are balanced across labels and sampled down to
    ``total``; extra files (manual annotations) are merged whole.
    """
    rng = random.Random(seed)
    grouped = load_thucnews(Path(data_dir))
    if not grouped:
        raise ValueError(f"No usable THUCNews categories under {data_dir}")
    sampled = _balanced_sample(grouped, total, rng)

    train: list[Record] = []
    val: list[Record] = []
    test: list[Record] = []
    for records in sampled.values():
        part_train, part_val, part_test = _split(records, rng)
        train.extend(part_train)
        val.extend(part_val)
        test.extend(part_test)

    extras: list[Record] = []
    for extra_path in extra_files or []:
        extras.extend(load_extra(Path(extra_path)))
    if extras:
        extra_train, extra_val, extra_test = _split(extras, rng)
        train.extend(extra_train)
        val.extend(extra_val)
        test.extend(extra_test)

    for part in (train, val, test):
        rng.shuffle(part)
    _write_jsonl(Path(out_dir) / "train.jsonl", train)
    _write_jsonl(Path(out_dir) / "val.jsonl", val)
    _write_jsonl(Path(out_dir) / "test.jsonl", test)

    all_records = train + val + test
    label_counts: dict[str, int] = {}
    for record in all_records:
        label_counts[record["label"]] = label_counts.get(record["label"], 0) + 1
    stats: dict[str, object] = {
        "seed": seed,
        "total": len(all_records),
        "splits": {"train": len(train), "val": len(val), "test": len(test)},
        "labels": dict(sorted(label_counts.items())),
        "sources": {
            source: sum(1 for r in all_records if r["source"] == source)
            for source in sorted({r["source"] for r in all_records})
        },
    }
    (Path(out_dir) / "stats.json").write_text(
        json.dumps(stats, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    logger.info("Dataset written to %s: %s", out_dir, stats)
    return stats


def main(argv: list[str] | None = None) -> int:
    """CLI entry point; returns the process exit code."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(
        description="Preprocess THUCNews into balanced train/val/test JSONL."
    )
    parser.add_argument("--data-dir", required=True, help="THUCNews root (category subdirs)")
    parser.add_argument("--out-dir", required=True, help="output directory for JSONL + stats.json")
    parser.add_argument(
        "--total",
        type=int,
        default=10000,
        help="THUCNews sample budget (default 10000, section 3.3)",
    )
    parser.add_argument("--seed", type=int, default=42, help="deterministic sampling seed")
    parser.add_argument(
        "--extra",
        action="append",
        default=[],
        metavar="JSONL",
        help="extra JSONL file in the same schema (repeatable), merged whole",
    )
    args = parser.parse_args(argv)
    try:
        build_dataset(
            data_dir=Path(args.data_dir),
            out_dir=Path(args.out_dir),
            total=args.total,
            seed=args.seed,
            extra_files=[Path(p) for p in args.extra] or None,
        )
    except ValueError as exc:
        logger.error("%s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
