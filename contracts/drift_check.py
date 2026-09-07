"""对比本仓钉住的字典副本与上游真源，**只告警，绝不阻断**。

单向依赖（设计文档 D9）：真源永远是 `ai4all_bridge` 的 `tracking/plum_events.yaml`。
这边持有的是钉住的副本，用来解释历史数据——**旧数据是按旧字典写的**，
副本自动跟随上游反而会让历史解释漂移。

为什么不阻断：数仓的构建门禁挡不住上游合并，只会让数仓在上游改字典后无法发布，
既不能阻止漂移，又把两个仓库的发布节奏绑死。漂移的正确响应是人来看一眼、
决定要不要跟进并重新钉版本。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any, Dict, List

import yaml

PINNED_DICT = Path(__file__).resolve().parent / "plum_events.yaml"
UPSTREAM_ENV = "PLUM_DA_UPSTREAM_DICT"


def _load(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: 字典根节点不是映射")
    return data


def _events(doc: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """把 `events:` 列表按 name 索引成映射。

    真源里 events 是**列表**（顺序即文档顺序）。这里必须显式转换：
    早先按映射直接 `.get()` 的写法会拿到空字典，于是任何漂移都报「无漂移」——
    一个永远通过的检查比没有检查更危险。
    """

    raw = doc.get("events")
    if not isinstance(raw, list):
        raise ValueError("字典的 events 不是列表，格式与预期不符")
    indexed: Dict[str, Dict[str, Any]] = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if name:
            indexed[name] = item
    return indexed


def diff(pinned: Dict[str, Any], upstream: Dict[str, Any]) -> List[str]:
    """列出钉住副本与上游的差异，人话描述，每条一行。"""

    notes: List[str] = []
    # 根键是 `version`（信封里的 `dict_version` 是它的取值，不是这里的键名）。
    pin_ver = str(pinned.get("version", "?"))
    up_ver = str(upstream.get("version", "?"))
    if pin_ver != up_ver:
        notes.append(f"字典 version: 钉住 {pin_ver} → 上游 {up_ver}")

    pin_events = _events(pinned)
    up_events = _events(upstream)

    for name in sorted(set(up_events) - set(pin_events)):
        notes.append(f"新增事件: {name}（数仓尚未消费）")
    for name in sorted(set(pin_events) - set(up_events)):
        # 上游删事件是**破坏性**的：历史数据里还有这些行，DWD 若依赖它会静默变空。
        notes.append(f"上游已删除事件: {name}（历史数据仍存在，检查下游依赖）")

    for name in sorted(set(pin_events) & set(up_events)):
        pin_status = str((pin_events[name] or {}).get("status") or "")
        up_status = str((up_events[name] or {}).get("status") or "")
        if pin_status != up_status:
            notes.append(f"{name}: status {pin_status} → {up_status}")

        pin_props = (pin_events[name] or {}).get("props") or {}
        up_props = (up_events[name] or {}).get("props") or {}
        if not isinstance(pin_props, dict) or not isinstance(up_props, dict):
            continue
        for field in sorted(set(up_props) - set(pin_props)):
            notes.append(f"{name}: 新增字段 {field}")
        for field in sorted(set(pin_props) - set(up_props)):
            notes.append(f"{name}: 上游已删除字段 {field}（历史数据仍存在）")
        for field in sorted(set(pin_props) & set(up_props)):
            pin_type = (pin_props[field] or {}).get("type")
            up_type = (up_props[field] or {}).get("type")
            if pin_type != up_type:
                notes.append(f"{name}.{field}: 类型 {pin_type} → {up_type}")
            # required 的变化必须看：上游把可选收紧成必填，意味着历史数据里这个字段
            # 有大量 NULL 而新数据没有——按新口径写的下游查询会在历史区间上悄悄少算。
            pin_req = bool((pin_props[field] or {}).get("required", False))
            up_req = bool((up_props[field] or {}).get("required", False))
            if pin_req != up_req:
                notes.append(
                    f"{name}.{field}: required {pin_req} → {up_req}"
                    f"（历史数据按 required={pin_req} 写入）"
                )
            # enum 去掉已有取值同样是破坏性的：历史行里还带着那个值，
            # 下游若按新取值集合做 CASE/JOIN，旧行会落到 else 分支里被静默归错类。
            pin_values = set((pin_props[field] or {}).get("values") or [])
            up_values = set((up_props[field] or {}).get("values") or [])
            for value in sorted(pin_values - up_values):
                notes.append(f"{name}.{field}: 上游已删除 enum 取值 {value}（历史数据仍存在）")
            for value in sorted(up_values - pin_values):
                notes.append(f"{name}.{field}: 新增 enum 取值 {value}")
    return notes


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="字典漂移检查（只告警）")
    parser.add_argument(
        "--upstream",
        default=os.environ.get(UPSTREAM_ENV, ""),
        help="上游 tracking/plum_events.yaml 路径",
    )
    parser.add_argument(
        "--strict", action="store_true", help="有漂移时返回 1（默认 0，不阻断构建）"
    )
    args = parser.parse_args(argv)

    if not args.upstream:
        print(f"跳过漂移检查：未提供 --upstream，也没有 {UPSTREAM_ENV}")
        return 0
    upstream_path = Path(args.upstream)
    if not upstream_path.exists():
        print(f"跳过漂移检查：上游字典不存在 {upstream_path}")
        return 0

    notes = diff(_load(PINNED_DICT), _load(upstream_path))
    if not notes:
        print("字典无漂移。")
        return 0
    print(f"⚠️ 字典漂移 {len(notes)} 处（不阻断；确认后更新 contracts/PINNED 重新钉版本）:")
    for note in notes:
        print(f"  - {note}")
    return 1 if args.strict else 0


if __name__ == "__main__":
    sys.exit(main())
