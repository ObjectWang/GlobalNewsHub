"""ONNX Runtime inference wrapper for the category classifier (P3.4).

Loads the INT8-quantized BERT (section 3.1) plus the HuggingFace
tokenizer and exposes a tiny synchronous API. Thread-safety note: one
:class:`BertClassifier` instance maps to one ONNX session; callers that
classify concurrently (e.g. a QThread worker, section 1.2) should share
one instance — ``onnxruntime`` sessions are safe for concurrent ``run``
calls. All blocking work stays out of the UI thread by contract.

The heavy imports (onnxruntime/transformers/numpy) are deferred to
construction time so importing this module never costs startup latency
(section 1.3: cold start <= 3s).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

import numpy as np

from core.constants import CATEGORIES, MAX_SEQ_LENGTH

logger = logging.getLogger(__name__)

_PROVIDERS: tuple[str, ...] = ("CPUExecutionProvider",)


class OnnxSession(Protocol):
    """Minimal surface of ``onnxruntime.InferenceSession`` we rely on."""

    def get_inputs(self) -> Sequence[Any]:
        """Return model input metadata objects carrying ``.name``."""
        ...

    def run(
        self, output_names: None, feed: Mapping[str, np.ndarray]
    ) -> list[np.ndarray]:
        """Execute the graph; return the requested outputs."""
        ...


class TokenizerFunc(Protocol):
    """Minimal surface of a HuggingFace fast tokenizer."""

    def __call__(
        self,
        text: str | list[str],
        text_pair: str | list[str] | None = ...,
        truncation: bool | str = ...,
        padding: bool = ...,
        max_length: int = ...,
        return_tensors: str | None = ...,
    ) -> Mapping[str, Any]:
        """Tokenize text pair(s); returns input_ids / attention_mask / ..."""
        ...


def softmax(logits: np.ndarray) -> np.ndarray:
    """Numerically stable softmax along the last axis."""
    shifted = logits - logits.max(axis=-1, keepdims=True)
    exp = np.exp(shifted.astype(np.float64))
    return np.asarray(exp / exp.sum(axis=-1, keepdims=True))


class _TokenizersAdapter:
    """``tokenizers``-library drop-in for the AutoTokenizer call convention.

    Produces the same ``input_ids`` / ``attention_mask`` / ``token_type_ids``
    numpy arrays as a HuggingFace fast tokenizer for the subset of kwargs
    :meth:`BertClassifier._feed` uses (pair encoding, longest-first
    truncation, batch padding to the longest sequence).
    """

    def __init__(self, tokenizer_dir: Path) -> None:
        from tokenizers import Tokenizer  # type: ignore[import-untyped]

        self._tok = Tokenizer.from_file(str(Path(tokenizer_dir) / "tokenizer.json"))
        self._tok.no_truncation()
        self._tok.no_padding()

    def __call__(
        self,
        text: str | list[str],
        text_pair: str | list[str] | None = None,
        truncation: bool | str = True,
        max_length: int = MAX_SEQ_LENGTH,
        **_: Any,
    ) -> dict[str, Any]:
        titles = [text] if isinstance(text, str) else list(text)
        pairs: list[str | None]
        if text_pair is None:
            pairs = [None] * len(titles)
        elif isinstance(text_pair, str):
            pairs = [text_pair] * len(titles)
        else:
            pairs = list(text_pair)
        if truncation and max_length:
            self._tok.enable_truncation(
                max_length=max_length, strategy="longest_first"
            )
        encodings = [
            # HF semantics: an empty text_pair encodes as a SINGLE sequence
            # (no trailing second [SEP]) — match that for empty summaries.
            self._tok.encode(t, s) if s else self._tok.encode(t)
            for t, s in zip(titles, pairs, strict=True)
        ]
        pad_id = self._tok.token_to_id("[PAD]") or 0
        width = max((len(e.ids) for e in encodings), default=0)

        ids_out: list[list[int]] = []
        mask_out: list[list[int]] = []
        type_out: list[list[int]] = []
        for e in encodings:
            gap = width - len(e.ids)
            ids_out.append(list(e.ids) + [pad_id] * gap)
            mask_out.append(list(e.attention_mask) + [0] * gap)
            type_out.append(list(e.type_ids) + [0] * gap)
        return {
            "input_ids": np.asarray(ids_out, dtype=np.int64),
            "attention_mask": np.asarray(mask_out, dtype=np.int64),
            "token_type_ids": np.asarray(type_out, dtype=np.int64),
        }


def _load_tokenizer(tokenizer_dir: Path) -> Any:
    """AutoTokenizer when transformers exists; raw adapter otherwise."""
    try:
        from transformers import AutoTokenizer
    except Exception:  # frozen build excludes transformers entirely
        logger.info("transformers unavailable; using tokenizers adapter")
        return _TokenizersAdapter(tokenizer_dir)
    return AutoTokenizer.from_pretrained(str(tokenizer_dir))


class BertClassifier:
    """Category scores from an exported BERT ONNX model (section 3.1)."""

    def __init__(
        self,
        *,
        session: OnnxSession,
        tokenizer: TokenizerFunc,
        labels: Sequence[str] = CATEGORIES,
        max_seq_length: int = MAX_SEQ_LENGTH,
    ) -> None:
        self._session = session
        self._tokenizer = tokenizer
        self.labels: tuple[str, ...] = tuple(labels)
        self.max_seq_length = max_seq_length
        self._input_names = tuple(item.name for item in session.get_inputs())

    @classmethod
    def from_files(
        cls,
        model_path: Path,
        tokenizer_dir: Path,
        labels: Sequence[str] = CATEGORIES,
        max_seq_length: int = MAX_SEQ_LENGTH,
    ) -> BertClassifier:
        """Build from the artifacts under ``resources/models`` (settings.yaml).

        Tokenizer loading prefers ``transformers.AutoTokenizer`` and falls
        back to a direct ``tokenizers`` adapter when transformers is
        unavailable — the packaged build (P5.1) excludes transformers to
        keep install size and cold start within section 1.3 limits; the
        adapter reads the same standard ``tokenizer.json`` artifact.
        """
        import onnxruntime

        if not Path(model_path).is_file():
            raise FileNotFoundError(f"ONNX model not found: {model_path}")
        if not Path(tokenizer_dir).is_dir():
            raise FileNotFoundError(f"tokenizer directory not found: {tokenizer_dir}")
        session = onnxruntime.InferenceSession(
            str(model_path), providers=list(_PROVIDERS)
        )
        tokenizer = _load_tokenizer(Path(tokenizer_dir))
        logger.info("Loaded classifier %s (%d labels)", model_path, len(labels))
        return cls(
            session=session,
            tokenizer=tokenizer,
            labels=labels,
            max_seq_length=max_seq_length,
        )

    def _feed(self, titles: list[str], summaries: list[str]) -> dict[str, np.ndarray]:
        """Tokenize and assemble int64 feeds restricted to model inputs."""
        encoded = self._tokenizer(
            titles,
            summaries,
            truncation="longest_first",
            padding=True,
            max_length=self.max_seq_length,
            return_tensors="np",
        )
        feed = {
            name: np.asarray(encoded[name], dtype=np.int64)
            for name in self._input_names
            if name in encoded
        }
        missing = [name for name in self._input_names if name not in feed]
        if missing:
            raise ValueError(f"tokenizer did not produce model inputs: {missing}")
        return feed

    def predict_proba_batch(
        self, pairs: Sequence[tuple[str, str]]
    ) -> list[dict[str, float]]:
        """Probability dicts for a batch of (title, summary) pairs."""
        if not pairs:
            return []
        feed = self._feed([p[0] for p in pairs], [p[1] for p in pairs])
        logits = self._session.run(None, feed)[0]
        probs = softmax(np.asarray(logits))
        return [
            {label: float(p) for label, p in zip(self.labels, row, strict=True)}
            for row in probs
        ]

    def classify(self, title: str, summary: str = "") -> tuple[str, float]:
        """Return ``(label, confidence)`` for one article heading."""
        results = self.predict_proba_batch([(title, summary)])
        best = max(results[0].items(), key=lambda kv: kv[1])
        return best[0], best[1]
