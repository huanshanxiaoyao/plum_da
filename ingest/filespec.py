"""机器 A 落盘文件契约的**唯一解析点**（设计文档 §3.0 / §4.1）。

上游 `app/platform/analytics/sinks.py` 决定这些形状，本模块是它在机器 B 侧的镜像。
两边都不 import 对方——契约是文件，不是代码。所以这里的每条常量都必须能在上游找到出处，
改动上游 sink 的命名或 manifest 字段时，本文件是唯一需要跟着改的地方。

目录形态：

    <root>/business_day=YYYY-MM-DD/plum-<day>T<HH>-<writer>-<NNNN>.ndjson.gz
    <root>/business_day=YYYY-MM-DD/plum-<day>T<HH>-<writer>-<NNNN>.ndjson.gz.manifest
    <root>/_dead/YYYY-MM-DD/plum-dead-<day>T<HH>-<writer>-<NNNN>.ndjson.gz[.manifest]
    <root>/.writer.lock                                   # 机器 A 的目录锁，永不拉取

**manifest 存在 ≡ 该数据文件已封盘、可安全拉取。** 它由上游在关闭切片时用
`os.replace` 原子出现，所以「有 manifest」是一个可信信号；没有 manifest 的文件是
上游正在写的活切片，拉走会得到半截 gzip。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Iterator, Optional

DATA_SUFFIX = ".ndjson.gz"
MANIFEST_SUFFIX = ".manifest"
DEAD_DIRNAME = "_dead"
LOCK_FILENAME = ".writer.lock"
PARTITION_PREFIX = "business_day="

LANE_DATA = "data"
LANE_DEAD = "dead"

# plum-2026-09-07T01-5c37-0000.ndjson.gz / plum-dead-2026-09-07T01-5c37-0000.ndjson.gz
_NAME_RE = re.compile(
    r"^plum(?P<dead>-dead)?-(?P<day>\d{4}-\d{2}-\d{2})T(?P<hour>\d{2})"
    r"-(?P<writer>[0-9a-f]+)-(?P<index>\d{4})\.ndjson\.gz$"
)


class ContractError(ValueError):
    """文件不符合契约。调用方应把这类文件隔离，而不是猜测它的含义。"""


@dataclass(frozen=True)
class Manifest:
    """封盘回执。`line_count` 与 `sha256` 是装载前校验的全部依据。"""

    line_count: int
    sha256: str
    first_ts: Optional[str]
    last_ts: Optional[str]
    closed_at: Optional[str]


@dataclass(frozen=True)
class SliceFile:
    """一个已封盘的切片：数据文件 + 它的 manifest。"""

    path: Path
    manifest_path: Path
    lane: str
    business_day: date
    writer_id: str
    index: int

    @property
    def key(self) -> str:
        """台账主键：相对 root 的路径。

        用路径而不是文件名：`_dead` 与主通道的文件名可能只差前缀，而分区目录本身
        是契约的一部分，带上它才能保证跨通道、跨天全局唯一。
        """

        return f"{self.lane}/{self.business_day.isoformat()}/{self.path.name}"


def parse_slice_name(name: str) -> tuple[str, date, str, int]:
    """从文件名解析 (lane, business_day, writer_id, index)。不合契约就抛。"""

    match = _NAME_RE.match(name)
    if match is None:
        raise ContractError(f"文件名不符合契约: {name}")
    lane = LANE_DEAD if match.group("dead") else LANE_DATA
    return (
        lane,
        date.fromisoformat(match.group("day")),
        match.group("writer"),
        int(match.group("index")),
    )


def read_manifest(path: Path) -> Manifest:
    """读 manifest。字段缺失即视为不合契约——半个 manifest 比没有更危险。"""

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as err:
        raise ContractError(f"manifest 不可读: {path} ({err})") from err
    if not isinstance(raw, dict):
        raise ContractError(f"manifest 不是对象: {path}")
    try:
        line_count = int(raw["line_count"])
        sha256 = str(raw["sha256"])
    except (KeyError, TypeError, ValueError) as err:
        raise ContractError(f"manifest 缺少 line_count/sha256: {path}") from err
    if not sha256:
        raise ContractError(f"manifest 的 sha256 为空: {path}")
    return Manifest(
        line_count=line_count,
        sha256=sha256,
        first_ts=raw.get("first_ts"),
        last_ts=raw.get("last_ts"),
        closed_at=raw.get("closed_at"),
    )


def iter_sealed_slices(root: Path) -> Iterator[SliceFile]:
    """遍历 root 下所有**已封盘**的切片，按 (业务日, 序号) 稳定排序。

    活切片（无 manifest）、目录锁与任何不合契约的文件都跳过而不是报错：机器 A 一直在写，
    「此刻还没封盘」是常态而非异常。真正的异常是 manifest 存在但内容坏掉，那由
    `read_manifest` 在装载时抛。
    """

    found: list[SliceFile] = []
    for manifest_path in root.rglob("*" + DATA_SUFFIX + MANIFEST_SUFFIX):
        data_path = manifest_path.with_name(manifest_path.name[: -len(MANIFEST_SUFFIX)])
        if not data_path.exists():
            continue
        try:
            lane, business_day, writer_id, index = parse_slice_name(data_path.name)
        except ContractError:
            continue
        found.append(
            SliceFile(
                path=data_path,
                manifest_path=manifest_path,
                lane=lane,
                business_day=business_day,
                writer_id=writer_id,
                index=index,
            )
        )
    found.sort(key=lambda item: (item.business_day, item.lane, item.writer_id, item.index))
    yield from found


def sha256_of(path: Path) -> str:
    """流式算 sha256。切片可能有 64 MB，不整包读进内存。"""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
