"""从机器 A 拉取已封口的切片文件（C1 通道，尽力而为）。

用 rsync 而不是自己写传输：rsync 默认写临时名、传完才 rename，
**局部传输永远不会以最终文件名出现**——这正好和「manifest 在场即完整」这条约定叠成两层。

拉取端不做任何过滤逻辑：拉全量目录，由装载端按「有没有 manifest」决定哪些能用。
把「文件是否完整」的判断集中在一处，是为了不让传输层和装载层各持一套标准。
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import List, Optional

from .filespec import LOCK_FILENAME

logger = logging.getLogger("plum_da.pull")

REMOTE_ENV = "PLUM_DA_REMOTE"  # 形如 user@host:/var/lib/plum/analytics
LANDING_ENV = "PLUM_DA_LANDING"  # 本地落地目录
SSH_KEY_ENV = "PLUM_DA_SSH_KEY"

DEFAULT_LANDING_RETENTION_DAYS = 7


@dataclass(frozen=True)
class PullResult:
    """一次拉取的结果。`returncode` 非 0 表示 rsync 失败，文件可能只拉到一部分。"""

    returncode: int
    stdout: str
    stderr: str


def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} 未设置")
    return value


def build_command(remote: str, landing: Path, ssh_key: Optional[str] = None) -> List[str]:
    """拼出 rsync 命令。抽成纯函数，便于在没有网络的测试里断言参数。"""

    ssh = "ssh -o BatchMode=yes -o StrictHostKeyChecking=yes"
    if ssh_key:
        ssh += f" -i {ssh_key}"
    return [
        "rsync",
        "-az",
        "--partial",
        # 写锁是机器 A 的进程内状态，拉过来没有意义还会造成误解。
        f"--exclude={LOCK_FILENAME}",
        # 只增不删：机器 A 过了保留期删文件，不该连带删掉这边还没装的落地副本。
        "-e",
        ssh,
        f"{remote.rstrip('/')}/",
        f"{str(landing).rstrip('/')}/",
    ]


def pull(
    remote: Optional[str] = None,
    landing: Optional[Path] = None,
    ssh_key: Optional[str] = None,
    timeout: int = 900,
) -> PullResult:
    """执行一次拉取。失败**不抛异常**，由调用方决定是否继续装载已有文件。

    理由：上一批文件已经在本地了，网络断掉不该让这轮装载也一起停。
    """

    remote = remote or _require_env(REMOTE_ENV)
    landing = landing or Path(_require_env(LANDING_ENV))
    landing.mkdir(parents=True, exist_ok=True)
    ssh_key = ssh_key or os.environ.get(SSH_KEY_ENV) or None

    if shutil.which("rsync") is None:
        raise RuntimeError("PATH 上没有 rsync")

    cmd = build_command(remote, landing, ssh_key)
    logger.info("pull: %s", " ".join(cmd))
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    if proc.returncode != 0:
        logger.error("rsync 退出码 %s: %s", proc.returncode, proc.stderr.strip()[:500])
    return PullResult(proc.returncode, proc.stdout, proc.stderr)


def prune_landing(
    landing: Path, today: date, retention_days: int = DEFAULT_LANDING_RETENTION_DAYS
) -> List[str]:
    """清掉落地目录里过期的分区目录，返回被删的目录名。

    落地副本只是重放窗口，不是归档；长期归档在机器 A。留 7 天足够覆盖
    「装载出错到人来处理」的间隔。
    """

    cutoff = today - timedelta(days=retention_days)
    removed: List[str] = []
    for directory in sorted(landing.rglob("*")):
        if not directory.is_dir():
            continue
        name = directory.name
        raw = name.split("=", 1)[1] if name.startswith("business_day=") else name
        try:
            day = date.fromisoformat(raw)
        except ValueError:
            continue
        if day >= cutoff:
            continue
        shutil.rmtree(directory, ignore_errors=True)
        removed.append(str(directory.relative_to(landing)))
        logger.info("清理过期落地目录 %s", directory)
    return removed
