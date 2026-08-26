#!/usr/bin/env python3
"""ONNX export + INT8 dynamic quantization (PRD task P3.3).

Takes a P3.2 fine-tuned checkpoint and produces:

- ``{name}_fp32.onnx``   — full-precision export, opset 14 (section 3.1),
                           dynamic batch/sequence axes;
- ``{name}_int8.onnx``   — INT8 dynamic quantization via
                           ``onnxruntime.quantization.quantize_dynamic``
                           (acceptance: <= 120MB);
- ``{name}_report.json`` — sizes, parity check (max |fp32-int8| logit diff
                           and argmax agreement), opset.

The FP32 intermediate is removed unless ``--keep-fp32`` is given.
Offline-only tooling (pyproject [train] extra); nothing in core/ or ui/
imports this module.

Usage:
    python scripts/export_onnx.py \
        --checkpoint data/checkpoints/category \
        --tokenizer-dir resources/models/tokenizer \
        --out-dir resources/models --name category_classifier
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
import types
from pathlib import Path
from typing import Any

# scripts/ is not a package; make the repo root importable on direct runs.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from core.constants import MAX_SEQ_LENGTH  # noqa: E402

logger = logging.getLogger(__name__)

DEFAULT_OPSET: int = 14  # section 3.1 model spec


def _build_logits_wrapper(model: Any) -> Any:
    """Wrap a HF sequence-classification model to output raw ``logits``.

    torch.onnx.export traces a single tensor output far more reliably than
    the ModelOutput dataclass; ``token_type_ids`` stays optional because
    some tokenizer/model combos omit it.
    """
    import torch.nn as nn

    class _LogitsWrapper(nn.Module):
        """Tracing shim: (input_ids, attention_mask[, token_type_ids]) → logits."""

        def __init__(self) -> None:
            super().__init__()
            self.model = model

        def forward(  # type: ignore[override]
            self,
            input_ids: Any,
            attention_mask: Any,
            token_type_ids: Any = None,
        ) -> Any:
            kwargs: dict[str, Any] = {
                "input_ids": input_ids,
                "attention_mask": attention_mask,
            }
            if token_type_ids is not None:
                kwargs["token_type_ids"] = token_type_ids
            return self.model(**kwargs).logits

    return _LogitsWrapper()


def _sample_encoding(
    checkpoint: Path,
    tokenizer_dir: Path,
    max_seq_length: int,
) -> dict[str, Any]:
    """Tokenize fixed sample texts; their shapes seed the traced graph."""
    from transformers import AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(str(tokenizer_dir))
    encoded = tokenizer(
        ["中国股市发布新政策报告", "军演部队武器装备"],
        ["相关摘要内容一", "相关摘要内容二"],
        truncation="longest_first",
        padding=True,
        max_length=max_seq_length,
        return_tensors="pt",
    )
    return {key: encoded[key] for key in ("input_ids", "attention_mask",
                                          "token_type_ids")}


def export_fp32(
    *,
    checkpoint: Path,
    tokenizer_dir: Path,
    out_path: Path,
    opset: int = DEFAULT_OPSET,
    max_seq_length: int = MAX_SEQ_LENGTH,
) -> None:
    """Export the checkpoint to a FP32 ONNX file with dynamic axes."""
    import torch
    from transformers import AutoModelForSequenceClassification

    model = AutoModelForSequenceClassification.from_pretrained(str(checkpoint))
    model.eval()
    wrapper = _build_logits_wrapper(model)
    encoded = _sample_encoding(checkpoint, tokenizer_dir, max_seq_length)
    args = tuple(encoded[key] for key in ("input_ids", "attention_mask",
                                          "token_type_ids"))
    out_path.parent.mkdir(parents=True, exist_ok=True)

    common = dict(
        input_names=["input_ids", "attention_mask", "token_type_ids"],
        output_names=["logits"],
        dynamic_axes={
            name: {0: "batch", 1: "sequence"}
            for name in ("input_ids", "attention_mask", "token_type_ids")
        },
        opset_version=opset,
    )
    try:
        # TorchScript tracer path: stable with HF models through torch 2.x.
        torch.onnx.export(wrapper, args, str(out_path), **common, dynamo=False)
    except TypeError:
        # Newer torch removed the legacy exporter; fall back to dynamo
        # (requires the optional onnxscript dependency).
        logger.warning("legacy ONNX exporter unavailable, using dynamo=True")
        torch.onnx.export(wrapper, args, str(out_path), **common, dynamo=True)


def _probe(command: list[str]) -> int:
    """Run a python snippet in an isolated subprocess; return its exit code."""
    return subprocess.run(
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    ).returncode


def _shield_onnx_reference_if_broken() -> None:
    """Neutralize a native crash in ``onnx.reference`` on some hosts.

    ``onnxruntime.quantization`` imports ``onnx.reference.ReferenceEvaluator``
    at module level, but the INT8 dynamic path never *calls* it (only the
    float8/4-bit branches do, see quant_utils.py in onnxruntime). On some
    Windows setups importing that module aborts the whole process inside
    onnx's C++ schema registry. Probe it in an isolated subprocess; only
    when it is genuinely broken do we pre-seed ``sys.modules`` with an
    inert stub so the quantization import stays safe and loud if ever hit.
    """
    if "onnxruntime.quantization" in sys.modules:
        return
    if _probe([sys.executable, "-c", "import onnx.reference"]) == 0:
        return
    logger.warning(
        "importing onnx.reference crashes on this machine; installing an "
        "inert stub — INT8 dynamic quantization does not use it"
    )
    stub = types.ModuleType("onnx.reference")

    class _StubReferenceEvaluator:  # pragma: no cover - must never run
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            raise RuntimeError(
                "ReferenceEvaluator is unavailable: the real onnx.reference "
                "module cannot be imported on this machine"
            )

    stub.ReferenceEvaluator = _StubReferenceEvaluator  # type: ignore[attr-defined]
    sys.modules["onnx.reference"] = stub


def _shield_shape_inference_if_broken() -> None:
    """Bypass C++ shape inference inside ORT quantization when it crashes.

    Some hosts segfault inside ``onnx.shape_inference`` (same C++ schema
    registry as above). Shape info is optional for *dynamic* INT8
    quantization (weights-only), so when the native path proves broken we
    replace ``load_model_with_shape_infer`` / its save-reload sibling with
    plain proto loads in both modules that reference them.
    """
    import onnx
    from onnxruntime.quantization import quant_utils

    probe_snippet = (
        "import tempfile, onnx\n"
        "from onnx import helper, TensorProto\n"
        "x = helper.make_tensor_value_info('X', TensorProto.FLOAT, [1])\n"
        "g = helper.make_graph([helper.make_node('Identity', ['X'], ['Y'])],"
        " 'g', [x], [helper.make_tensor_value_info('Y', TensorProto.FLOAT, [1])])\n"
        "p = tempfile.mktemp(suffix='.onnx')\n"
        "onnx.save(helper.make_model(g), p)\n"
        "onnx.shape_inference.infer_shapes_path(p)\n"
    )
    if _probe([sys.executable, "-c", probe_snippet]) == 0:
        return
    logger.warning(
        "onnx.shape_inference crashes on this machine; quantization will "
        "skip shape inference (harmless for dynamic INT8)"
    )

    def _load_without_infer(model_path: Any) -> Any:
        model = onnx.load(str(model_path))
        quant_utils.add_infer_metadata(model)
        return model

    def _identity_reload(model: Any) -> Any:
        return model

    # Patch the *function's own* globals: ORT can end up with duplicate
    # module instances, so rewriting ``sys.modules`` attributes alone may
    # miss the instance whose closure actually runs.
    from onnxruntime.quantization import quantize_dynamic  # noqa: PLC0415

    call_site_globals = quantize_dynamic.__globals__
    if "load_model_with_shape_infer" in call_site_globals:
        call_site_globals["load_model_with_shape_infer"] = _load_without_infer
        call_site_globals["save_and_reload_model_with_shape_infer"] = (
            _identity_reload
        )
    quant_utils.load_model_with_shape_infer = _load_without_infer
    quant_utils.save_and_reload_model_with_shape_infer = _identity_reload


def quantize_int8(*, fp32_in: Path, int8_out: Path) -> None:
    """INT8 dynamic weight quantization (section 3.1 quantization spec)."""
    import onnx  # noqa: PLC0415 (lazy, heavy)

    _shield_onnx_reference_if_broken()
    from onnxruntime.quantization import QuantType  # noqa: PLC0415 (lazy)

    _shield_shape_inference_if_broken()
    from onnxruntime.quantization import quantize_dynamic  # noqa: PLC0415

    int8_out.parent.mkdir(parents=True, exist_ok=True)
    # When native shape inference had to be skipped, the quantizer cannot
    # read activation dtypes from value_info; our exported graphs are
    # pure-FP32 BERTs, so declaring FLOAT as the default type is exact.
    quantize_dynamic(
        model_input=str(fp32_in),
        model_output=str(int8_out),
        weight_type=QuantType.QInt8,
        extra_options={"DefaultTensorType": onnx.TensorProto.FLOAT},
    )


def verify_parity(
    *,
    fp32_session: Any,
    int8_session: Any,
    feeds: dict[str, Any],
) -> dict[str, object]:
    """Compare logits of both artifacts on the sample inputs."""
    fp32_logits = fp32_session.run(None, feeds)[0]
    int8_logits = int8_session.run(None, feeds)[0]
    max_abs_diff = float(abs(fp32_logits - int8_logits).max())
    argmax_agree = bool((fp32_logits.argmax(-1) == int8_logits.argmax(-1)).all())
    if not argmax_agree or max_abs_diff > 0.05:
        logger.warning("quantization parity degraded: diff=%.6f agree=%s",
                       max_abs_diff, argmax_agree)
    return {"max_abs_diff": max_abs_diff, "argmax_agree": argmax_agree}


def size_mb(path: Path) -> float:
    """File size in megabytes."""
    return path.stat().st_size / (1024 * 1024)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point; 2 = size gate failure (PRD P3.3 acceptance)."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--checkpoint", type=Path, required=True,
                        help="P3.2 output directory (model + tokenizer)")
    parser.add_argument("--tokenizer-dir", type=Path, default=None,
                        help="defaults to the checkpoint directory itself")
    parser.add_argument("--out-dir", type=Path, default=Path("resources/models"))
    parser.add_argument("--name", default="category_classifier",
                        help="artifact stem; final file is {name}_int8.onnx")
    parser.add_argument("--opset", type=int, default=DEFAULT_OPSET)
    parser.add_argument("--max-seq-length", type=int, default=MAX_SEQ_LENGTH)
    parser.add_argument("--max-size-mb", type=float, default=120.0,
                        help="P3.3 acceptance gate for the INT8 artifact")
    parser.add_argument("--keep-fp32", action="store_true",
                        help="retain the intermediate FP32 ONNX file")
    args = parser.parse_args(argv)

    tokenizer_dir = args.tokenizer_dir or args.checkpoint
    fp32_path = args.out_dir / f"{args.name}_fp32.onnx"
    int8_path = args.out_dir / f"{args.name}_int8.onnx"
    try:
        export_fp32(checkpoint=args.checkpoint, tokenizer_dir=tokenizer_dir,
                    out_path=fp32_path, opset=args.opset,
                    max_seq_length=args.max_seq_length)
        quantize_int8(fp32_in=fp32_path, int8_out=int8_path)

        import numpy as np
        import onnxruntime as ort

        providers = ["CPUExecutionProvider"]
        encoded = _sample_encoding(args.checkpoint, tokenizer_dir,
                                   args.max_seq_length)
        feeds = {
            key: value.detach().numpy().astype(np.int64)
            for key, value in encoded.items()
        }
        parity = verify_parity(
            fp32_session=ort.InferenceSession(str(fp32_path), providers=providers),
            int8_session=ort.InferenceSession(str(int8_path), providers=providers),
            feeds=feeds,
        )

        mb = size_mb(int8_path)
        report = {
            "name": args.name,
            "opset": args.opset,
            "size_mb": mb,
            "parity": parity,
        }
        report_path = args.out_dir / f"{args.name}_report.json"
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2)
                               + "\n", encoding="utf-8")
        logger.info("exported %s (%.2f MB, parity=%s)", int8_path, mb, parity)
    except (OSError, RuntimeError, ValueError) as exc:
        logger.error("%s", exc)
        return 1
    finally:
        if not args.keep_fp32 and fp32_path.exists():
            fp32_path.unlink()

    if mb > args.max_size_mb:  # PRD P3.3 gate: INT8 <= 120MB
        logger.error("Gate failed: %s is %.2f MB > %.2f MB",
                     int8_path, mb, args.max_size_mb)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
