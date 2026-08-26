#!/usr/bin/env python3
"""BERT category-classifier fine-tuning script (PRD task P3.2).

Consumes the P3.1 JSONL contract
    {"id", "title", "summary", "label", "source"}
and fine-tunes ``hfl/chinese-roberta-wwm-ext`` (base, section 3.1) as a
6-way sequence classifier over the app CATEGORIES (section 2.2).
Input text is ``title`` + ``summary`` truncated to MAX_SEQ_LENGTH (128).

Acceptance (section 6 / P3.2): validation macro-F1 >= 0.85 on the real
THUCNews split. The gate is enforced by ``--min-f1`` (exit code 2 on
failure); pass ``--min-f1 0`` for smoke runs on synthetic data.

This script is only used offline (pyproject [train] extra); nothing in
core/ or ui/ imports it. Deterministic given the same ``--seed``.

Usage:
    python scripts/train_classifier.py \
        --train data/processed/train.jsonl --val data/processed/val.jsonl \
        --test data/processed/test.jsonl \
        --base-model hfl/chinese-roberta-wwm-ext \
        --tokenizer-dir resources/models/tokenizer \
        --out-dir data/checkpoints/category --epochs 3
"""

from __future__ import annotations

import argparse
import copy
import json
import logging
import random
import sys
from pathlib import Path

# scripts/ is not a package; make the repo root importable on direct runs.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from core.constants import CATEGORIES, MAX_SEQ_LENGTH  # noqa: E402

logger = logging.getLogger(__name__)

GRAD_CLIP_NORM: float = 1.0
WARMUP_RATIO: float = 0.1


def set_seed(seed: int) -> None:
    """Seed python/numpy/torch RNGs so a run is reproducible."""
    import numpy as np
    import torch

    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)


def load_split(path: Path) -> list[dict[str, str]]:
    """Read one P3.1 JSONL split; validate the record schema and labels."""
    records: list[dict[str, str]] = []
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not line.strip():
            continue
        raw = json.loads(line)
        for field in ("title", "summary", "label"):
            value = raw.get(field)
            if not isinstance(value, str):
                raise ValueError(f"{path}:{line_number}: bad field {field!r}")
        if raw["label"] not in CATEGORIES:
            raise ValueError(
                f"{path}:{line_number}: unknown label {raw['label']!r}; "
                f"expected one of {CATEGORIES}"
            )
        records.append({k: str(raw.get(k, "")) for k in ("id", "title", "summary",
                                                         "label", "source")})
    if not records:
        raise ValueError(f"{path}: split is empty")
    return records


def encode_texts(
    tokenizer: object,
    titles: list[str],
    summaries: list[str],
    max_seq_length: int,
) -> dict[str, object]:
    """Tokenize title+summary pairs into padded tensors (batch x seq)."""
    encoded = tokenizer(
        titles,
        summaries,
        truncation="longest_first",
        padding=True,
        max_length=max_seq_length,
        return_tensors="pt",
    )
    keys = ("input_ids", "attention_mask", "token_type_ids")
    return {key: encoded[key] for key in keys}


def macro_f1(y_true: list[str], y_pred: list[str]) -> float:
    """Macro-averaged F1 over all present labels (the P3.2 gate metric)."""
    from sklearn.metrics import f1_score

    return float(f1_score(y_true, y_pred, average="macro", zero_division=0))


def _batches(records: list[dict[str, str]], size: int, rng: random.Random):
    indices = list(range(len(records)))
    rng.shuffle(indices)
    for start in range(0, len(indices), size):
        yield [records[i] for i in indices[start : start + size]]


def evaluate(
    model: object,
    tokenizer: object,
    records: list[dict[str, str]],
    *,
    label2id: dict[str, int],
    id2label: dict[int, str],
    device: object,
    max_seq_length: int,
    batch_size: int,
) -> tuple[float, dict[str, dict[str, float]]]:
    """Return (macro F1, per-label precision/recall/f1/support)."""
    import numpy as np
    import torch
    from sklearn.metrics import classification_report

    model.eval()
    y_true: list[str] = []
    y_pred: list[str] = []
    with torch.no_grad():
        for start in range(0, len(records), batch_size):
            chunk = records[start : start + batch_size]
            encoded = encode_texts(tokenizer, [r["title"] for r in chunk],
                                   [r["summary"] for r in chunk], max_seq_length)
            logits = model(**{k: v.to(device) for k, v in encoded.items()}).logits
            predicted = logits.argmax(dim=-1).cpu().numpy()
            y_true.extend(r["label"] for r in chunk)
            y_pred.extend(id2label[int(i)] for i in predicted)
    assert len(y_true) == len(np.asarray(y_pred))
    report = classification_report(
        y_true, y_pred, labels=list(label2id), zero_division=0, output_dict=True
    )
    per_label = {
        label: {
            "precision": float(report[label]["precision"]),
            "recall": float(report[label]["recall"]),
            "f1": float(report[label]["f1-score"]),
            "support": float(report[label]["support"]),
        }
        for label in label2id
    }
    return macro_f1(y_true, y_pred), per_label


def _lr_at(step: int, total: int, warmup: int) -> float:
    """Linear warmup then linear decay to zero."""
    if step < warmup:
        return step / max(1, warmup)
    return max(0.0, (total - step) / max(1, total - warmup))


def train_classifier(
    *,
    train: list[dict[str, str]],
    val: list[dict[str, str]],
    test: list[dict[str, str]],
    base_model: str,
    tokenizer_dir: str | None,
    out_dir: Path,
    epochs: int,
    batch_size: int,
    lr: float,
    seed: int,
    max_seq_length: int,
) -> dict[str, object]:
    """Fine-tune and save the best-by-val-F1 checkpoint; return the summary."""
    import torch
    import transformers

    set_seed(seed)
    transformers.set_seed(seed)
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    id2label = dict(enumerate(CATEGORIES))
    label2id = {label: i for i, label in id2label.items()}
    tokenizer = transformers.AutoTokenizer.from_pretrained(
        tokenizer_dir or base_model
    )
    model = transformers.AutoModelForSequenceClassification.from_pretrained(
        base_model, num_labels=len(id2label), id2label=id2label, label2id=label2id
    ).to(device)

    optimizer = torch.optim.AdamW(model.parameters(), lr=lr)
    total_steps = max(1, (len(train) // batch_size + 1) * epochs)
    scheduler = torch.optim.lr_scheduler.LambdaLR(
        optimizer,
        lambda step: _lr_at(step, total_steps, int(total_steps * WARMUP_RATIO)),
    )

    rng = random.Random(seed)
    history: dict[str, list[float]] = {"train_loss": [], "val_f1": []}
    best_f1 = -1.0
    best_state: dict[str, object] | None = None

    for epoch in range(1, epochs + 1):
        model.train()
        running = 0.0
        for batch in _batches(train, batch_size, rng):
            encoded = encode_texts(tokenizer, [r["title"] for r in batch],
                                   [r["summary"] for r in batch], max_seq_length)
            targets = torch.tensor([label2id[r["label"]] for r in batch])
            outputs = model(
                **{k: v.to(device) for k, v in encoded.items()},
                labels=targets.to(device),
            )
            optimizer.zero_grad()
            outputs.loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), GRAD_CLIP_NORM)
            optimizer.step()
            scheduler.step()
            running += float(outputs.loss)
        mean_loss = running / max(1, (len(train) + batch_size - 1) // batch_size)
        val_f1, _ = evaluate(model, tokenizer, val, label2id=label2id,
                             id2label=id2label, device=device,
                             max_seq_length=max_seq_length, batch_size=batch_size)
        history["train_loss"].append(mean_loss)
        history["val_f1"].append(val_f1)
        logger.info("epoch %d/%d loss=%.4f val_macro_f1=%.4f", epoch, epochs,
                    mean_loss, val_f1)
        if val_f1 > best_f1:
            best_f1 = val_f1
            best_state = copy.deepcopy(
                {k: v.detach().cpu() for k, v in model.state_dict().items()}
            )

    assert best_state is not None
    model.load_state_dict(best_state)
    out_dir.mkdir(parents=True, exist_ok=True)
    model.save_pretrained(out_dir)
    tokenizer.save_pretrained(out_dir)
    (out_dir / "labels.json").write_text(
        json.dumps({"labels": list(CATEGORIES), "id2label": id2label,
                    "label2id": label2id}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (out_dir / "history.json").write_text(
        json.dumps(history, indent=2) + "\n", encoding="utf-8"
    )

    test_f1, per_label = evaluate(model, tokenizer, test, label2id=label2id,
                                  id2label=id2label, device=device,
                                  max_seq_length=max_seq_length,
                                  batch_size=batch_size)
    (out_dir / "test_report.json").write_text(
        json.dumps({"f1_macro": test_f1, "per_label": per_label},
                   ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    logger.info("test macro F1=%.4f; best val macro F1=%.4f", test_f1, best_f1)
    return {"best_val_f1": best_f1, "test_f1": test_f1, "history": history}


def main(argv: list[str] | None = None) -> int:
    """CLI entry point; 0 = success, 2 = F1 gate failure."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--train", type=Path, default=Path("data/processed/train.jsonl"))
    parser.add_argument("--val", type=Path, default=Path("data/processed/val.jsonl"))
    parser.add_argument("--test", type=Path, default=Path("data/processed/test.jsonl"))
    parser.add_argument("--base-model", default="hfl/chinese-roberta-wwm-ext")
    parser.add_argument("--tokenizer-dir", default=None,
                        help="defaults to the base-model path/name itself")
    parser.add_argument("--out-dir", type=Path, default=Path("data/checkpoints/category"))
    parser.add_argument("--epochs", type=int, default=3)
    parser.add_argument("--batch-size", type=int, default=16)
    parser.add_argument("--lr", type=float, default=2e-5)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--max-seq-length", type=int, default=MAX_SEQ_LENGTH)
    parser.add_argument("--min-f1", type=float, default=0.85,
                        help="P3.2 acceptance gate on best val macro F1")
    args = parser.parse_args(argv)
    try:
        summary = train_classifier(
            train=load_split(args.train),
            val=load_split(args.val),
            test=load_split(args.test),
            base_model=args.base_model,
            tokenizer_dir=args.tokenizer_dir,
            out_dir=args.out_dir,
            epochs=args.epochs,
            batch_size=args.batch_size,
            lr=args.lr,
            seed=args.seed,
            max_seq_length=args.max_seq_length,
        )
    except (ValueError, OSError) as exc:
        logger.error("%s", exc)
        return 1
    if summary["best_val_f1"] < args.min_f1:  # section 6 P3.2 acceptance gate
        logger.error("Gate failed: best val F1 %.4f < required %.4f",
                     summary["best_val_f1"], args.min_f1)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
