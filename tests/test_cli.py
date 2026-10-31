import json
import sys

import pytest

from remote_sync import cli
from test_core import Provider, resource


def invoke(tmp_path, monkeypatch, provider, *arguments, resources=None, owner="test"):
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps({
        "owner": owner,
        "resources": {"api": {"value": 2}} if resources is None else resources,
    }))
    monkeypatch.setitem(cli.PROVIDERS, "hetrixtools", lambda: provider)
    monkeypatch.setattr(sys, "argv", ["remote-sync", "hetrixtools", "--config-file", str(config_file), *arguments])
    return cli.main()


def test_unknown_provider_is_rejected_before_reading_config(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.run(["unknown", "--config-file", "missing.json"])
    assert exc.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


def test_provider_connection_settings_are_passed_separately_from_resources(tmp_path, monkeypatch):
    config = tmp_path / "grafana.json"
    config.write_text(json.dumps({
        "owner": "test", "resources": {},
        "settings": {"url": "https://example.grafana.net", "namespace": "stacks-123"},
    }))
    received = []

    def provider(**settings):
        received.append(settings)
        return Provider()

    monkeypatch.setitem(cli.PROVIDERS, "grafana", provider)
    cli.run(["grafana", "--config-file", str(config)])
    assert received == [{"url": "https://example.grafana.net", "namespace": "stacks-123"}]


@pytest.mark.parametrize("arguments, applied", [((), False), (("--doit",), True)])
def test_cli_only_applies_with_doit(tmp_path, monkeypatch, capsys, arguments, applied):
    provider = Provider(resource("api"))
    assert invoke(tmp_path, monkeypatch, provider, *arguments) == 0
    assert "UPDATE api" in capsys.readouterr().out
    assert bool(provider.writes) == applied
    assert provider.resources["id"]["value"] == (2 if applied else 1)


@pytest.mark.parametrize("owner, key", [("Personal.Config", "HTTP_Main/Primary"), ("test", "host" * 30)])
def test_cli_reconciles_owner_and_keys_without_naming_rules(tmp_path, monkeypatch, capsys, owner, key):
    provider = Provider(resource(key, owner=owner))
    declarations = {key: {"value": 2}}
    assert invoke(tmp_path, monkeypatch, provider, "--doit", owner=owner, resources=declarations) == 0
    assert f"UPDATE {key}" in capsys.readouterr().out
    assert provider.resources == {"id": resource(key, owner=owner, value=2)}
    assert provider.writes == [("update", "id")]
    assert invoke(tmp_path, monkeypatch, provider, owner=owner, resources=declarations) == 0
    assert "No changes." in capsys.readouterr().out


def test_cli_lists_inventory_without_resolving_declarations(tmp_path, monkeypatch, capsys):
    provider = Provider({"id": "manual", "name": "manual"}, {"id": "owned", "name": "nix-sync-test--api"})
    assert invoke(tmp_path, monkeypatch, provider, "--list", resources={"api": {}}) == 0
    assert json.loads(capsys.readouterr().out) == provider.resources
    assert provider.reads == [("list",)]
    assert not provider.writes


def test_cli_adoption_requires_doit_and_applies_the_full_plan(tmp_path, monkeypatch, capsys):
    provider = Provider({"id": "manual", "name": "manual", "value": 7}, resource("obsolete", "old"))
    resources = {"api": {"value": 2}, "new": {"value": 3}}
    assert invoke(tmp_path, monkeypatch, provider, "--adopt", "api=manual", resources=resources) == 0
    output = capsys.readouterr().out
    assert all(change in output for change in ("ADOPT api (manual)", "CREATE new", "DELETE obsolete"))
    assert not provider.writes
    assert invoke(tmp_path, monkeypatch, provider, "--adopt", "api=manual", "--doit", resources=resources) == 0
    assert provider.resources == {
        "manual": {"id": "manual", "name": "nix-sync-test--api", "value": 2},
        "new": {"id": "new", "name": "nix-sync-test--new", "value": 3},
    }


def test_cli_deletion_is_previewed_and_applied_with_doit(tmp_path, monkeypatch, capsys):
    provider = Provider(resource("api"))
    assert invoke(tmp_path, monkeypatch, provider, resources={}) == 0
    assert "DELETE api" in capsys.readouterr().out
    assert not provider.writes
    assert invoke(tmp_path, monkeypatch, provider, "--doit", resources={}) == 0
    assert not provider.resources


@pytest.mark.parametrize("operation", ["list", "update"])
def test_interrupt_exits_quietly_during_discovery_or_apply(tmp_path, monkeypatch, capsys, operation):
    provider = Provider(resource("api"))

    def interrupt(*args):
        raise KeyboardInterrupt

    monkeypatch.setattr(provider, operation, interrupt)
    assert invoke(tmp_path, monkeypatch, provider, "--doit") == 130
    output = capsys.readouterr()
    assert output.err.endswith("\nInterrupted.\n")
    assert "Traceback" not in output.err and "Error:" not in output.err
    assert "Applied." not in output.out
    assert not provider.writes
