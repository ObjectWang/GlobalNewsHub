"""Tests for scripts/train_classifier.py (PRD task P3.2).

Acceptance: the fine-tuning pipeline consumes the P3.1 JSONL contract,
produces a loadable checkpoint + label mapping, and enforces the
"val F1 >= 0.85" gate. Real training needs THUCNews + a training box;
these tests run the full plumbing on a tiny offline BERT instead.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

pytest.importorskip("torch", reason="train extra not installed")

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "train_classifier.py"


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("train_classifier", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["train_classifier"] = module
    spec.loader.exec_module(module)
    return module


tc = _load_script()


# ------------------------------------------------------------ data loading


def test_load_split_reads_p3_1_schema(tmp_path: Path) -> None:
    path = tmp_path / "train.jsonl"
    path.write_text(
        json.dumps(
            {"id": "a", "title": "t", "summary": "s", "label": "tech", "source": "x"},
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    records = tc.load_split(path)
    assert len(records) == 1
    assert set(records[0]) == {"id", "title", "summary", "label", "source"}
    assert records[0]["label"] == "tech"


def test_load_split_rejects_unknown_label(tmp_path: Path) -> None:
    path = tmp_path / "bad.jsonl"
    path.write_text(
        json.dumps({"id": "a", "title": "t", "summary": "s", "label": "sports",
                    "source": "x"}, ensure_ascii=False),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="label"):
        tc.load_split(path)


def test_encode_texts_respects_max_length(mini_tokenizer_dir: Path) -> None:
    transformers = pytest.importorskip("transformers")
    tokenizer = transformers.AutoTokenizer.from_pretrained(str(mini_tokenizer_dir))
    titles = ["政治新闻标题"] * 3
    summaries = ["摘要内容" * 200] * 3  # way beyond 128 tokens
    encoded = tc.encode_texts(tokenizer, titles, summaries, max_seq_length=128)
    assert encoded["input_ids"].shape[0] == 3
    assert encoded["input_ids"].shape[1] <= 128
    assert set(encoded) == {"input_ids", "attention_mask", "token_type_ids"}


def test_macro_f1_matches_sklearn_reference() -> None:
    sklearn = pytest.importorskip("sklearn")
    y_true = ["politics", "economy", "tech", "tech", "other"]
    y_pred = ["politics", "economy", "economy", "tech", "other"]
    ours = tc.macro_f1(y_true, y_pred)
    reference = float(
        sklearn.metrics.f1_score(y_true, y_pred, average="macro")
    )
    assert abs(ours - reference) < 1e-9


def test_set_seed_is_deterministic() -> None:
    torch = pytest.importorskip("torch")
    random = pytest.importorskip("random")
    tc.set_seed(1234)
    first = [torch.rand(4).tolist(), random.random()]
    tc.set_seed(1234)
    second = [torch.rand(4).tolist(), random.random()]
    assert first == second


# ------------------------------------------------------- end-to-end smoke


@pytest.fixture()
def trained_run(
    tmp_path: Path,
    mini_checkpoint: Path,
    mini_tokenizer_dir: Path,
    mini_jsonl_dataset: dict[str, Path],
) -> Path:
    """Run one tiny training epoch via the CLI; return the output dir."""
    out_dir = tmp_path / "checkpoint"
    code = tc.main(
        [
            "--train", str(mini_jsonl_dataset["train"]),
            "--val", str(mini_jsonl_dataset["val"]),
            "--test", str(mini_jsonl_dataset["test"]),
            "--base-model", str(mini_checkpoint),
            "--tokenizer-dir", str(mini_tokenizer_dir),
            "--out-dir", str(out_dir),
            "--epochs", "1",
            "--batch-size", "8",
            "--min-f1", "0",
        ]
    )
    assert code == 0
    return out_dir


def test_training_writes_loadable_checkpoint_and_reports(trained_run: Path) -> None:
    transformers = pytest.importorskip("transformers")
    # Checkpoint must be reloadable through Auto* (contract for P3.3 export).
    model = transformers.AutoModelForSequenceClassification.from_pretrained(
        str(trained_run)
    )
    assert model.config.num_labels == 6
    labels_doc = json.loads((trained_run / "labels.json").read_text(encoding="utf-8"))
    assert labels_doc["labels"] == list(model.config.id2label.values())
    history = json.loads((trained_run / "history.json").read_text(encoding="utf-8"))
    assert len(history["val_f1"]) == 1
    report = json.loads((trained_run / "test_report.json").read_text(encoding="utf-8"))
    assert 0.0 <= report["f1_macro"] <= 1.0
    assert set(report["per_label"]) == {
        "politics", "economy", "military", "life", "tech", "other",
    }


def test_f1_gate_returns_exit_code_2(
    tmp_path: Path,
    mini_checkpoint: Path,
    mini_tokenizer_dir: Path,
    mini_jsonl_dataset: dict[str, Path],
) -> None:
    """The acceptance gate 'val F1 >= min-f1' fails the run when unmet."""
    code = tc.main(
        [
            "--train", str(mini_jsonl_dataset["train"]),
            "--val", str(mini_jsonl_dataset["val"]),
            "--test", str(mini_jsonl_dataset["test"]),
            "--base-model", str(mini_checkpoint),
            "--tokenizer-dir", str(mini_tokenizer_dir),
            "--out-dir", str(tmp_path / "ckpt"),
            "--epochs", "1",
            "--batch-size", "8",
            "--min-f1", "0.99",  # unreachable for a 1-epoch tiny model
        ]
    )
    assert code == 2
