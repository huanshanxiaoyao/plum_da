"""入库心跳：把「上一次完整跑完」这个事实写在本机磁盘上。

**为什么是文件，不是库里一行。** PG 连不上正是最该被抓到的故障之一。心跳写进库意味着
最需要报警的时刻心跳也写不进去，看门读到的只是一个不再更新的旧值——和「刚跑过」在
数据形态上无法区分。文件写在本机盘上，与分析库的可用性无关。

**它和台账水位不是同一个信号，别拿一个替另一个：**

| 信号 | 变旧说明什么 |
| --- | --- |
| 心跳（本文件） | 入库**进程**没跑完。service 挂了、PG 连不上、凭据失效、盘满、venv 坏了 |
| `max(loaded_at)` | 没有**新数据**进库。可能是 B 坏了，**也可能只是机器 A 那段时间没流量** |

台账水位分不清后两者，所以它的阈值必须宽得多（见 `run_watchdog.py`）。心跳分得清：
即使一个文件都没拉到，只要跑完了就更新。
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

STATE_DIR_ENV = "PLUM_DA_STATE_DIR"
FILENAME = "last_ingest_success.json"

#: 落地目录下的状态子目录名。以 `.` 开头且不是日期形态，
#: 因此 `prune_landing()` 的日期解析扫不到它，不会被当成过期分区删掉。
DEFAULT_SUBDIR = ".state"


def state_dir(landing: Optional[Path] = None) -> Path:
    """状态目录：优先取环境变量，否则落在落地目录下的 `.state/`。

    默认值挂在落地目录下是为了让部署少一步——那个目录已经存在、已经归 `plum_da`。
    状态与数据分开放更干净，但多一个需要 root 创建的目录就多一个装机时会漏的步骤，
    而漏掉的表现是看门静默不工作。
    """

    override = os.environ.get(STATE_DIR_ENV, "").strip()
    if override:
        return Path(override)
    if landing is None:
        raise RuntimeError(f"未设置 {STATE_DIR_ENV}，且没有提供落地目录作为回退")
    return Path(landing) / DEFAULT_SUBDIR


def write_success(directory: Path, report: Dict[str, Any]) -> Path:
    """记下一次成功跑完。只在**整轮无异常结束**时调用。

    装载失败（`run_ingest.py` 以非 0 退出）时**不写**：那一轮没跑成，心跳不该变新。
    先写临时文件再 `os.replace` —— 写到一半被杀掉会留下半个 JSON，
    而看门解析不了它时只能当成「没有心跳」，等于把一次崩溃放大成一条误报。
    """

    directory.mkdir(parents=True, exist_ok=True)
    payload = {
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "sealed_slices": report.get("sealed_slices"),
        "rows_loaded": (report.get("load") or {}).get("rows_loaded")
        if isinstance(report.get("load"), dict)
        else None,
        "pull_returncode": (report.get("pull") or {}).get("returncode")
        if isinstance(report.get("pull"), dict)
        else None,
    }
    target = directory / FILENAME
    tmp = directory / f"{FILENAME}.tmp"
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, target)
    return target


def read(directory: Path) -> Optional[Dict[str, Any]]:
    """读回心跳。文件不存在或内容不可解析都返回 None——两者对看门是同一件事。"""

    target = Path(directory) / FILENAME
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def finished_at(payload: Optional[Dict[str, Any]]) -> Optional[datetime]:
    """从心跳内容里取完成时刻。

    取内容里的时间戳而不是文件 mtime：mtime 会被备份、rsync、`touch` 改写，
    那些操作都不代表入库真的跑过。
    """

    if not payload:
        return None
    raw = payload.get("finished_at")
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
