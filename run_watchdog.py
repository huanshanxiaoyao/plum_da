#!/usr/bin/env python3
"""入库滞后看门：在「B 停拉」变成永久数据丢失之前把它喊出来。

**为什么需要它。** `plum-da-ingest.service` 是 `oneshot`，失败只以非 0 退出、由
systemd 记在 journal 里，**没有任何人会收到通知**；而机器 A 的落盘只保留 30 天。
两者相乘：B 停拉超过 30 天，那几天的事件就永久没了，且现象和「那几天没流量」
完全一样——没有报错，只有一段安静的空洞。

**两个信号，两种含义，两个阈值。**

| 信号 | 变旧说明什么 | 阈值 |
| --- | --- | --- |
| 心跳文件 | 入库**进程**没跑完：service/timer 挂了、PG 连不上、rsync 凭据失效、盘满 | 6 小时 |
| `max(loaded_at)` | 没有**新数据**进库：可能 B 坏了，**也可能只是 A 那段时间没流量** | 26 小时 |

台账水位分不清后两种情况，所以它的窗口必须跨过一整个日夜低谷。海外线深夜本来就
可能几小时没有事件，把它按 6 小时报警会在每个凌晨误报一次；而「误报几次之后没人
再看这个频道」比没有告警更糟——它同时废掉了心跳那条真信号。

**方向是 B 自查，不是 A 反查 B。** 机器 A 是线上业务机，让它去探测数仓机等于给业务
机加一条指向数仓的依赖，方向反了。

**它覆盖不了什么（已知缺口，别以为装了就万无一失）：** 机器 B 整机宕机、或
watchdog 自己的 timer 没起来时，本机的一切检查都不会执行，表现同样是「一片安静」。
本机看门无法自证存活，这需要一个外部的 dead-man switch。当前阶段的替代手段是
`--ping`：每天主动播报一次「我还活着」，由人注意到它的缺席（见部署文档第 9 节）。

    ./watchdog.py            # 检查；有异常则告警并以 1 退出
    ./watchdog.py --ping     # 无论是否异常都播报一次（给每日定时用）
    ./watchdog.py --dry-run  # 只打印，不发送
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ingest import db, heartbeat, pull as pull_mod  # noqa: E402
from migrations import SCHEMA  # noqa: E402

logger = logging.getLogger("plum_da.watchdog")

WEBHOOK_ENV = "PLUM_DA_ALERT_WEBHOOK"
HEARTBEAT_HOURS_ENV = "PLUM_DA_WATCHDOG_HEARTBEAT_HOURS"
LEDGER_HOURS_ENV = "PLUM_DA_WATCHDOG_LEDGER_HOURS"
COOLDOWN_HOURS_ENV = "PLUM_DA_WATCHDOG_COOLDOWN_HOURS"

DEFAULT_HEARTBEAT_HOURS = 6.0
DEFAULT_LEDGER_HOURS = 26.0
#: 同一类异常的再告警间隔。入库停三天而看门每小时跑一次，不去重就是 72 条同样的
#: 消息——被刷屏的频道会被静音，静音的告警等于没有告警。
DEFAULT_COOLDOWN_HOURS = 6.0

ALERT_STATE_FILENAME = "watchdog_alerts.json"


@dataclass(frozen=True)
class Alert:
    """一条待发告警。`kind` 用于去重，同类在冷却期内只发一次。"""

    kind: str
    text: str


def _hours(env_name: str, default: float) -> float:
    """读一个以小时为单位的阈值；写错了用默认值并留痕，不让看门因为配置错而不跑。"""

    raw = os.environ.get(env_name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        logger.warning("%s=%r 不是数字，取默认值 %s", env_name, raw, default)
        return default
    if value <= 0:
        logger.warning("%s=%s 非正数，取默认值 %s", env_name, value, default)
        return default
    return value


def _fmt_age(delta: Optional[timedelta]) -> str:
    """把时长写成人看得懂的形式。告警文案里「38.2 小时」比「137520 秒」有用。"""

    if delta is None:
        return "未知"
    hours = delta.total_seconds() / 3600
    if hours < 1:
        return f"{delta.total_seconds() / 60:.0f} 分钟"
    if hours < 48:
        return f"{hours:.1f} 小时"
    return f"{hours / 24:.1f} 天"


def evaluate(
    now: datetime,
    heartbeat_at: Optional[datetime],
    ledger_at: Optional[datetime],
    db_error: Optional[str] = None,
    heartbeat_hours: float = DEFAULT_HEARTBEAT_HOURS,
    ledger_hours: float = DEFAULT_LEDGER_HOURS,
) -> List[Alert]:
    """纯判定：给定三个观测值，算出该报哪些警。不做 IO，便于测试。

    `heartbeat_at` / `ledger_at` 为 None 表示「读不到」，与「很旧」分开报——
    两者的处置动作不同：前者多半是没装好或从没成功跑过，后者是跑过但停了。
    """

    alerts: List[Alert] = []

    if db_error:
        # 库读不到本身就是最高优先的异常：入库这时候一定也在失败，而且台账水位
        # 这个信号此刻是读不到的，不能因为读不到就当作「没问题」。文案说「读不到」
        # 而不是「连不上」：迁移没跑导致的 UndefinedTable 也走这条路，
        # 一口咬定是网络会把人引向错误的排查方向。
        alerts.append(
            Alert(
                kind="db_unreachable",
                text=f"【Plum 数仓】读不到分析库台账，入库必然已停。\n错误：{db_error}",
            )
        )

    if heartbeat_at is None:
        alerts.append(
            Alert(
                kind="heartbeat_missing",
                text=(
                    "【Plum 数仓】读不到入库心跳：从安装以来没有任何一轮完整跑成功过。\n"
                    "排查：systemctl status plum-da-ingest.service；"
                    "确认 PLUM_DA_STATE_DIR / 落地目录权限。"
                ),
            )
        )
    else:
        age = now - heartbeat_at
        if age > timedelta(hours=heartbeat_hours):
            alerts.append(
                Alert(
                    kind="heartbeat_stale",
                    text=(
                        f"【Plum 数仓】入库已 {_fmt_age(age)} 没有成功跑完"
                        f"（阈值 {heartbeat_hours:g} 小时）。\n"
                        "含义：进程侧出了问题——timer 没触发、PG 连不上、"
                        "rsync 凭据失效或盘满，不是「没有流量」。\n"
                        "排查：journalctl -u plum-da-ingest.service -n 50"
                    ),
                )
            )

    if not db_error:
        if ledger_at is None:
            alerts.append(
                Alert(
                    kind="ledger_empty",
                    text=(
                        "【Plum 数仓】入库台账为空：一个文件都没装载过。\n"
                        "若刚装完且机器 A 尚未开启采集，这是预期的；"
                        "否则检查 rsync 拉取路径两侧是否逐字符一致。"
                    ),
                )
            )
        else:
            age = now - ledger_at
            if age > timedelta(hours=ledger_hours):
                alerts.append(
                    Alert(
                        kind="ledger_stale",
                        text=(
                            f"【Plum 数仓】已 {_fmt_age(age)} 没有新数据入库"
                            f"（阈值 {ledger_hours:g} 小时）。\n"
                            "注意：这个信号**分不清**「B 拉取坏了」和「A 真的没有流量」。"
                            "若上面没有同时报心跳异常，优先怀疑机器 A 侧采集停了。\n"
                            "机器 A 只保留 30 天，超过就永久丢失——请在窗口内确认。"
                        ),
                    )
                )

    return alerts


def read_alert_state(directory: Path) -> Dict[str, str]:
    """读回各类告警的上次发送时刻。读不到就当作从没发过。"""

    try:
        payload = json.loads((Path(directory) / ALERT_STATE_FILENAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def write_alert_state(directory: Path, state: Dict[str, str]) -> None:
    """记下本轮的发送时刻，供下一轮做冷却判断。"""

    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    tmp = directory / f"{ALERT_STATE_FILENAME}.tmp"
    tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, directory / ALERT_STATE_FILENAME)


def filter_cooldown(
    alerts: List[Alert],
    state: Dict[str, str],
    now: datetime,
    cooldown_hours: float = DEFAULT_COOLDOWN_HOURS,
) -> List[Alert]:
    """滤掉冷却期内的同类告警。返回的是「本轮真要发出去的」那些。"""

    kept: List[Alert] = []
    for alert in alerts:
        raw = state.get(alert.kind)
        last: Optional[datetime] = None
        if isinstance(raw, str):
            try:
                parsed = datetime.fromisoformat(raw)
                last = parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
            except ValueError:
                last = None
        if last is not None and now - last < timedelta(hours=cooldown_hours):
            logger.info("%s 在冷却期内，本轮不重复发送", alert.kind)
            continue
        kept.append(alert)
    return kept


def read_ledger_watermark(conn: Any) -> Optional[datetime]:
    """台账水位：最后一次装载发生在什么时候。空表返回 None。"""

    with conn.cursor() as cur:
        cur.execute(f"SELECT max(loaded_at) FROM {SCHEMA}.ingest_ledger")
        row = cur.fetchone()
    value = row[0] if row else None
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def send(text: str, webhook: str, timeout: float = 10.0) -> bool:
    """发一条飞书文本消息。用标准库，不为了发个 POST 引入新依赖。

    返回是否送达。任何异常都吞掉并返回 False——看门自己不能因为发不出去而崩溃，
    崩溃了下一轮的判定也一起没了。**永远不要把 webhook URL 打进日志**，它本身是凭据。

    **HTTP 200 不等于送达**：飞书机器人把业务错误（签名不对、机器人被停用、被限频）
    也放在 200 的响应体里，用 `code != 0` 表示。只看状态码会让「发失败」被记成
    「发成功」，进而写入冷却期状态——于是接下来 N 小时连重试都不会有，看门狗静默失效，
    而它存在的全部意义就是在没人看的时候还能报警。所以业务码必须一起判。
    """

    payload = json.dumps({"msg_type": "text", "content": {"text": text}}).encode("utf-8")
    request = urllib.request.Request(
        webhook, data=payload, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:  # noqa: S310
            if not 200 <= resp.status < 300:
                logger.error("告警发送失败：HTTP %s", resp.status)
                return False
            body = resp.read(_MAX_ALERT_RESPONSE_BYTES)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        logger.error("告警发送失败：%s", type(exc).__name__)
        return False
    return _alert_response_ok(body)


#: 只读足够判业务码的长度。响应体不可信，不能无上限读进内存。
_MAX_ALERT_RESPONSE_BYTES = 4096


def _alert_response_ok(body: bytes) -> bool:
    """判飞书响应体里的业务码。**读不懂时按送达处理。**

    这个方向是刻意的：判错成「没送达」只会让下一轮重发一条重复告警，判错成「送达」
    却会吞掉一次真实告警。但响应体格式变化 / 非 JSON 不该让每轮都重复轰炸群，
    所以只在**明确读到非零 code** 时才判失败，其余一律放行并留日志。
    """

    try:
        parsed = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        logger.warning("告警响应体不是 JSON，按送达处理")
        return True
    if not isinstance(parsed, dict):
        return True
    code = parsed.get("code", parsed.get("StatusCode", 0))
    if isinstance(code, bool) or not isinstance(code, int):
        return True
    if code != 0:
        # msg 是飞书自己的错误描述，不含我们的告警正文，可以安全落日志。
        logger.error("告警被拒绝：code=%s msg=%s", code, parsed.get("msg"))
        return False
    return True


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Plum 数仓入库滞后看门")
    parser.add_argument("--ping", action="store_true", help="无异常时也播报一次存活")
    parser.add_argument("--dry-run", action="store_true", help="只打印，不发送、不写状态")
    parser.add_argument("--landing", default=os.environ.get(pull_mod.LANDING_ENV, ""))
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    landing = Path(args.landing) if args.landing else None
    try:
        state = heartbeat.state_dir(landing)
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc

    now = datetime.now(timezone.utc)
    heartbeat_at = heartbeat.finished_at(heartbeat.read(state))

    ledger_at: Optional[datetime] = None
    db_error: Optional[str] = None
    try:
        conn = db.connect()
        try:
            ledger_at = read_ledger_watermark(conn)
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 — 连库的失因很多，这里一律转成告警文案
        db_error = f"{type(exc).__name__}: {exc}"[:300]

    alerts = evaluate(
        now=now,
        heartbeat_at=heartbeat_at,
        ledger_at=ledger_at,
        db_error=db_error,
        heartbeat_hours=_hours(HEARTBEAT_HOURS_ENV, DEFAULT_HEARTBEAT_HOURS),
        ledger_hours=_hours(LEDGER_HOURS_ENV, DEFAULT_LEDGER_HOURS),
    )

    summary = (
        f"心跳={heartbeat_at.isoformat() if heartbeat_at else '无'}"
        f"（{_fmt_age(now - heartbeat_at) if heartbeat_at else '—'}前）"
        f" 台账水位={ledger_at.isoformat() if ledger_at else '无'}"
        f"（{_fmt_age(now - ledger_at) if ledger_at else '—'}前）"
    )
    print(summary)
    for alert in alerts:
        print(f"[!!] {alert.kind}\n{alert.text}")

    webhook = os.environ.get(WEBHOOK_ENV, "").strip()
    if not webhook:
        # 没配 webhook 时不静默通过：有异常就以非 0 退出，至少 systemd 会记一笔失败。
        # 「装了看门但没配告警地址」和「没装看门」在效果上一样，必须让它可见。
        if alerts:
            print(f"[!!] 未配置 {WEBHOOK_ENV}，以上异常无法送达任何人")
        return 1 if alerts else 0

    if args.dry_run:
        print("[dry-run] 不发送、不写状态")
        return 1 if alerts else 0

    saved = read_alert_state(state)
    to_send = filter_cooldown(
        alerts, saved, now, _hours(COOLDOWN_HOURS_ENV, DEFAULT_COOLDOWN_HOURS)
    )
    for alert in to_send:
        if send(alert.text, webhook):
            saved[alert.kind] = now.isoformat()
    if to_send:
        try:
            write_alert_state(state, saved)
        except OSError as exc:
            # 写不了状态只会导致下一轮重复发送，不影响告警本身，降级为日志。
            logger.warning("写入告警状态失败：%s", exc)

    if args.ping and not alerts:
        # 正向播报：本机看门无法自证存活，这条日报的**缺席**才是真正的信号，
        # 需要人来注意到。它不是「一切正常」的证明，只是 dead-man switch 的人肉版。
        send(f"【Plum 数仓】入库正常。\n{summary}", webhook)

    return 1 if alerts else 0


if __name__ == "__main__":
    sys.exit(main())
