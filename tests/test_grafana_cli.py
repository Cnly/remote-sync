import json
import sys

import pytest

from remote_sync import cli
from remote_sync.providers.grafana import Grafana
from test_grafana import GrafanaAPI, declarations as folders, folder, rule
from test_grafana_inhibitions import (
    DISCOVERY, PATH, InhibitionsAPI, declaration as inhibition, rule as inhibition_rule,
)


class GrafanaAPIs:
    def __init__(self, folder_api=None, inhibition_api=None):
        self.folders = folder_api or GrafanaAPI()
        self.inhibitions = inhibition_api or InhibitionsAPI()
        self.calls = []
        self.fail_read = None
        self.on_write = None

    def request(self, method, path, **kwargs):
        self.calls.append((method, path))
        if method == "GET" and path == self.fail_read:
            raise RuntimeError("API returned HTTP 403")
        if method != "GET" and self.on_write:
            self.on_write()
        api = self.inhibitions if path.startswith(DISCOVERY) else self.folders
        return api.request(method, path, **kwargs)


def invoke(tmp_path, monkeypatch, api, *arguments, resources=None, provider_name="grafana"):
    config = tmp_path / "grafana.json"
    config.write_text(json.dumps({
        "owner": "test",
        "settings": {"url": "https://example.grafana.net", "namespace": "default"},
        "resources": {"folders": folders(), "inhibitionRules": {"unit-failed": inhibition()}}
                     if resources is None else resources,
    }))
    monkeypatch.setitem(cli.PROVIDERS, provider_name, lambda **settings: Grafana(api=api, **settings))
    monkeypatch.setattr(sys, "argv", ["remote-sync", provider_name, "--config-file", str(config), *arguments])
    return cli.main()


def test_preview_shows_both_plans_without_writes(tmp_path, monkeypatch, capsys):
    api = GrafanaAPIs(folder_api=GrafanaAPI(folder(owner="test"), rules=[rule()]))
    assert invoke(tmp_path, monkeypatch, api) == 0
    output = capsys.readouterr().out
    assert output.startswith("=== folders ===\n\nNo changes.\n\n=== inhibitionRules ===\n\n")
    details, overview = output.split("Overall summary:\n")
    assert "CREATE (inhibitionRules) unit-failed" in details and "Create (1):\n    unit-failed" in details
    assert [line.split() for line in overview.splitlines()] == [
        ["Collection", "Adopt", "Create", "Update", "Delete"],
        ["folders", "0", "0", "0", "0"],
        ["inhibitionRules", "0", "1", "0", "0"],
    ]
    assert all(method == "GET" for method, _ in api.calls)


@pytest.mark.parametrize("provider_name", ["grafana", "example"])
def test_apply_reads_validates_and_prints_both_plans_before_writing(tmp_path, monkeypatch, capsys, provider_name):
    api = GrafanaAPIs()
    before_writes = []

    def check_preview():
        if not before_writes:
            output = capsys.readouterr().out
            assert all(text in output for text in (
                "=== folders ===", "CREATE (folders) infra",
                "=== inhibitionRules ===", "CREATE (inhibitionRules) unit-failed",
            ))
            assert output.count("\nSummary:\n") == 2
            assert output.rindex("\nSummary:\n") < output.index("\nOverall summary:\n")
            before_writes.append(output)

    api.on_write = check_preview
    assert invoke(tmp_path, monkeypatch, api, "--doit", provider_name=provider_name) == 0
    first_write = next(i for i, (method, _) in enumerate(api.calls) if method != "GET")
    assert all(method != "GET" for method, _ in api.calls[first_write:])
    assert set(api.folders.folders) == {"infra"} and set(api.folders.rules) == {"host-down"}
    assert set(api.inhibitions.objects) == {"nix-sync-test--unit-failed"}
    assert "Applied." in capsys.readouterr().out
    api.calls.clear()
    assert invoke(tmp_path, monkeypatch, api, "--doit", provider_name=provider_name) == 0
    output = capsys.readouterr().out
    assert output.count("No changes.") == 2
    assert [line.split() for line in output.split("Overall summary:\n")[1].splitlines()] == [
        ["Collection", "Adopt", "Create", "Update", "Delete"],
        ["folders", "0", "0", "0", "0"],
        ["inhibitionRules", "0", "0", "0", "0"],
    ]
    assert all(method == "GET" for method, _ in api.calls)


@pytest.mark.parametrize("failure", ["discovery", "read", "validation", "ownership"])
def test_inhibition_preflight_failure_prevents_folder_writes(tmp_path, monkeypatch, capsys, failure):
    api = GrafanaAPIs()
    resources = {"folders": folders(), "inhibitionRules": {"unit-failed": inhibition()}}
    if failure in ("discovery", "read"):
        api.fail_read = DISCOVERY if failure == "discovery" else PATH
    elif failure == "validation":
        resources["inhibitionRules"]["unit-failed"]["target_matchers"] = []
    else:
        api.inhibitions = InhibitionsAPI(inhibition_rule(provenance="api"))
    assert invoke(tmp_path, monkeypatch, api, "--doit", resources=resources) == 1
    assert all(method == "GET" for method, _ in api.calls)
    assert not api.folders.folders
    output = capsys.readouterr()
    assert "Error:" in output.err
    assert "CREATE" not in output.out and "Applied." not in output.out


def test_folder_validation_failure_prevents_inhibition_writes(tmp_path, monkeypatch, capsys):
    api = GrafanaAPIs()
    resources = {"folders": folders(), "inhibitionRules": {"unit-failed": inhibition()}}
    resources["folders"]["monitoring"]["rules"][0]["expressions"] = {}
    assert invoke(tmp_path, monkeypatch, api, "--doit", resources=resources) == 1
    assert not api.inhibitions.objects and all(method == "GET" for method, _ in api.calls)
    assert "Exactly one expression" in capsys.readouterr().err


def test_empty_inhibition_declarations_still_reconcile_owned_rules(tmp_path, monkeypatch):
    api = GrafanaAPIs(inhibition_api=InhibitionsAPI(inhibition_rule(), inhibition_rule("manual")))
    assert invoke(tmp_path, monkeypatch, api, "--doit", resources={"folders": {}, "inhibitionRules": {}}) == 0
    assert set(api.inhibitions.objects) == {"manual"}


def test_list_keeps_resource_sets_separate_and_skips_declaration_validation(tmp_path, monkeypatch, capsys):
    uid = "nix-sync-test--unit-failed"
    api = GrafanaAPIs(folder_api=GrafanaAPI(folder(uid)), inhibition_api=InhibitionsAPI(inhibition_rule()))
    resources = {"folders": {"invalid": {}}, "inhibitionRules": {"invalid": {}}}
    assert invoke(tmp_path, monkeypatch, api, "--list", resources=resources) == 0
    inventory = json.loads(capsys.readouterr().out)
    assert set(inventory) == {"folders", "inhibitionRules"}
    assert inventory["folders"][uid]["object"]["kind"] == "Folder"
    assert inventory["inhibitionRules"][uid]["object"]["kind"] == "InhibitionRule"
    assert all(method == "GET" for method, _ in api.calls)


def test_adoption_arguments_still_target_folders_in_the_combined_plan(tmp_path, monkeypatch, capsys):
    api = GrafanaAPIs(folder_api=GrafanaAPI(folder()))
    assert invoke(tmp_path, monkeypatch, api, "--adopt", "monitoring=infra", "--doit") == 0
    assert "ADOPT (folders) infra" in capsys.readouterr().out
    assert api.folders.folders["infra"]["metadata"]["annotations"]["remote-sync/owner"] == "nix-sync-test"
    assert set(api.inhibitions.objects) == {"nix-sync-test--unit-failed"}


def test_write_failure_is_reported_and_next_run_finishes_remaining_changes(tmp_path, monkeypatch, capsys):
    api = GrafanaAPIs()
    api.inhibitions.fail_write = "nix-sync-test--unit-failed"
    assert invoke(tmp_path, monkeypatch, api, "--doit") == 1
    output = capsys.readouterr()
    assert "HTTP 500" in output.err and "Applied." not in output.out
    assert set(api.folders.folders) == {"infra"} and not api.inhibitions.objects
    api.calls.clear()
    assert invoke(tmp_path, monkeypatch, api, "--doit") == 0
    assert [(method, path) for method, path in api.calls if method != "GET"] == [("POST", PATH)]


@pytest.mark.parametrize("resources", [{}, {"folders": {}}, {"folders": {}, "inhibitionRules": {}, "typo": {}}])
def test_missing_or_unknown_resource_sets_fail_before_any_requests(tmp_path, monkeypatch, resources):
    api = GrafanaAPIs()
    assert invoke(tmp_path, monkeypatch, api, "--doit", resources=resources) == 1
    assert api.calls == []
