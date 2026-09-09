"""用真实 rsync 验证活切片不被复制，无需连接机器 A。"""
from pathlib import Path
from subprocess import CompletedProcess

from ingest.pull import PullResult, pull
import run_ingest


def test_only_manifest_backed_data_is_pulled(tmp_path):
    source = tmp_path / "source"
    day = source / "business_day=2026-09-07"
    day.mkdir(parents=True)
    sealed = day / "plum-2026-09-07T04-abcd-0000.ndjson.gz"
    sealed.write_bytes(b"sealed")
    receipt = Path(str(sealed) + ".manifest")
    receipt.write_text("{}")
    active = day / "plum-2026-09-07T05-abcd-0001.ndjson.gz"
    active.write_bytes(b"still writing")
    (source / ".writer.lock").write_text("locked")
    (day / "unrelated.txt").write_text("unrelated")
    dead_dir = source / "_dead" / "2026-09-07"
    dead_dir.mkdir(parents=True)
    dead = dead_dir / "plum-dead-2026-09-07T04-abcd-0000.ndjson.gz"
    dead.write_bytes(b"dead")
    dead_manifest = Path(str(dead) + ".manifest")
    dead_manifest.write_text("{}")
    landing = tmp_path / "landing"

    result = pull(remote=str(source), landing=landing)
    assert result.returncode == 0, result.stderr
    expected = {p.relative_to(source) for p in (sealed, receipt, dead, dead_manifest)}
    assert {p.relative_to(landing) for p in landing.rglob("*") if p.is_file()} == expected

    receipt.unlink()
    sealed.write_bytes(b"now unsealed")
    assert pull(remote=str(source), landing=landing).returncode == 0
    assert (landing / sealed.relative_to(source)).read_bytes() == b"sealed"


def test_no_manifest_means_no_data_transfer(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "live.ndjson.gz").write_bytes(b"active")
    landing = tmp_path / "landing"
    assert pull(remote=str(source), landing=landing).returncode == 0
    assert list(landing.rglob("*")) == []


def test_scan_failure_stops_before_transfer(tmp_path, monkeypatch):
    calls = []

    def fail_scan(cmd, **kwargs):
        calls.append(cmd)
        return CompletedProcess(cmd, 255, "", "Permission denied")

    monkeypatch.setattr("ingest.pull.subprocess.run", fail_scan)
    result = pull(remote="user@host:/", landing=tmp_path)
    assert result.returncode == 255
    assert len(calls) == 1
    assert list(tmp_path.iterdir()) == []


def test_pull_failure_causes_nonzero_exit(tmp_path, monkeypatch):
    monkeypatch.setattr(run_ingest.pull_mod, "pull", lambda **kw: PullResult(23, "", "failed"))
    assert run_ingest.main(["--only", "pull", "--landing", str(tmp_path)]) == 1
