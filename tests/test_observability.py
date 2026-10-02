"""The observability files agree with each other and with the audit path the app writes."""

import json
import re
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


def _panels(kind: str) -> list[dict]:
    return [p for p in _dashboard()["panels"] if p["type"] == kind]


def test_dashboard_panels_query_the_audit_job_through_the_loki_uid() -> None:
    dash = _dashboard()
    assert dash["uid"] == "sovrag-audit"
    assert {p["type"] for p in dash["panels"]} == {"stat", "table"}
    assert len(_panels("stat")) == 3 and len(_panels("table")) == 2
    for panel in dash["panels"]:
        assert panel["datasource"] == {"type": "loki", "uid": "loki"}
        for target in panel["targets"]:
            assert target["datasource"] == {"type": "loki", "uid": "loki"}
            assert '{job="sovrag-audit"' in target["expr"]  # stats wrap it in count_over_time
            assert 'principal=~"$principal"' in target["expr"]
    assert {p["title"] for p in _panels("table")} == {"Audit stream", "Refusals"}
    assert {p["title"] for p in _panels("stat")} == {"Answers", "Refusals", "Resolutions"}


def test_stat_panels_count_events_over_the_selected_range() -> None:
    for panel in _panels("stat"):
        (target,) = panel["targets"]
        assert target.get("instant") is True
        assert target["expr"].startswith("sum(count_over_time(")
        assert "[$__range]" in target["expr"]
    exprs = {p["title"]: p["targets"][0]["expr"] for p in _panels("stat")}
    assert 'event="answer", refused="false"' in exprs["Answers"]
    assert 'event="answer", refused="true"' in exprs["Refusals"]
    assert 'event="resolve"' in exprs["Resolutions"]


def test_audit_stream_is_a_table_of_parsed_fields_with_the_filter_split() -> None:
    """Columns come from the JSON line itself; the entitlement filter is split into the
    classifications and departments the principal may see."""
    (stream,) = [p for p in _panels("table") if p["title"] == "Audit stream"]
    kinds = [t["id"] for t in stream["transformations"]]
    assert kinds == ["extractFields", "extractFields", "organize"]
    parse, split, organize = stream["transformations"]
    assert parse["options"] == {
        "source": "Line",
        "format": "json",
        "replace": True,
        "keepTime": True,
    }
    assert split["options"]["source"] == "applied_filter"
    assert split["options"]["format"] == "regexp"
    assert "(?<clearance>" in split["options"]["regExp"]
    assert "(?<departments>" in split["options"]["regExp"]
    shown = organize["options"]["includeByName"]
    assert {k for k, v in shown.items() if v} == {
        "Time",
        "event",
        "principal",
        "refused",
        "reason",
        "clearance",
        "departments",
        "cited_chunk_ids",
        "error",
    }
    assert stream["options"]["sortBy"] == [{"displayName": "Time", "desc": True}]


def test_refusals_table_selects_the_refused_label() -> None:
    """`refused` is a stream label (absent on resolve events), so it belongs in the selector."""
    (refusals,) = [p for p in _panels("table") if p["title"] == "Refusals"]
    expr = refusals["targets"][0]["expr"]
    assert expr.startswith('{job="sovrag-audit", event="answer", refused="true"')
    kinds = [t["id"] for t in refusals["transformations"]]
    assert kinds == ["extractFields", "extractFields", "organize"]


def test_principal_variable_filters_every_panel() -> None:
    dash = _dashboard()
    (var,) = dash["templating"]["list"]
    assert var["name"] == "principal" and var["type"] == "query"
    assert var["query"] == "label_values(principal)"
    assert var["includeAll"] is True


def test_alloy_tails_the_path_the_app_writes_with_only_the_agreed_labels() -> None:
    alloy = (OBS / "alloy.alloy").read_text()
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text())
    assert compose["services"]["app"]["environment"]["AUDIT_LOG_PATH"] == AUDIT_PATH
    assert f'__path__ = "{AUDIT_PATH}"' in alloy
    assert 'job = "sovrag-audit"' in alloy
    assert 'values = {event = "", principal = "", refused = ""}' in alloy
    assert 'url = "http://loki:3100/loki/api/v1/push"' in alloy


def test_loki_accepts_the_existing_audit_history_and_adds_no_service_label() -> None:
    """The audit file predates the profile by days; Loki's default rejects samples older
    than a week, which drops the whole backlog on first start."""
    loki = yaml.safe_load((OBS / "loki.yaml").read_text())
    limits = loki["limits_config"]
    assert limits["reject_old_samples"] is False
    assert limits["discover_service_name"] == []


def test_alloy_drops_the_automatic_filename_label() -> None:
    alloy = (OBS / "alloy.alloy").read_text()
    assert re.search(r'stage\.label_drop\s*\{\s*values\s*=\s*\["filename"\]\s*\}', alloy)


def test_loki_queries_the_ingester_for_any_range() -> None:
    """Backlog lines sit in ingester memory until a chunk flushes; with the default 3h lookback
    the querier never asks the ingester for them, so days of history stay invisible."""
    loki = yaml.safe_load((OBS / "loki.yaml").read_text())
    assert loki["querier"]["query_ingesters_within"] == "0s"


def test_loki_flushes_chunks_within_minutes_so_history_reaches_the_index() -> None:
    """With the defaults (30m idle, 2h max age) backlog streams stayed unqueryable for up to
    two hours; the live check with these values showed all history queryable within 90 s."""
    loki = yaml.safe_load((OBS / "loki.yaml").read_text())
    ingester = loki["ingester"]
    assert ingester["chunk_idle_period"] == "1m"
    assert ingester["max_chunk_age"] == "5m"
    assert ingester["flush_check_period"] == "10s"
