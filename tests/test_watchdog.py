"""入库看门。它的失败模式是「永远不报警」，和没装看门无法区分，所以必须有测试盯着。

重点覆盖两件容易写错、写错了又看不出来的事：
1. 两个信号的阈值不能混用——台账水位按心跳的 6 小时报，会在每个深夜低谷误报；
2. 冷却去重不能把「第一条」也滤掉，也不能在过了冷却期后继续沉默。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import run_watchdog
from ingest import heartbeat
from run_watchdog import Alert, evaluate, filter_cooldown, read_alert_state, write_alert_state

NOW = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)


def _kinds(alerts):
    return {a.kind for a in alerts}


def test_一切正常时不报警():
    alerts = evaluate(NOW, NOW - timedelta(hours=1), NOW - timedelta(hours=2))
    assert alerts == []


def test_心跳变旧报进程异常():
    alerts = evaluate(NOW, NOW - timedelta(hours=7), NOW - timedelta(hours=7))
    assert "heartbeat_stale" in _kinds(alerts)


def test_心跳容忍连续几次抖动():
    """timer 每小时一次，阈值 6 小时意味着连挂 5 次才报；单次网络抖动不该惊动人。"""

    alerts = evaluate(NOW, NOW - timedelta(hours=3), NOW - timedelta(hours=3))
    assert alerts == []


def test_台账水位用的是更宽的窗口而不是心跳阈值():
    """夜间低流量下台账 12 小时不动是正常的；按心跳阈值报会训练出「忽略告警」的习惯。"""

    alerts = evaluate(NOW, NOW - timedelta(minutes=30), NOW - timedelta(hours=12))
    assert alerts == []

    alerts = evaluate(NOW, NOW - timedelta(minutes=30), NOW - timedelta(hours=30))
    assert "ledger_stale" in _kinds(alerts)


def test_台账告警文案必须说明它分不清没流量():
    """这条告警天然二义，文案不写清楚就会被当成「B 坏了」而去查错方向。"""

    (alert,) = evaluate(NOW, NOW - timedelta(minutes=1), NOW - timedelta(hours=30))
    assert "分不清" in alert.text
    assert "30 天" in alert.text


def test_没有心跳文件与心跳变旧是两类告警():
    """前者是「从没装好过」，后者是「跑过但停了」，处置动作不同，不能合并。"""

    alerts = evaluate(NOW, None, NOW - timedelta(hours=1))
    assert _kinds(alerts) == {"heartbeat_missing"}


def test_连不上库时不把读不到水位当成没问题():
    """DB 挂了会让 ledger_at 读不出来，这时候必须报连库失败，而不是静默跳过。"""

    alerts = evaluate(NOW, NOW - timedelta(minutes=1), None, db_error="OperationalError: refused")
    assert _kinds(alerts) == {"db_unreachable"}


def test_台账为空单独报():
    alerts = evaluate(NOW, NOW - timedelta(minutes=1), None)
    assert _kinds(alerts) == {"ledger_empty"}


def test_冷却期内不重复发送但第一条要发():
    alerts = [Alert("heartbeat_stale", "x")]
    assert filter_cooldown(alerts, {}, NOW, cooldown_hours=6) == alerts

    recent = {"heartbeat_stale": (NOW - timedelta(hours=1)).isoformat()}
    assert filter_cooldown(alerts, recent, NOW, cooldown_hours=6) == []

    old = {"heartbeat_stale": (NOW - timedelta(hours=7)).isoformat()}
    assert filter_cooldown(alerts, old, NOW, cooldown_hours=6) == alerts


def test_冷却状态坏掉时按未发送处理():
    """状态文件损坏不该让告警永久沉默——宁可重复发，也不能不发。"""

    assert filter_cooldown([Alert("k", "x")], {"k": "不是时间"}, NOW) == [Alert("k", "x")]


def test_冷却按类型隔离():
    """心跳已在冷却期不该顺带压掉刚出现的连库失败。"""

    alerts = [Alert("heartbeat_stale", "x"), Alert("db_unreachable", "y")]
    recent = {"heartbeat_stale": NOW.isoformat()}
    assert _kinds(filter_cooldown(alerts, recent, NOW)) == {"db_unreachable"}


def test_告警状态可写可读(tmp_path):
    write_alert_state(tmp_path, {"heartbeat_stale": NOW.isoformat()})
    assert read_alert_state(tmp_path)["heartbeat_stale"] == NOW.isoformat()


def test_状态文件损坏时读成空而不是抛(tmp_path):
    (tmp_path / "watchdog_alerts.json").write_text("{坏的", encoding="utf-8")
    assert read_alert_state(tmp_path) == {}


def test_心跳读写往返(tmp_path):
    path = heartbeat.write_success(tmp_path, {"sealed_slices": 3, "load": {"rows_loaded": 42}})
    payload = heartbeat.read(tmp_path)
    assert payload["sealed_slices"] == 3
    assert payload["rows_loaded"] == 42
    assert heartbeat.finished_at(payload) is not None
    assert json.loads(path.read_text(encoding="utf-8"))["finished_at"]


def test_心跳时间取内容而不是文件mtime(tmp_path):
    """备份、rsync、touch 都会改 mtime，但都不代表入库真的跑过。"""

    heartbeat.write_success(tmp_path, {})
    target = tmp_path / heartbeat.FILENAME
    payload = json.loads(target.read_text(encoding="utf-8"))
    payload["finished_at"] = (NOW - timedelta(days=3)).isoformat()
    target.write_text(json.dumps(payload), encoding="utf-8")
    import os

    os.utime(target, None)  # mtime 刷新到此刻
    assert heartbeat.finished_at(heartbeat.read(tmp_path)) == NOW - timedelta(days=3)


def test_心跳损坏时当作没有心跳(tmp_path):
    (tmp_path / heartbeat.FILENAME).write_text("半个 json {", encoding="utf-8")
    assert heartbeat.read(tmp_path) is None


def test_状态目录默认挂在落地目录下且不会被清理误删(tmp_path):
    """prune_landing 只删名字能解析成日期的目录，`.state` 必须避开这个形态。"""

    from ingest.pull import prune_landing

    directory = heartbeat.state_dir(tmp_path)
    heartbeat.write_success(directory, {})
    prune_landing(tmp_path, datetime(2026, 9, 7).date(), retention_days=0)
    assert heartbeat.read(directory) is not None


def test_业务错误码算发送失败():
    """飞书把「签名不对 / 机器人被停用 / 限频」放在 HTTP 200 的响应体里。

    只看状态码会把这三种情况记成送达，进而写入冷却期——接下来几个小时连重试都没有，
    看门狗静默失效。
    """

    assert run_watchdog._alert_response_ok(b'{"code":19021,"msg":"sign match fail"}') is False


def test_业务码为零算送达():
    assert run_watchdog._alert_response_ok(b'{"code":0,"msg":"success"}') is True


def test_unknown_response_does_not_confirm_delivery():
    for body in [b"<html>502</html>", b"[]", b'{"msg":"ok"}', b'{"code":true}']:
        assert run_watchdog._alert_response_ok(body) is False
    assert run_watchdog._alert_response_ok(b'{"StatusCode":0}') is True


def test_failed_health_ping_returns_nonzero(monkeypatch, tmp_path):
    from types import SimpleNamespace
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(run_watchdog.db, "connect", lambda: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(run_watchdog, "read_ledger_watermark", lambda conn: now)
    monkeypatch.setattr(run_watchdog.heartbeat, "finished_at", lambda value: now)
    monkeypatch.setattr(run_watchdog, "send", lambda *args: False)
    monkeypatch.setenv(run_watchdog.WEBHOOK_ENV, "https://example.invalid/test")
    assert run_watchdog.main(["--ping", "--landing", str(tmp_path)]) == 1
    monkeypatch.setattr(run_watchdog, "send", lambda *args: True)
    assert run_watchdog.main(["--ping", "--landing", str(tmp_path)]) == 0
    monkeypatch.delenv(run_watchdog.WEBHOOK_ENV)
    assert run_watchdog.main(["--ping", "--landing", str(tmp_path)]) == 1
