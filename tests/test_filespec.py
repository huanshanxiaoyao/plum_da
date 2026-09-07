"""文件契约解析。这是跨仓库唯一的接口，解析错了两边都不会报错，只会静默丢数据。"""

from __future__ import annotations

import gzip
import hashlib
import json
from datetime import date
from pathlib import Path

import pytest

from ingest.filespec import (
    ContractError,
    iter_sealed_slices,
    parse_slice_name,
    read_manifest,
    sha256_of,
)


def _write_slice(root: Path, name: str, lines: list, *, partition: str, manifest=True) -> Path:
    directory = root / partition
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    raw = "".join(json.dumps(line, ensure_ascii=False) + "\n" for line in lines).encode("utf-8")
    with gzip.open(path, "wb") as handle:
        handle.write(raw)
    if manifest:
        (directory / f"{name}.manifest").write_text(
            json.dumps(
                {
                    "line_count": len(lines),
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "first_ts": None,
                    "last_ts": None,
                    "closed_at": "2026-09-07T02:00:00Z",
                }
            ),
            encoding="utf-8",
        )
    return path


def test_parse_data_and_dead_names():
    data = parse_slice_name("plum-2026-09-07T01-f71cf8-0000.ndjson.gz")
    assert data == ("data", date(2026, 9, 7), "f71cf8", 0)
    dead = parse_slice_name("plum-dead-2026-09-07T01-f71cf8-0003.ndjson.gz")
    assert dead == ("dead", date(2026, 9, 7), "f71cf8", 3)


@pytest.mark.parametrize(
    "name",
    [
        "plum-2026-09-07T01-f71cf8-0000.ndjson",  # 没压缩
        "other-2026-09-07T01-f71cf8-0000.ndjson.gz",  # 不是我们的前缀
        "plum-2026-9-7T01-f71cf8-0000.ndjson.gz",  # 日期没补零
        "plum-2026-09-07T01-f71cf8-0.ndjson.gz",  # 序号位数不对
    ],
)
def test_rejects_non_contract_names(name):
    # 解析层「不认识就抛」，遍历层才做静默跳过：让「这个文件我读不懂」和
    # 「这个目录里有别的文件」是两件事，前者必须响，后者不该响。
    with pytest.raises(ContractError):
        parse_slice_name(name)


def test_iter_only_returns_sealed_slices(tmp_path):
    """manifest 在场 ≡ 文件已封口。没有 manifest 的切片正在被写，绝不能拉。"""

    _write_slice(
        tmp_path,
        "plum-2026-09-07T01-aaaaaa-0000.ndjson.gz",
        [{"a": 1}],
        partition="business_day=2026-09-07",
    )
    _write_slice(
        tmp_path,
        "plum-2026-09-07T02-aaaaaa-0001.ndjson.gz",
        [{"a": 2}],
        partition="business_day=2026-09-07",
        manifest=False,
    )
    _write_slice(
        tmp_path,
        "plum-dead-2026-09-07T01-aaaaaa-0002.ndjson.gz",
        [{"r": 1}],
        partition="_dead/2026-09-07",
    )
    (tmp_path / ".writer.lock").write_text("1", encoding="utf-8")

    found = list(iter_sealed_slices(tmp_path))
    assert [s.index for s in found] == [0, 2]
    assert {s.lane for s in found} == {"data", "dead"}
    assert found[0].key == "data/2026-09-07/plum-2026-09-07T01-aaaaaa-0000.ndjson.gz"


def test_manifest_sha_matches_file(tmp_path):
    path = _write_slice(
        tmp_path,
        "plum-2026-09-07T01-aaaaaa-0000.ndjson.gz",
        [{"a": 1}],
        partition="business_day=2026-09-07",
    )
    manifest = read_manifest(Path(f"{path}.manifest"))
    assert manifest.sha256 == sha256_of(path)
    assert manifest.line_count == 1


def test_broken_manifest_is_a_contract_error(tmp_path):
    directory = tmp_path / "business_day=2026-09-07"
    directory.mkdir(parents=True)
    broken = directory / "plum-2026-09-07T01-aaaaaa-0000.ndjson.gz.manifest"
    broken.write_text("{not json", encoding="utf-8")
    with pytest.raises(ContractError):
        read_manifest(broken)
