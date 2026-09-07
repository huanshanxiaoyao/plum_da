#!/usr/bin/env python3
"""机器 B 的入库入口：迁移 → 建分区 → 拉取 → 装载 → 清理。

设计成**一条命令跑完全流程**且可重复执行：cron 每小时跑一次即可，
不需要外部编排器记住上次跑到哪——进度全在台账和 manifest 里。

    PLUM_DA_DATABASE_URL=... PLUM_DA_REMOTE=user@a:/var/lib/plum/analytics \\
    PLUM_DA_LANDING=/srv/plum_landing ./run_ingest.py

单步调试：--skip-pull / --only migrate|partitions|pull|load|prune
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ingest import db, pull as pull_mod  # noqa: E402
from ingest.filespec import iter_sealed_slices  # noqa: E402
from ingest.load_ods import load_pending  # noqa: E402
from migrations import apply_migrations  # noqa: E402
from warehouse import partitions as partitions_mod  # noqa: E402

logger = logging.getLogger("plum_da.run")

STAGES = ("migrate", "partitions", "pull", "load", "prune")


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Plum 数仓入库")
    parser.add_argument("--only", choices=STAGES, action="append", help="只跑指定阶段，可重复")
    parser.add_argument("--skip-pull", action="store_true", help="不拉取，只装载本地已有文件")
    parser.add_argument("--landing", default=os.environ.get(pull_mod.LANDING_ENV, ""))
    parser.add_argument("--dry-run", action="store_true", help="只列出待装载文件，不写库")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    stages = set(args.only or STAGES)
    if args.skip_pull or args.dry_run:
        # dry-run 不该有任何副作用，拉取会往磁盘写文件，也算副作用。
        stages.discard("pull")
    if not args.landing:
        raise SystemExit(f"缺少落地目录：--landing 或 {pull_mod.LANDING_ENV}")
    landing = Path(args.landing)
    today = datetime.now(timezone.utc).date()

    report: Dict[str, Any] = {"today": today.isoformat()}

    if "pull" in stages:
        result = pull_mod.pull(landing=landing)
        report["pull"] = {"returncode": result.returncode}
        # 拉取失败不中断：本地已有的文件照装不误，否则一次网络抖动会让当天全部停摆。
        if result.returncode != 0:
            report["pull"]["stderr"] = result.stderr.strip()[:500]

    slices = list(iter_sealed_slices(landing)) if landing.exists() else []
    report["sealed_slices"] = len(slices)

    if args.dry_run:
        report["pending_preview"] = [s.key for s in slices[:20]]
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    needs_db = bool(stages & {"migrate", "partitions", "load"})
    if needs_db:
        conn = db.connect()
        try:
            if "migrate" in stages:
                report["migrations_applied"] = apply_migrations(conn)
            if "partitions" in stages:
                report["partitions_ensured"] = len(partitions_mod.ensure_partitions(conn, today))
                report["partitions_dropped"] = partitions_mod.drop_expired_partitions(conn, today)
            if "load" in stages:
                report["load"] = load_pending(conn, slices)
        finally:
            conn.close()

    if "prune" in stages and landing.exists():
        report["landing_pruned"] = pull_mod.prune_landing(landing, today)

    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    failed = report.get("load", {}).get("failed") if isinstance(report.get("load"), dict) else None
    # 有文件装载失败时以非 0 退出，让 cron 的失败告警能抓到；已成功的部分不回滚。
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
