"""Tests for scripts/preprocess_thucnews.py (PRD task P3.1).

Acceptance: JSONL format is correct and the THUCNews -> app label
mapping has no mistakes. All tests run against synthetic THUCNews
trees in tmp_path; the real dataset (thuctc.github.io) is not required.

Record schema produced by the script (contract for P3.2 training):
    {"id": str, "title": str, "summary": str, "label": str, "source": str}
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "preprocess_thucnews.py"


def _load_script() -> ModuleType:
    """Load scripts/preprocess_thucnews.py as a module (scripts/ is not a package)."""
    spec = importlib.util.spec_from_file_location("preprocess_thucnews", _SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["preprocess_thucnews"] = module
    spec.loader.exec_module(module)
    return module


pp = _load_script()


def _write_news(root: Path, category: str, count: int, *, body_chars: int = 400) -> None:
    """Create ``count`` fake THUCNews txt files under root/category/."""
    folder = root / category
    folder.mkdir(parents=True, exist_ok=True)
    for i in range(count):
        title = f"{category}新闻标题第{i}号"
        body = f"这是正文的开头句子编号{i}。" + ("内容填充文字。" * (body_chars // 7))
        (folder / f"doc_{i}.txt").write_text(f"{title}\n\n{body}\n", encoding="utf-8")


def _make_tree(root: Path) -> None:
    _write_news(root, "时政", 12)
    _write_news(root, "财经", 30)
    _write_news(root, "科技", 12)
    _write_news(root, "娱乐", 12)


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


# ---------------------------------------------------------------- mapping


def test_label_mapping_covers_all_thucnews_categories() -> None:
    expected = {"时政", "财经", "股票", "房产", "科技", "教育", "社会", "体育", "游戏", "娱乐"}
    assert set(pp.LABEL_MAP) == expected
    # Every target label must belong to the app's CATEGORIES (§2.2).
    from core.constants import CATEGORIES

    assert set(pp.LABEL_MAP.values()) <= set(CATEGORIES)
    # military has no THUCNews source (covered by manual annotations later).
    assert "military" not in pp.LABEL_MAP.values()


# ---------------------------------------------------------------- parsing


def test_parse_article_extracts_title_and_summary(tmp_path: Path) -> None:
    file = tmp_path / "a.txt"
    payload = ("标题行\n\n第一段正文甲，描述事件的起因与经过。\n"
        "第二段正文乙，补充各方回应与后续安排。\n")
    file.write_text(payload, encoding="utf-8")
    record = pp.parse_article(file)
    assert record is not None
    assert record["title"] == "标题行"
    assert record["summary"].startswith("第一段正文甲，")
    assert "第二段正文乙，" in record["summary"]
    assert "\n" not in record["summary"]  # whitespace flattened


def test_parse_article_truncates_long_summary(tmp_path: Path) -> None:
    file = tmp_path / "long.txt"
    file.write_text("长文标题\n" + ("长" * 2000) + "\n", encoding="utf-8")
    record = pp.parse_article(file)
    assert record is not None
    assert len(record["summary"]) <= pp.SUMMARY_MAX_CHARS


def test_parse_article_skips_degenerate_files(tmp_path: Path) -> None:
    empty = tmp_path / "empty.txt"
    empty.write_text("", encoding="utf-8")
    title_only = tmp_path / "title_only.txt"
    title_only.write_text("只有标题没有正文\n", encoding="utf-8")
    assert pp.parse_article(empty) is None
    assert pp.parse_article(title_only) is None


# ---------------------------------------------------------------- dataset


def test_build_dataset_jsonl_format_and_labels(tmp_path: Path) -> None:
    data_dir = tmp_path / "thucnews"
    _make_tree(data_dir)
    out_dir = tmp_path / "processed"
    stats = pp.build_dataset(data_dir=data_dir, out_dir=out_dir, total=40, seed=42)
    for split in ("train", "val", "test"):
        records = _read_jsonl(out_dir / f"{split}.jsonl")
        assert records, f"{split}.jsonl must not be empty"
        for record in records:
            assert set(record) == {"id", "title", "summary", "label", "source"}
            assert isinstance(record["id"], str) and record["id"]
            assert isinstance(record["title"], str) and record["title"]
            assert isinstance(record["summary"], str)
            assert record["label"] in pp.LABEL_MAP.values()
            assert record["source"] == "thucnews"
    assert stats["total"] == sum(stats["splits"][s] for s in ("train", "val", "test"))


def test_build_dataset_balances_labels(tmp_path: Path) -> None:
    data_dir = tmp_path / "thucnews"
    _make_tree(data_dir)  # 财经 has 30 docs, others 12 each
    out_dir = tmp_path / "processed"
    pp.build_dataset(data_dir=data_dir, out_dir=out_dir, total=40, seed=42)
    records = (
        _read_jsonl(out_dir / "train.jsonl")
        + _read_jsonl(out_dir / "val.jsonl")
        + _read_jsonl(out_dir / "test.jsonl")
    )
    counts: dict[str, int] = {}
    for record in records:
        counts[str(record["label"])] = counts.get(str(record["label"]), 0) + 1
    # Balanced sampling caps the majority class to the same quota as others.
    assert counts["economy"] <= 10
    assert len(counts) == 4


def test_splits_are_disjoint_and_stratified(tmp_path: Path) -> None:
    data_dir = tmp_path / "thucnews"
    _make_tree(data_dir)
    out_dir = tmp_path / "processed"
    pp.build_dataset(data_dir=data_dir, out_dir=out_dir, total=40, seed=42)
    ids_by_split = {
        split: [str(r["id"]) for r in _read_jsonl(out_dir / f"{split}.jsonl")]
        for split in ("train", "val", "test")
    }
    all_ids = ids_by_split["train"] + ids_by_split["val"] + ids_by_split["test"]
    assert len(all_ids) == len(set(all_ids))  # no leakage across splits
    for split in ids_by_split:
        labels = {str(r["label"]) for r in _read_jsonl(out_dir / f"{split}.jsonl")}
        assert len(labels) >= 2, f"{split} should contain multiple labels"


def test_unknown_category_dirs_are_ignored(tmp_path: Path) -> None:
    data_dir = tmp_path / "thucnews"
    _make_tree(data_dir)
    _write_news(data_dir, "星座", 5)  # not in LABEL_MAP
    out_dir = tmp_path / "processed"
    stats = pp.build_dataset(data_dir=data_dir, out_dir=out_dir, total=40, seed=42)
    assert "星座" not in json.dumps(stats, ensure_ascii=False)
    records = _read_jsonl(out_dir / "train.jsonl")
    assert all(r["label"] in pp.LABEL_MAP.values() for r in records)


def test_deterministic_with_same_seed(tmp_path: Path) -> None:
    data_dir = tmp_path / "thucnews"
    _make_tree(data_dir)
    out_a = tmp_path / "a"
    out_b = tmp_path / "b"
    pp.build_dataset(data_dir=data_dir, out_dir=out_a, total=40, seed=7)
    pp.build_dataset(data_dir=data_dir, out_dir=out_b, total=40, seed=7)
    for split in ("train", "val", "test"):
        assert (out_a / f"{split}.jsonl").read_bytes() == (out_b / f"{split}.jsonl").read_bytes()


def test_extra_jsonl_is_merged_and_validated(tmp_path: Path) -> None:
    data_dir = tmp_path / "thucnews"
    _make_tree(data_dir)
    extra = tmp_path / "manual.jsonl"
    extra.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "id": "m-1",
                        "title": "军演",
                        "summary": "某部举行演习",
                        "label": "military",
                        "source": "manual",
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "id": "m-2",
                        "title": "哨所",
                        "summary": "边防哨所日常",
                        "label": "military",
                        "source": "manual",
                    },
                    ensure_ascii=False,
                ),
            ]
        ),
        encoding="utf-8",
    )
    out_dir = tmp_path / "processed"
    stats = pp.build_dataset(
        data_dir=data_dir, out_dir=out_dir, total=40, seed=42, extra_files=[extra]
    )
    records = (
        _read_jsonl(out_dir / "train.jsonl")
        + _read_jsonl(out_dir / "val.jsonl")
        + _read_jsonl(out_dir / "test.jsonl")
    )
    merged_ids = {str(r["id"]) for r in records}
    assert {"m-1", "m-2"} <= merged_ids
    assert stats["labels"]["military"] == 2


def test_extra_jsonl_rejects_bad_label(tmp_path: Path) -> None:
    data_dir = tmp_path / "thucnews"
    _make_tree(data_dir)
    bad = tmp_path / "bad.jsonl"
    bad.write_text(
        json.dumps({"id": "x", "title": "t", "summary": "s", "label": "sports", "source": "manual"},
                   ensure_ascii=False),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="label"):
        pp.build_dataset(
            data_dir=data_dir, out_dir=tmp_path / "out", total=40, seed=1, extra_files=[bad]
        )


# ---------------------------------------------------------------- CLI


# ---------------------------------------------------------------- cnews


def _write_cnews(path: Path, rows: list[tuple[str, str, str]]) -> None:
    path.write_text(
        "\n".join(f"{cn}\t{title}\t{body}" for cn, title, body in rows) + "\n",
        encoding="utf-8",
    )


def test_load_cnews_maps_and_skips_unknown_classes(tmp_path: Path) -> None:
    path = tmp_path / "cnews.train.txt"
    body = "正文内容足够长，包含有效训练信号。" * 3
    _write_cnews(path, [
        ("时政", "两会召开", body),
        ("财经", "央行降准", body),
        ("家居", "沙发选购指南", body),   # not in LABEL_MAP -> skipped
        ("时政", "两会召开", body),       # duplicate (label,title) -> dropped
        ("时政", "缺正文的标题", ""),     # degenerate -> dropped
    ])
    grouped = pp.load_cnews(path)
    assert set(grouped) == {"politics", "economy"}
    assert [r["title"] for r in grouped["politics"]] == ["两会召开"]
    assert all(r["source"] == "thucnews-cnews" for r in
               grouped["politics"] + grouped["economy"])


def test_build_dataset_with_cnews_and_military_extra(tmp_path: Path) -> None:
    cnews = tmp_path / "cnews.train.txt"
    body = "这是足够长的正文内容，用于训练与测试。" * 5
    rows: list[tuple[str, str, str]] = []
    for i in range(30):
        rows.append(("时政", f"时政新闻第{i}号", body))
        rows.append(("科技", f"科技新闻第{i}号", body))
    _write_cnews(cnews, rows)

    extra = tmp_path / "military.jsonl"
    extra.write_text(
        "\n".join(
            json.dumps({"id": f"mil-{i}", "title": f"军演第{i}号",
                        "summary": "东部战区组织多军兵种联合演习",
                        "label": "military", "source": "manual"},
                       ensure_ascii=False)
            for i in range(6)
        ),
        encoding="utf-8",
    )
    out_dir = tmp_path / "processed"
    stats = pp.build_dataset(
        cnews_file=cnews, out_dir=out_dir, total=20, seed=42,
        extra_files=[extra],
    )
    records = (
        _read_jsonl(out_dir / "train.jsonl")
        + _read_jsonl(out_dir / "val.jsonl")
        + _read_jsonl(out_dir / "test.jsonl")
    )
    counts: dict[str, int] = {}
    for record in records:
        counts[str(record["label"])] = counts.get(str(record["label"]), 0) + 1
    assert counts["military"] == 6          # extras merged whole
    assert counts["politics"] == 10         # balanced base pool
    assert counts["tech"] == 10
    assert stats["sources"] == {"manual": 6, "thucnews-cnews": 20}


def test_main_cli_requires_a_corpus_source(tmp_path: Path) -> None:
    code = pp.main(["--out-dir", str(tmp_path / "out"), "--total", "40"])
    assert code == 1


def test_main_cli_end_to_end(tmp_path: Path) -> None:
    data_dir = tmp_path / "thucnews"
    _make_tree(data_dir)
    out_dir = tmp_path / "processed"
    code = pp.main(
        [
            "--data-dir", str(data_dir),
            "--out-dir", str(out_dir),
            "--total", "40",
            "--seed", "42",
        ]
    )
    assert code == 0
    for name in ("train.jsonl", "val.jsonl", "test.jsonl", "stats.json"):
        assert (out_dir / name).is_file()
    stats = json.loads((out_dir / "stats.json").read_text(encoding="utf-8"))
    assert stats["total"] == 40 + 0  # no extras merged
    assert stats["seed"] == 42
