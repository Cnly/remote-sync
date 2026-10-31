from copy import deepcopy
import json

import pytest

from conftest import FakeAPI
from remote_sync.cli import print_plan
from remote_sync.core import APIAdapter, ConfigAdapter, Resource
from remote_sync.providers.healthchecks import Healthchecks


def check(uuid="existing", *, slug="server", tags="production", **settings):
    return {
        "uuid": uuid, "name": "Server", "slug": slug, "tags": tags,
        "timeout": 120, "grace": 60, "channels": "email,matrix",
        "desc": "Keep this description", "n_pings": 123, "status": "up", "manual_resume": True,
        "ping_url": f"https://hc-ping.com/{uuid}", **settings,
    }


class HealthchecksAPI:
    """Independent API store; updates omit fields that should stay unchanged."""

    def __init__(self, *checks):
        self.checks = {c["uuid"]: deepcopy(c) for c in checks}
        self.calls = []

    def request(self, method, path, **kwargs):
        self.calls.append((method, path, deepcopy(kwargs)))
        if method == "GET":
            assert path == "/checks/"
            return {"checks": deepcopy(list(self.checks.values()))}
        if method == "POST" and path == "/checks/":
            payload = dict(kwargs["json"])
            assert "unique" not in payload  # Upsert can overwrite unowned checks.
            if payload.get("channels") == "*":
                payload["channels"] = "email,matrix"
            self.checks["created"] = check("created", n_pings=0, status="new", **payload)
            return deepcopy(self.checks["created"])
        uuid, _, operation = path.removeprefix("/checks/").partition("/")
        assert method == "POST"
        if operation == "pause":
            self.checks[uuid]["status"] = "paused"
        elif operation == "resume":
            assert self.checks[uuid]["status"] == "paused"
            self.checks[uuid]["status"] = "new"
        else:
            assert operation == ""
            self.checks[uuid].update(kwargs["json"])
            if "timeout" in kwargs["json"]:
                self.checks[uuid].pop("schedule", None)
                self.checks[uuid].pop("tz", None)
        return deepcopy(self.checks[uuid])

    def ping(self, uuid):
        check = self.checks[uuid]
        if check["status"] == "paused" and check["manual_resume"]:
            return
        check["n_pings"] += 1
        check["status"] = "up"


def plan(api, declarations=None, **kwargs):
    remote = APIAdapter(
        provider=Healthchecks(api=api), owner="test",
        declarations={"host": {"name": "Server", "slug": "server", "timeout": 120, "grace": 60}}
        if declarations is None else declarations, **kwargs,
    )
    desired = ConfigAdapter(desired=remote.desired)
    return remote, desired, remote.diff_from(desired)


@pytest.mark.parametrize("status", ["up", "paused"])
@pytest.mark.parametrize("explicit", [False, True])
def test_adoption_preserves_name_history_tags_integrations_and_uses_one_inventory(capsys, status, explicit):
    api = HealthchecksAPI(check(name="Old name", timeout=300, grace=180, status=status, manual_resume=False),
                          check("manual", slug="manual"),
                          check("foreign", slug="foreign", tags="nix-sync-other"))
    untouched = deepcopy({uuid: api.checks[uuid] for uuid in ("manual", "foreign")})
    adoptions = {"host": "existing"} if explicit else None
    remote, desired, diff = plan(api, adoptions=adoptions)
    assert remote.pending_adoptions == {"server"}
    assert [method for method, _, _ in api.calls] == ["GET"]
    print_plan(diff, remote)
    output = capsys.readouterr().out
    assert "ADOPT server (existing)" in output
    assert '-  "name"' not in output and '+  "name"' not in output
    assert "hc-ping.com" not in output
    remote.sync_from(desired, diff=diff)
    resumed = status == "paused"
    assert [method for method, _, _ in api.calls] == ["GET"] + ["POST"] * (3 if resumed else 2)
    assert [path for _, path, _ in api.calls[1:3]] == ["/checks/existing"] * 2
    assert all("name" not in kwargs.get("json", {}) for _, _, kwargs in api.calls[1:])
    assert api.checks["existing"] == check(name="Old name", tags="production nix-sync-test",
                                          status="new" if resumed else "up")
    assert {uuid: api.checks[uuid] for uuid in untouched} == untouched
    assert not remote.diff_from(desired).has_diffs()
    assert not plan(api)[2].has_diffs()


def test_matching_settings_only_need_the_ownership_tag():
    api = HealthchecksAPI(check(tags="production backup"))
    remote, desired, diff = plan(api)
    remote.sync_from(desired, diff=diff)
    assert api.calls[1:] == [("POST", "/checks/existing", {
        "json": {"tags": "production backup nix-sync-test"},
    })]
    assert not plan(api)[2].has_diffs()


@pytest.mark.parametrize("explicit", [False, True])
def test_keyed_marker_migrates_by_slug_without_changing_the_check(explicit, capsys):
    original = check(name="Custom name", tags="production nix-sync-test--old-key backup")
    api = HealthchecksAPI(original)
    declarations = {"new-key": {"name": "Server", "slug": "server", "timeout": 120, "grace": 60}}
    remote, desired, diff = plan(api, declarations, adoptions={"new-key": "existing"} if explicit else {})
    assert not remote.pending_adoptions
    assert diff.summary()["create"] == diff.summary()["delete"] == 0
    print_plan(diff, remote)
    output = capsys.readouterr().out
    assert "UPDATE server" in output and "ADOPT" not in output
    assert '-  "ownership": "nix-sync-test--old-key"' in output
    assert '+  "ownership": "nix-sync-test"' in output
    remote.sync_from(desired, diff=diff)
    assert api.checks == {"existing": original | {"tags": "production backup nix-sync-test"}}
    assert api.calls == [
        ("GET", "/checks/", {}),
        ("POST", "/checks/existing", {"json": {"tags": "production backup nix-sync-test"}}),
    ]
    assert not plan(api, declarations)[2].has_diffs()


def test_shared_owner_uses_slugs_even_when_declaration_keys_are_swapped():
    api = HealthchecksAPI(check("one", slug="first", tags="nix-sync-test"),
                          check("two", slug="second", tags="nix-sync-test"))
    declarations = {key: {"name": key, "slug": slug, "timeout": 120, "grace": 60}
                    for key, slug in (("second", "first"), ("first", "second"))}
    assert not plan(api, declarations)[2].has_diffs()
    remote, desired, diff = plan(api, {"first": declarations["first"]})
    remote.sync_from(desired, diff=diff)
    assert api.calls[-1] == ("POST", "/checks/one/pause", {})
    assert api.checks["one"]["status"] == "paused"
    assert api.checks["two"]["status"] == "up"


def test_duplicate_owned_slugs_abort_before_writes():
    api = HealthchecksAPI(check(tags="nix-sync-test"), check("duplicate", tags="nix-sync-test--old-key"))
    with pytest.raises(AssertionError, match="Multiple owned resources for server"):
        plan(api)
    assert api.calls == [("GET", "/checks/", {})]


def test_creation_tags_atomically_and_attaches_existing_integrations():
    api = HealthchecksAPI(check("manual", slug="manual"))
    remote, desired, diff = plan(api, {"host": {
        "name": "New server", "slug": "server", "timeout": 120, "grace": 60,
    }})
    assert not remote.pending_adoptions
    remote.sync_from(desired, diff=diff)
    assert [method for method, _, _ in api.calls] == ["GET", "POST"]
    assert api.calls[-1][1:] == ("/checks/", {"json": {
        "name": "New server", "slug": "server", "timeout": 120, "grace": 60,
        "tags": "nix-sync-test", "channels": "*", "manual_resume": True,
    }})
    assert api.checks["created"]["channels"] == "email,matrix"
    assert remote.get(Resource, "server").remote_id == "created"
    assert not plan(api)[2].has_diffs()
    api.checks["created"]["name"] = "Renamed in dashboard"
    assert not plan(api)[2].has_diffs()


def test_config_key_change_reuses_check_by_slug_and_preserves_its_name():
    api = HealthchecksAPI(check(name="Custom name", tags="production nix-sync-test", timeout=300))
    declarations = {"renamed": {"name": "Server", "slug": "server", "timeout": 120, "grace": 60}}
    remote, desired, diff = plan(api, declarations)
    remote.sync_from(desired, diff=diff)
    assert api.calls[1:] == [("POST", "/checks/existing", {"json": {
        "timeout": 120,
    }})]
    assert api.checks["existing"]["n_pings"] == 123
    assert api.checks["existing"]["name"] == "Custom name"
    assert api.checks["existing"]["tags"] == "production nix-sync-test"
    assert not plan(api, declarations)[2].has_diffs()


@pytest.mark.parametrize(("status", "manual_resume"), [
    ("up", False), ("paused", False), ("down", True), ("paused", True),
])
def test_absent_checks_are_paused_only_in_our_namespace(capsys, status, manual_resume):
    original = check(tags="production nix-sync-test", status=status, manual_resume=manual_resume)
    api = HealthchecksAPI(original, check("manual", slug="manual", manual_resume=False),
                          check("foreign", slug="foreign", tags="nix-sync-other"))
    untouched = deepcopy({uuid: api.checks[uuid] for uuid in ("manual", "foreign")})
    remote, desired, diff = plan(api, {})
    assert diff.summary()["delete"] == 0
    assert len(api.calls) == 1
    print_plan(diff, remote)
    output = capsys.readouterr().out
    if status == "paused" and manual_resume:
        assert not diff.has_diffs()
    else:
        assert "UPDATE server" in output and "Update (1):\n    server" in output
        if status != "paused":
            assert '-  "paused": false' in output and '+  "paused": true' in output
    assert "PAUSE server" not in output and "Pause (" not in output
    assert "DELETE server" not in output
    remote.sync_from(desired, diff=diff)
    if diff.has_diffs():
        assert "Updating server..." in capsys.readouterr().err
    assert api.checks["existing"] == original | {"status": "paused", "manual_resume": True}
    assert {uuid: api.checks[uuid] for uuid in untouched} == untouched
    expected_writes = []
    if not manual_resume:
        expected_writes.append(("POST", "/checks/existing", {"json": {"manual_resume": True}}))
    if status != "paused":
        expected_writes.append(("POST", "/checks/existing/pause", {}))
    assert api.calls[1:] == expected_writes
    api.ping("existing")
    assert api.checks["existing"]["status"] == "paused"
    assert not remote.diff_from(desired).has_diffs()
    assert not plan(api, {})[2].has_diffs()


def test_returning_host_resumes_the_same_check_with_its_history(capsys):
    original = check(name="Custom name", tags="production nix-sync-test", status="paused", timeout=300)
    api = HealthchecksAPI(original)
    remote, desired, diff = plan(api)
    assert diff.summary()["create"] == diff.summary()["delete"] == 0
    print_plan(diff, remote)
    output = capsys.readouterr().out
    assert "UPDATE server" in output and "Update (1):\n    server" in output
    assert '-  "paused": true' in output and '+  "paused": false' in output
    assert "RESUME server" not in output and "Resume (" not in output
    remote.sync_from(desired, diff=diff)
    assert "Updating server..." in capsys.readouterr().err
    assert api.calls[1:] == [
        ("POST", "/checks/existing", {"json": {"timeout": 120}}),
        ("POST", "/checks/existing/resume", {}),
    ]
    assert api.checks["existing"] == original | {"timeout": 120, "status": "new"}
    assert not plan(api)[2].has_diffs()


@pytest.mark.parametrize("status", ["new", "up", "down", "grace"])
def test_declared_unpaused_checks_do_not_reset_their_monitoring_status(status):
    api = HealthchecksAPI(check(tags="nix-sync-test", status=status))
    remote, desired, diff = plan(api)
    assert not diff.has_diffs()
    remote.sync_from(desired, diff=diff)
    assert len(api.calls) == 1
    assert api.checks["existing"]["status"] == status


def test_absent_scheduled_check_is_paused_without_changing_its_schedule():
    original = check(tags="nix-sync-test", schedule="* * * * *", tz="UTC", manual_resume=False)
    del original["timeout"]
    api = HealthchecksAPI(original)
    remote, desired, diff = plan(api, {})
    remote.sync_from(desired, diff=diff)
    assert api.checks["existing"] == original | {"status": "paused", "manual_resume": True}
    assert not plan(api, {})[2].has_diffs()


@pytest.mark.parametrize("marker", ["nix-sync-test", "nix-sync-test--old-key"])
def test_failed_pause_is_reconciled_without_replaying_successful_settings_update(monkeypatch, marker):
    api = HealthchecksAPI(check(tags=marker, manual_resume=False))
    request = api.request

    def fail_pause(method, path, **kwargs):
        if path.endswith("/pause"):
            raise RuntimeError("API connection failed")
        return request(method, path, **kwargs)

    monkeypatch.setattr(api, "request", fail_pause)
    remote, desired, diff = plan(api, {})
    with pytest.raises(RuntimeError, match="connection failed"):
        remote.sync_from(desired, diff=diff)
    assert api.checks["existing"]["manual_resume"] is True
    assert api.checks["existing"]["tags"] == "nix-sync-test"
    monkeypatch.setattr(api, "request", request)
    api.calls.clear()
    remote, desired, diff = plan(api, {})
    remote.sync_from(desired, diff=diff)
    assert api.calls == [("GET", "/checks/", {}), ("POST", "/checks/existing/pause", {})]
    assert api.checks["existing"]["n_pings"] == 123


@pytest.mark.parametrize("tags", ["nix-sync-other", "nix-sync-other--host", "nix-sync-testing"])
@pytest.mark.parametrize("explicit", [False, True])
def test_matching_slug_never_takes_over_another_owner(tags, explicit):
    api = HealthchecksAPI(check(tags=tags))
    with pytest.raises(AssertionError, match="refusing to take"):
        plan(api, adoptions={"host": "existing"} if explicit else {})
    assert all(method == "GET" for method, _, _ in api.calls)


@pytest.mark.parametrize("tags, error", [
    ("nix-sync-test nix-sync-other", "Multiple ownership markers"),
    ("nix-sync-test nix-sync-test", "Multiple ownership markers"),
    ("nix-sync-test--host nix-sync-test", "Multiple ownership markers"),
])
def test_invalid_ownership_tags_abort_before_writes(tags, error):
    api = HealthchecksAPI(check(tags=tags))
    with pytest.raises(AssertionError, match=error):
        plan(api)
    assert len(api.calls) == 1


@pytest.mark.parametrize("adoptions", [{}, {"host": "existing"}])
def test_duplicate_remote_slugs_abort_even_with_explicit_selection(adoptions):
    api = HealthchecksAPI(check(), check("duplicate"))
    with pytest.raises(AssertionError, match="slug"):
        plan(api, adoptions=adoptions)
    assert len(api.calls) == 1


def test_slug_change_creates_new_check_and_pauses_old_check():
    original = check(slug="old-slug", tags="nix-sync-test")
    api = HealthchecksAPI(original)
    remote, desired, diff = plan(api)
    assert diff.summary()["create"] == diff.summary()["update"] == 1
    assert diff.summary()["delete"] == 0
    remote.sync_from(desired, diff=diff)
    assert api.checks["existing"] == original | {"status": "paused"}
    assert api.checks["created"]["slug"] == "server"
    assert api.checks["created"]["tags"] == "nix-sync-test"
    assert not plan(api)[2].has_diffs()


def test_duplicate_declared_slugs_abort_before_creating_any_checks():
    declaration = {"name": "Server", "slug": "server", "timeout": 120, "grace": 60}
    api = HealthchecksAPI()
    with pytest.raises(AssertionError, match="Multiple declarations"):
        plan(api, {"first": declaration, "second": declaration})
    assert len(api.calls) == 1


def test_explicit_adoption_selects_the_matching_slug_only():
    api = HealthchecksAPI(check())
    remote, desired, diff = plan(api, adoptions={"host": "existing"})
    remote.sync_from(desired, diff=diff)
    assert api.checks["existing"]["tags"] == "production nix-sync-test"
    api = HealthchecksAPI(check(slug="wrong"))
    with pytest.raises(AssertionError, match="declared adoption target"):
        plan(api, adoptions={"host": "existing"})
    assert len(api.calls) == 1


def test_scheduled_check_is_converted_to_the_declared_heartbeat_period():
    record = check(schedule="* * * * *", tz="UTC")
    del record["timeout"]
    api = HealthchecksAPI(record)
    remote, desired, diff = plan(api)
    remote.sync_from(desired, diff=diff)
    assert api.checks["existing"]["timeout"] == 120
    assert "schedule" not in api.checks["existing"]
    assert not plan(api)[2].has_diffs()


def test_read_only_key_cannot_be_used_for_discovery():
    record = check()
    del record["uuid"]
    api = FakeAPI({"checks": [record]})
    with pytest.raises(AssertionError, match="read-write API key"):
        plan(api)
    assert len(api.calls) == 1


def test_api_key_is_required_at_runtime(monkeypatch):
    monkeypatch.delenv("HEALTHCHECKS_API_KEY", raising=False)
    with pytest.raises(AssertionError, match="HEALTHCHECKS_API_KEY in sopsSecretsFile"):
        Healthchecks()


def test_cli_lists_complete_inventory(tmp_path, monkeypatch, capsys):
    from remote_sync import cli

    config_file = tmp_path / "healthchecks.json"
    config_file.write_text(json.dumps({"owner": "test", "resources": {}}))
    api = HealthchecksAPI(check(tags="production nix-sync-test"))
    monkeypatch.setitem(cli.PROVIDERS, "healthchecks", lambda: Healthchecks(api=api))
    cli.run(["healthchecks", "--config-file", str(config_file), "--list"])
    assert json.loads(capsys.readouterr().out) == {
        "existing": api.checks["existing"] | {"id": "existing"},
    }
    assert len(api.calls) == 1
