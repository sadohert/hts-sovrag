"""The observability files agree with each other and with the audit path the app writes."""

import json
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]
OBS = ROOT / "observability"
AUDIT_PATH = "/app/audit/audit.jsonl"


def _dashboard() -> dict:
    return json.loads((OBS / "grafana/dashboards/sovrag-audit.json").read_text())


def test_datasource_is_loki_by_uid_at_the_compose_service() -> None:
    ds = yaml.safe_load((OBS / "grafana/provisioning/datasources/loki.yaml").read_text())
    (source,) = ds["datasources"]
    assert source["uid"] == "loki" and source["type"] == "loki"
    assert source["url"] == "http://loki:3100"
    assert source["isDefault"] is True


def test_dashboard_provider_points_at_the_mounted_dashboards_dir() -> None:
    provider = yaml.safe_load((OBS / "grafana/provisioning/dashboards/provider.yaml").read_text())
    (p,) = provider["providers"]
    assert p["type"] == "file"
    assert p["options"]["path"] == "/var/lib/grafana/dashboards"


def test_dashboard_panels_query_the_audit_job_through_the_loki_uid() -> None:
    dash = _dashboard()
    assert dash["uid"] == "sovrag-audit"
    assert len(dash["panels"]) == 2
    for panel in dash["panels"]:
        assert panel["type"] == "logs"
        assert panel["datasource"] == {"type": "loki", "uid": "loki"}
        for target in panel["targets"]:
            assert target["expr"].startswith('{job="sovrag-audit"')
            assert "| json" in target["expr"]
    titles = {p["title"] for p in dash["panels"]}
    assert titles == {"Audit stream", "Refusals"}


def test_refusals_panel_filters_on_the_parsed_field_not_only_the_label() -> None:
    (refusals,) = [p for p in _dashboard()["panels"] if p["title"] == "Refusals"]
    expr = refusals["targets"][0]["expr"]
    assert 'event="answer"' in expr
    assert '| refused="true"' in expr  # resolve events carry no refused field


def test_principal_variable_filters_both_panels() -> None:
    dash = _dashboard()
    (var,) = dash["templating"]["list"]
    assert var["name"] == "principal" and var["type"] == "query"
    assert var["query"] == "label_values(principal)"
    assert var["includeAll"] is True
    for panel in dash["panels"]:
        assert 'principal=~"$principal"' in panel["targets"][0]["expr"]


def test_alloy_tails_the_path_the_app_writes_with_only_the_agreed_labels() -> None:
    alloy = (OBS / "alloy.alloy").read_text()
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text())
    assert compose["services"]["app"]["environment"]["AUDIT_LOG_PATH"] == AUDIT_PATH
    assert f'__path__ = "{AUDIT_PATH}"' in alloy
    assert 'job = "sovrag-audit"' in alloy
    assert 'values = {event = "", principal = "", refused = ""}' in alloy
    assert 'url = "http://loki:3100/loki/api/v1/push"' in alloy
