"""Shared test fixtures for the classifier training/export tests (P3.2-P3.3).

The real ``hfl/chinese-roberta-wwm-ext`` base model (~400MB, HuggingFace
download) must never be fetched inside unit tests. Instead these helpers
build an architecturally identical but tiny BERT (hidden 32, 2 layers)
plus a matching WordPiece tokenizer from a hand-written vocab file. The
training/export scripts consume them exactly like a real checkpoint, so
the plumbing is verified end to end while staying offline and fast.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from PySide6.QtWidgets import QApplication


def _bootstrap_qt() -> None:
    """Apply ui's Windows Qt DLL bootstrap before any PySide6 import.

    Core-only test runs stay unaffected when PySide6 is not installed.
    """
    try:
        import PySide6  # noqa: F401
    except ImportError:
        return
    import ui

    ui.ensure_qt_runtime()


_bootstrap_qt()

# Must match BertTokenizer specials: ids are fixed by position in vocab.txt.
SPECIAL_TOKENS: list[str] = ["[PAD]", "[UNK]", "[CLS]", "[SEP]", "[MASK]"]

_CJK = (
    "新闻政治经济社会科技军事生活教育医疗体育游戏娱乐"
    "股市金融贸易央行武器军演导弹人工智能芯片互联网手机航天"
    "中国美国英国法国德国日本韩国朝鲜印度新加坡欧盟联合国"
    "北京上海华盛顿纽约伦敦巴黎柏林东京首尔加沙伊朗以色列沙特全球国际"
    "公司市场政策报告增长发布研究服务项目安全发展合作问题影响"
)


def build_mini_tokenizer(target: Path) -> Path:
    """Write a minimal but valid BERT tokenizer directory; return it.

    Contains only ``vocab.txt`` + ``tokenizer_config.json`` — exactly what
    ``AutoTokenizer.from_pretrained`` needs for a slow/fast BertTokenizer.
    """
    tokens = SPECIAL_TOKENS + [chr(c) for c in range(ord("!"), ord("~") + 1)]
    tokens += list(dict.fromkeys(_CJK))
    target.mkdir(parents=True, exist_ok=True)
    (target / "vocab.txt").write_text("\n".join(tokens) + "\n", encoding="utf-8")

    # transformers >= 5 refuses to convert bare vocab.txt into a fast
    # tokenizer, so serialize the standard ``tokenizer.json`` ourselves
    # (the same file shape a real HuggingFace tokenizer directory has).
    from tokenizers import (
        Tokenizer,
        decoders,
        models,
        normalizers,
        pre_tokenizers,
        processors,
    )

    wordpiece = models.WordPiece(
        vocab={token: i for i, token in enumerate(tokens)},
        unk_token="[UNK]",
        continuing_subword_prefix="##",
    )
    fast = Tokenizer(wordpiece)
    fast.normalizer = normalizers.BertNormalizer(
        clean_text=True,
        handle_chinese_chars=True,
        strip_accents=True,
        lowercase=True,
    )
    fast.pre_tokenizer = pre_tokenizers.BertPreTokenizer()
    fast.post_processor = processors.TemplateProcessing(
        single="[CLS] $A [SEP]",
        pair="[CLS] $A [SEP] $B:1 [SEP]:1",
        special_tokens=[
            ("[CLS]", tokens.index("[CLS]")),
            ("[SEP]", tokens.index("[SEP]")),
        ],
    )
    fast.decoder = decoders.WordPiece(prefix="##")
    fast.save(str(target / "tokenizer.json"))
    (target / "tokenizer_config.json").write_text(
        json.dumps({"model_max_length": 128, "tokenizer_class": "BertTokenizer"}),
        encoding="utf-8",
    )
    (target / "special_tokens_map.json").write_text(
        json.dumps(
            {
                "unk_token": "[UNK]",
                "sep_token": "[SEP]",
                "pad_token": "[PAD]",
                "cls_token": "[CLS]",
                "mask_token": "[MASK]",
            }
        ),
        encoding="utf-8",
    )
    return target


def mini_vocab_size(target: Path) -> int:
    """Number of tokens in :func:`build_mini_tokenizer` output."""
    return len((target / "vocab.txt").read_text(encoding="utf-8").splitlines())


@pytest.fixture(scope="session")
def qapp() -> QApplication:
    """One offscreen QApplication shared by every UI test (P4.x).

    ``QT_QPA_PLATFORM=offscreen`` must be set *before* PySide6 imports,
    so the environment variable is applied here rather than at module top.
    """
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pyside_widgets = pytest.importorskip("PySide6.QtWidgets", reason="UI extra not installed")
    app = pyside_widgets.QApplication.instance()
    if app is None:
        app = pyside_widgets.QApplication([])
    return app


@pytest.fixture(scope="session")
def mini_tokenizer_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return build_mini_tokenizer(tmp_path_factory.mktemp("tok") / "tokenizer")


@pytest.fixture(scope="session")
def mini_checkpoint(
    tmp_path_factory: pytest.TempPathFactory,
    mini_tokenizer_dir: Path,
) -> Path:
    """A tiny random-weight BERT sequence-classification checkpoint."""
    pytest.importorskip("torch", reason="training extra not installed")
    transformers = pytest.importorskip("transformers")

    labels = ("politics", "economy", "military", "life", "tech", "other")
    # Deterministic weights: INT8 parity assertions are sensitive to the
    # global RNG state otherwise (order-dependent flakes, see P3.3).
    import torch

    torch.manual_seed(20260820)
    config = transformers.BertConfig(
        vocab_size=mini_vocab_size(mini_tokenizer_dir),
        hidden_size=32,
        num_hidden_layers=2,
        num_attention_heads=4,
        intermediate_size=64,
        max_position_embeddings=180,
        num_labels=len(labels),
        id2label=dict(enumerate(labels)),
        label2id={label: i for i, label in enumerate(labels)},
    )
    model = transformers.AutoModelForSequenceClassification.from_config(config)
    out = tmp_path_factory.mktemp("ckpt") / "mini-bert"
    model.save_pretrained(out)
    return out


@pytest.fixture()
def mini_jsonl_dataset(tmp_path: Path) -> dict[str, Path]:
    """Synthetic P3.1-schema splits with learnable token-label correlation."""
    label_marker = {
        "politics": "政治",
        "economy": "股市",
        "military": "军演",
        "life": "教育",
        "tech": "芯片",
        "other": "娱乐",
    }
    paths: dict[str, Path] = {}
    for split, count in (("train", 36), ("val", 12), ("test", 12)):
        lines = []
        for i in range(count):
            label = list(label_marker)[i % len(label_marker)]
            marker = label_marker[label]
            record = {
                "id": f"{split}-{i}",
                "title": f"{marker}新闻第{i}号",
                "summary": f"关于{marker}的报道内容 {marker} {split} {i % 10}",
                "label": label,
                "source": "synthetic",
            }
            lines.append(json.dumps(record, ensure_ascii=False))
        path = tmp_path / f"{split}.jsonl"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        paths[split] = path
    return paths
