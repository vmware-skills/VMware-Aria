"""The anomaly column reads the key Aria itself calls "Total Anomalies".

Flagged 2026-09-14 as possibly mislabelled: vcsa showed 5 while it had no
alerts. Checked on the live 8.18.7 key catalogue on 2026-09-15 — the label was
right and the suspicion was not:

    System Attributes|total_alarms       VMware Aria Operations Generated|Total Anomalies
    System Attributes|total_alert_count  VMware Aria Operations Generated|Total Alert Count

and vcsa read total_alarms=5 while alert_count_critical/immediate/warning were
all 0. The CLI now uses Aria's own name and says which key is the alert count,
so the next reader does not have to re-derive this.
"""

from __future__ import annotations

import pytest


def test_the_anomaly_key_is_not_the_alert_count_key():
    from vmware_aria.ops.anomaly import _TOTAL_ANOMALIES_STAT_KEY

    assert _TOTAL_ANOMALIES_STAT_KEY == "System Attributes|total_alarms"


def test_cli_names_total_anomalies_and_points_at_the_alert_count(monkeypatch: pytest.MonkeyPatch):
    from typer.testing import CliRunner

    from vmware_aria import cli
    from vmware_aria.ops import anomaly

    monkeypatch.setattr(cli, "_get_connection", lambda *_a, **_k: (object(), None))
    monkeypatch.setattr(
        anomaly,
        "list_anomalies",
        lambda *_a, **_k: {"items": [{"resource_id": "c996", "resource_name": "vcsa", "anomaly_count": 5.0}]},
    )
    monkeypatch.setenv("COLUMNS", "200")
    result = CliRunner().invoke(cli.app, ["anomaly", "list"])
    assert result.exit_code == 0, result.output
    assert "Total Anomalies" in result.output
    assert "total_alert_count" in result.output
