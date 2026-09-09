"""从机器 A 拉取已封口的切片文件（C1 通道，尽力而为）。

用 rsync 而不是自己写传输：rsync 默认写临时名、传完才 rename，
**局部传输永远不会以最终文件名出现**——这正好和「manifest 在场即完整」这条约定叠成两层。

先扫描 manifest，再按明确清单拉取数据及其 manifest，不传输活切片。
"""

from __future__ import annotations

import logging
import os
import shlex
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import List, Optional

from .filespec import DATA_SUFFIX, LOCK_FILENAME, MANIFEST_SUFFIX

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
        ssh += f" -o IdentitiesOnly=yes -i {shlex.quote(ssh_key)}"
    return [
        "rsync",
        "-az",
        "--no-links",
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

    # 每轮重新扫描，不能使用本地残留 manifest 作为远端已封盘的证据。
    with tempfile.TemporaryDirectory(prefix="plum-da-manifests-") as scan_dir:
        scan_root = Path(scan_dir)
        scan_cmd = build_command(remote, scan_root, ssh_key)
        scan_cmd[1:1] = [
            "--include=*/",
            f"--include=*{DATA_SUFFIX}{MANIFEST_SUFFIX}",
            "--exclude=*",
            "--prune-empty-dirs",
        ]
        logger.info("扫描远端 manifest")
        scan = subprocess.run(
            scan_cmd, capture_output=True, text=True, timeout=timeout, check=False
        )
        if scan.returncode != 0:
            logger.error("manifest 扫描失败 %s: %s", scan.returncode, scan.stderr.strip()[:500])
            return PullResult(scan.returncode, scan.stdout, scan.stderr)

        files: List[str] = []
        for manifest in sorted(scan_root.rglob(f"*{DATA_SUFFIX}{MANIFEST_SUFFIX}")):
            if not manifest.is_file() or manifest.is_symlink():
                continue
            relative = manifest.relative_to(scan_root).as_posix()
            files.extend([relative[: -len(MANIFEST_SUFFIX)], relative])
        logger.info("远端已封盘切片: %d", len(files) // 2)
        if not files:
            return PullResult(0, "", "")

        cmd = build_command(remote, landing, ssh_key)
        # --files-from 下 -a 不隐含递归；清单只含文件，不能扩大为整个日期目录。
        cmd[1:1] = ["--files-from=-", "--from0"]
        proc = subprocess.run(
            cmd, input="\0".join(files) + "\0", capture_output=True,
            text=True, timeout=timeout, check=False,
        )
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
