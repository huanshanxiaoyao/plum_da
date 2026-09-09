from types import SimpleNamespace

import run_ingest


def test_retention_guard_never_prevents_new_raw_ingestion(monkeypatch, tmp_path):
    operations = []
    conn = SimpleNamespace(close=lambda: None, rollback=lambda: operations.append("rollback"))
    monkeypatch.setattr(run_ingest.db, "connect", lambda: conn)
    monkeypatch.setattr(run_ingest.partitions_mod, "ensure_partitions", lambda *args: [])

    def load(*args):
        operations.append("load")
        return {"failed": []}

    def expire(*args):
        operations.append("expire")
        raise RuntimeError("unprojected source")

    monkeypatch.setattr(run_ingest, "load_pending", load)
    monkeypatch.setattr(run_ingest.partitions_mod, "drop_expired_partitions", expire)
    result = run_ingest.main(["--only", "partitions", "--only", "load", "--landing", str(tmp_path)])
    assert result == 1
    assert operations == ["load", "expire", "rollback"]
