import json
import os
from pathlib import Path

import pytest

from warehouse.report import demo, publish, render, snapshot


def test_script_data_is_escaped_and_rendered_without_network_dependencies():
    data = demo()
    attack = '</script><img src="x" onerror="alert(1)">'
    data["feed"][0]["character_id"] = attack
    html = render(data)
    assert attack not in html
    encoded = html.split('<script id="report-data" type="application/json">')[1].split('</script>')[0]
    assert json.loads(encoded)["feed"][0]["character_id"] == attack
    assert '<script src=' not in html


def test_failed_publication_preserves_previous_report(monkeypatch, tmp_path):
    output = tmp_path / "index.html"
    publish(demo(), output)
    previous = output.read_bytes()

    def fail(*args):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(OSError):
        publish(demo(), output)
    assert output.read_bytes() == previous
    assert sorted(p.name for p in tmp_path.iterdir()) == ["index.html"]


@pytest.mark.skipif(not os.environ.get("PLUM_DA_TEST_DSN"), reason="requires isolated database")
def test_export_uses_aggregate_only_reader_and_contains_no_browser_ids(monkeypatch, tmp_path):
    import psycopg
    from migrations import apply_migrations
    from warehouse.project import project
    from warehouse.partitions import ensure_partitions
    from test_projection import DAY, event, source
    dsn = os.environ["PLUM_DA_TEST_DSN"]
    with psycopg.connect(dsn) as conn:
        conn.execute("DROP SCHEMA IF EXISTS analytics CASCADE")
        conn.commit()
        apply_migrations(conn)
        ensure_partitions(conn, DAY)
        source(conn, event())
        project(conn, 604800, start_day=DAY)
        conn.execute(Path(__file__).parents[1].joinpath("deploy/report_reader.sql").read_text())
        conn.commit()
        import run_report
        from psycopg.conninfo import make_conninfo
        monkeypatch.setenv("PLUM_DA_DATABASE_URL", dsn)
        monkeypatch.setenv("PLUM_DA_REPORT_DATABASE_URL", make_conninfo(dsn, user="plum_report"))
        output = tmp_path / "index.html"
        assert run_report.main(["--attribution-seconds", "604800", "--output", str(output)]) == 0
        assert "browser-private" not in output.read_text()
        conn.execute("SET ROLE plum_report")
        conn.commit()
        data = snapshot(conn)
        assert data["feed"][0]["impressions"] == 1
        assert "browser-private" not in render(data)
        assert "visitor_id" not in render(data)
        for table in ["product_events", "event_registry", "dim_visitor", "visitor_activity"]:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(f"SELECT * FROM analytics.{table}")
            conn.rollback()
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("INSERT INTO analytics.daily_visitors VALUES ('2026-09-09', 1, 1, 1)")
        conn.rollback()
