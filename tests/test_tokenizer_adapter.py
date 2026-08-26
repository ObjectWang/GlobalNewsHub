"""P5 tokenizer adapter tests: tokenizers-direct path == AutoTokenizer.

The packaged build excludes transformers; the fallback adapter must
produce byte-identical feeds so ONNX inference is unaffected.
"""

from __future__ import annotations

import numpy as np
import pytest

pytest.importorskip("tokenizers", reason="tokenizers extra not installed")

from core.classifier.bert_classifier import _TokenizersAdapter  # noqa: E402

SAMPLES = [
    ("国务院发布减税降费政策", "财政部称减免超5000亿元"),
    ("Short ascii title", ""),
    ("解放军东部战区组织联合军演" * 20, "超长文本触发截断逻辑"),  # > max_length
]


def test_adapter_matches_auto_tokenizer(mini_tokenizer_dir) -> None:
    transformers = pytest.importorskip("transformers")
    ref = transformers.AutoTokenizer.from_pretrained(str(mini_tokenizer_dir))
    adapter = _TokenizersAdapter(mini_tokenizer_dir)

    for title, summary in SAMPLES:
        kwargs = dict(truncation="longest_first", padding=True,
                      max_length=32, return_tensors="np")
        want = ref(title, summary, **kwargs)
        got = adapter(title, summary, truncation="longest_first",
                      padding=True, max_length=32)
        assert np.array_equal(np.asarray(want["input_ids"]), got["input_ids"]), title
        assert np.array_equal(
            np.asarray(want["attention_mask"]), got["attention_mask"]
        ), title


def test_batch_shapes_and_padding(mini_tokenizer_dir) -> None:
    adapter = _TokenizersAdapter(mini_tokenizer_dir)
    out = adapter(["短标题", "另一个长得多的标题内容"], ["摘要", ""],
                  truncation="longest_first", padding=True, max_length=16)
    width = out["input_ids"].shape[1]
    assert out["attention_mask"].sum(axis=1).max() == width
    for row in out["attention_mask"]:
        ones = int(row.sum())
        assert list(row[:ones]) == [1] * ones      # attended prefix
        assert list(row[ones:]) == [0] * (len(row) - ones)  # pad suffix


def test_load_tokenizer_falls_back_without_transformers(
    mini_tokenizer_dir, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_load_tokenizer picks the adapter when transformers is absent."""
    import builtins

    from core.classifier import bert_classifier as bc

    real_import = builtins.__import__

    def blocked(name: str, *args: object, **kwargs: object):
        if name == "transformers" or name.startswith("transformers."):
            raise ImportError("transformers excluded in frozen build")
        return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(builtins, "__import__", blocked)
    tokenizer = bc._load_tokenizer(mini_tokenizer_dir)
    assert isinstance(tokenizer, bc._TokenizersAdapter)
    out = tokenizer(["军演"], [""], truncation="longest_first",
                    padding=True, max_length=32)
    assert set(out) >= {"input_ids", "attention_mask", "token_type_ids"}
