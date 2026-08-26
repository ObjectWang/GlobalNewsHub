"""Tests for scripts/export_onnx.py (PRD task P3.3).

Acceptance: the trained checkpoint exports to ONNX (opset 14) and the
INT8 dynamic-quantized artifact stays under 120MB while remaining a
loadable, behavior-preserving onnxruntime model. Verified offline on the
tiny conftest checkpoint; the 120MB gate itself is exercised with an
impossibly small limit so the test does not depend on model size.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

pytest.importorskip("torch", reason="train extra not installed")
pytest.importorskip("onnxruntime")

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "export_onnx.py"


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("export_onnx", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["export_onnx"] = module
    spec.loader.exec_module(module)
    return module


eo = _load_script()


def _session(path: Path):  # type: ignore[no-untyped-def]
    import onnxruntime as ort

    return ort.InferenceSession(
        str(path), providers=["CPUExecutionProvider"]
    )


def _feeds(mini_tokenizer_dir: Path, seq_len: int = 16) -> dict[str, object]:
    """Build valid model inputs, longer than one token to exercise padding."""
    transformers = pytest.importorskip("transformers")
    tokenizer = transformers.AutoTokenizer.from_pretrained(str(mini_tokenizer_dir))
    encoded = tokenizer(
        ["中国股市发布新政策报告", "军演部队武器装备"],
        ["相关摘要内容一", "相关摘要内容二"],
        truncation=True,
        max_length=seq_len,
        return_tensors="np",
    )
    return {
        "input_ids": encoded["input_ids"].astype("int64"),
        "attention_mask": encoded["attention_mask"].astype("int64"),
        "token_type_ids": encoded["token_type_ids"].astype("int64"),
    }


def test_export_fp32_creates_loadable_opset14_model(
    tmp_path: Path,
    mini_checkpoint: Path,
    mini_tokenizer_dir: Path,
) -> None:
    out = tmp_path / "model_fp32.onnx"
    eo.export_fp32(
        checkpoint=mini_checkpoint,
        tokenizer_dir=mini_tokenizer_dir,
        out_path=out,
        opset=14,
    )
    session = _session(out)
    outputs = session.run(None, dict(_feeds(mini_tokenizer_dir)))
    assert outputs[0].shape == (2, 6)


def test_quantize_int8_produces_distinct_working_artifact(
    tmp_path: Path,
    mini_checkpoint: Path,
    mini_tokenizer_dir: Path,
) -> None:
    fp32 = tmp_path / "model_fp32.onnx"
    int8 = tmp_path / "model_int8.onnx"
    eo.export_fp32(checkpoint=mini_checkpoint, tokenizer_dir=mini_tokenizer_dir,
                   out_path=fp32, opset=14)
    eo.quantize_int8(fp32_in=fp32, int8_out=int8)
    assert int8.read_bytes() != fp32.read_bytes()
    outputs = _session(int8).run(None, dict(_feeds(mini_tokenizer_dir)))
    assert outputs[0].shape == (2, 6)


def test_parity_between_fp32_and_int8(
    tmp_path: Path,
    mini_checkpoint: Path,
    mini_tokenizer_dir: Path,
) -> None:
    import numpy as np

    fp32 = tmp_path / "model_fp32.onnx"
    int8 = tmp_path / "model_int8.onnx"
    eo.export_fp32(checkpoint=mini_checkpoint, tokenizer_dir=mini_tokenizer_dir,
                   out_path=fp32, opset=14)
    eo.quantize_int8(fp32_in=fp32, int8_out=int8)
    feeds = dict(_feeds(mini_tokenizer_dir))
    report = eo.verify_parity(fp32_session=_session(fp32),
                              int8_session=_session(int8), feeds=feeds)
    assert set(report) >= {"max_abs_diff", "argmax_agree"}
    assert np.isfinite(report["max_abs_diff"])
    assert report["argmax_agree"] is True


def test_cli_end_to_end_removes_intermediate_fp32(
    tmp_path: Path,
    mini_checkpoint: Path,
    mini_tokenizer_dir: Path,
) -> None:
    out_dir = tmp_path / "models"
    code = eo.main(
        [
            "--checkpoint", str(mini_checkpoint),
            "--tokenizer-dir", str(mini_tokenizer_dir),
            "--out-dir", str(out_dir),
            "--name", "category_classifier",
            "--max-size-mb", "120",
        ]
    )
    assert code == 0
    int8 = out_dir / "category_classifier_int8.onnx"
    assert int8.is_file()
    # Intermediate FP32 artifact is cleaned up unless --keep-fp32 is set.
    assert not (out_dir / "category_classifier_fp32.onnx").exists()
    report = json.loads((out_dir / "category_classifier_report.json").read_text())
    assert report["size_mb"] <= 120
    assert report["opset"] == 14


def test_cli_size_gate_fails_run(
    tmp_path: Path,
    mini_checkpoint: Path,
    mini_tokenizer_dir: Path,
) -> None:
    code = eo.main(
        [
            "--checkpoint", str(mini_checkpoint),
            "--tokenizer-dir", str(mini_tokenizer_dir),
            "--out-dir", str(tmp_path / "models"),
            "--name", "category_classifier",
            "--max-size-mb", "0.000001",  # impossible gate -> exit code 2
        ]
    )
    assert code == 2
