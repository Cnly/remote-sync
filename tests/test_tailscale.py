from copy import deepcopy
import json
from urllib.parse import unquote

import pytest
import requests

from remote_sync.core import print_plan
from remote_sync.providers.tailscale import Tailscale
from test_core import apply, plan
from test_http import Response, Session


MARKER = "nix-sync-test"
DECLARATION = {"name": "svc:app", "displayName": "", "ports": ["tcp:443"], "tags": [], "devices": ["seattle"]}


def service(name="svc:app", **fields):
    return {
        "name": name, "displayName": "", "ports": ["tcp:443"], "tags": [],
        "comment": MARKER, "addrs": ["100.93.49.180", "fd7a:115c:a1e0::3456:3cb4"], **fields,
    }


def device(node_id="nHostCNTRL", **fields):
    return {"nodeId": node_id, "id": "123", "hostname": "seattle", "name": "seattle.tail.example.ts.net", **fields}


class TailscaleAPI:
    """Independent service state with full PUT replacement and per-host approvals."""

    def __init__(self, *services, devices=None, approvals=None):
        self.services = {item["name"]: deepcopy(item) for item in services}
        self.devices = [device()] if devices is None else deepcopy(devices)
        self.approvals = deepcopy(approvals or {})
        self.calls = []
        self.fail_approval = False

    @property
    def writes(self):
        return [call for call in self.calls if call[0] != "GET"]

    def request(self, method, path, **kwargs):
        self.calls.append((method, path, deepcopy(kwargs)))
        if path == "/tailnet/-/services":
            assert method == "GET"
            return {"vipServices": deepcopy(list(self.services.values()))}
        if path == "/tailnet/-/devices":
            assert method == "GET"
            return {"devices": deepcopy(self.devices)}
        name, _, rest = unquote(path.removeprefix("/tailnet/-/services/")).partition("/")
        if rest:
            prefix, node_id, operation = rest.split("/")
            assert prefix == "device" and operation == "approved"
            assert name in self.services
            assert node_id in {item["nodeId"] for item in self.devices}
            if method == "POST":
                assert kwargs["json"] == {"approved": True}
                if self.fail_approval:
                    raise RuntimeError("Approval failed")
                self.approvals[name, node_id] = {"approved": True, "autoApproved": False}
            else:
                assert method == "GET"
            return deepcopy(self.approvals.get((name, node_id), {"approved": False, "autoApproved": False}))
        if method == "DELETE":
            assert kwargs == {"expect_json": False}
            del self.services[name]
            return None
        assert method == "PUT"
        payload = deepcopy(kwargs["json"])
        new_name = payload["name"]
        assert new_name == name or new_name not in self.services
        if name in self.services:
            del self.services[name]
            self.approvals = {(new_name if old_name == name else old_name, node_id): approval
                              for (old_name, node_id), approval in self.approvals.items()}
        else:
            assert name == new_name
            payload["addrs"] = ["100.93.49.181", "fd7a:115c:a1e0::3456:3cb5"]
        self.services[new_name] = payload
        return deepcopy(payload)


def sync_plan(api, declarations=None, **kwargs):
    return plan(Tailscale(api=api), {"app": DECLARATION} if declarations is None else declarations, **kwargs)


@pytest.mark.parametrize("explicit", [False, True])
def test_adoption_preserves_other_comment_content_and_addresses(explicit, capsys):
    comment = "  Operator note: café\nKeep this line.\n"
    original = service(comment=comment, displayName="Old label")
    api = TailscaleAPI(original)
    remote, desired, diff = sync_plan(api, adoptions={"app": "svc:app"} if explicit else {})
    print_plan(diff, remote)
    output = capsys.readouterr().out
    assert "ADOPT svc:app (svc:app)" in output and "Operator note" not in output
    assert not api.writes
    reads = [call for call in api.calls if call[0] == "GET"]
    apply(remote, desired, diff)
    assert api.services["svc:app"] == original | {"comment": comment + MARKER, "displayName": ""}
    assert api.approvals["svc:app", "nHostCNTRL"]["approved"] is True
    assert [call for call in api.calls if call[0] == "GET"] == reads
    assert not sync_plan(api)[2].has_diffs()


def test_create_attaches_ownership_and_approves_declared_hosts():
    api = TailscaleAPI()
    remote, desired, diff = sync_plan(api)
    assert not api.writes
    apply(remote, desired, diff)
    assert api.services["svc:app"]["comment"] == MARKER
    assert api.services["svc:app"]["displayName"] == ""
    assert [call[0] for call in api.writes] == ["PUT", "POST"]
    assert not any("/approved" in path for method, path, _ in api.calls if method == "GET")
    assert not sync_plan(api)[2].has_diffs()


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_keyed_marker_migration_preserves_comment_text_addresses_and_service(newline, capsys):
    old_marker = "nix-sync-test--old-key"
    comment = newline.join([f"Operator note: café {old_marker}", f" \t{old_marker}  ", "Keep this line.", ""])
    original = service(comment=comment)
    api = TailscaleAPI(original)
    declarations = {"new-key": DECLARATION | {"devices": []}}
    remote, desired, diff = sync_plan(api, declarations)
    assert not remote.pending_adoptions
    assert diff.summary()["create"] == diff.summary()["delete"] == 0
    print_plan(diff, remote)
    output = capsys.readouterr().out
    assert "UPDATE svc:app" in output and "ADOPT" not in output
    assert "Operator note" not in output
    apply(remote, desired, diff)
    expected = original | {"comment": comment.replace(f" \t{old_marker}  ", f" \t{MARKER}  ")}
    assert api.services == {"svc:app": expected}
    assert api.calls == [
        ("GET", "/tailnet/-/services", {}),
        ("PUT", "/tailnet/-/services/svc%3Aapp", {"json": expected}),
    ]
    assert not sync_plan(api, declarations)[2].has_diffs()


def test_pending_host_approval_is_an_update_without_rewriting_service(capsys):
    api = TailscaleAPI(service(), devices=[device(), device("nOtherCNTRL", id="456", hostname="other", name="other.tail.example.ts.net")],
                       approvals={("svc:app", "nOtherCNTRL"): {"approved": True, "autoApproved": False}})
    remote, desired, diff = sync_plan(api)
    print_plan(diff, remote)
    output = capsys.readouterr().out
    assert "UPDATE svc:app" in output and '"seattle": false' in output and '"seattle": true' in output
    assert "Update (1):\n    svc:app" in output
    apply(remote, desired, diff)
    assert api.writes == [("POST", "/tailnet/-/services/svc%3Aapp/device/nHostCNTRL/approved", {"json": {"approved": True}})]
    assert api.approvals["svc:app", "nOtherCNTRL"]["approved"] is True
    assert not remote.diff_from(desired).has_diffs()


@pytest.mark.parametrize("approval", [
    {"approved": True, "autoApproved": False},
    {"approved": False, "autoApproved": True},
])
def test_existing_manual_or_automatic_approval_is_a_noop(approval):
    api = TailscaleAPI(service(), approvals={("svc:app", "nHostCNTRL"): approval})
    remote, desired, diff = sync_plan(api)
    assert not diff.has_diffs()
    apply(remote, desired, diff)
    assert not api.writes


def test_removing_a_device_declaration_does_not_revoke_approval():
    api = TailscaleAPI(service(), approvals={("svc:app", "nHostCNTRL"): {"approved": True, "autoApproved": False}})
    assert not sync_plan(api, {"app": DECLARATION | {"devices": []}})[2].has_diffs()
    assert api.calls == [("GET", "/tailnet/-/services", {})]
    assert api.approvals["svc:app", "nHostCNTRL"]["approved"] is True


def test_config_key_change_reuses_service_by_name_and_approves_hosts():
    original = service(comment="Operator note\n" + MARKER)
    api = TailscaleAPI(original)
    declarations = {"renamed": DECLARATION | {"displayName": "Friendly name"}}
    apply(*sync_plan(api, declarations))
    assert list(api.services) == ["svc:app"]
    assert api.services["svc:app"]["addrs"] == original["addrs"]
    assert api.services["svc:app"]["comment"] == original["comment"]
    assert api.writes[0][:2] == ("PUT", "/tailnet/-/services/svc%3Aapp")
    assert api.writes[1][1] == "/tailnet/-/services/svc%3Aapp/device/nHostCNTRL/approved"
    assert not sync_plan(api, declarations)[2].has_diffs()


@pytest.mark.parametrize("operation", ["create", "update", "migrate"])
def test_failed_approval_recovers_without_replaying_service_write(operation):
    existing = service(displayName="Old label", comment="nix-sync-test--old-key" if operation == "migrate" else MARKER)
    api = TailscaleAPI(*([existing] if operation != "create" else []))
    api.fail_approval = True
    with pytest.raises(RuntimeError, match="Approval failed"):
        apply(*sync_plan(api))
    assert api.services["svc:app"]["comment"] == MARKER
    api.fail_approval = False
    api.calls.clear()
    apply(*sync_plan(api))
    assert [call[0] for call in api.writes] == ["POST"]
    assert not sync_plan(api)[2].has_diffs()


@pytest.mark.parametrize("comment", ["nix-sync-other", "nix-sync-other--app", "nix-sync-testing"])
@pytest.mark.parametrize("explicit", [False, True])
def test_adoption_never_takes_over_another_marker(comment, explicit):
    api = TailscaleAPI(service(comment=comment))
    with pytest.raises(AssertionError, match="refusing to take"):
        sync_plan(api, adoptions={"app": "svc:app"} if explicit else {})
    assert not api.writes


@pytest.mark.parametrize("comment", [MARKER + "\n" + MARKER, "nix-sync-test--app\n" + MARKER])
def test_ambiguous_comment_markers_abort_before_writes(comment):
    api = TailscaleAPI(service(comment=comment))
    with pytest.raises(AssertionError, match="ownership marker"):
        sync_plan(api)
    assert not api.writes


def test_service_name_change_creates_new_service_and_deletes_old_service():
    original = service()
    api = TailscaleAPI(original)
    declarations = {"app": DECLARATION | {"name": "svc:renamed"}}
    remote, desired, diff = sync_plan(api, declarations)
    assert diff.summary()["create"] == diff.summary()["delete"] == 1
    assert diff.summary()["update"] == 0
    apply(remote, desired, diff)
    assert list(api.services) == ["svc:renamed"]
    assert api.services["svc:renamed"]["comment"] == MARKER
    assert api.services["svc:renamed"]["addrs"] != original["addrs"]
    assert api.approvals["svc:renamed", "nHostCNTRL"]["approved"] is True
    assert not sync_plan(api, declarations)[2].has_diffs()


def test_duplicate_service_declarations_abort_before_writes():
    api = TailscaleAPI()
    with pytest.raises(AssertionError, match="Multiple declarations"):
        sync_plan(api, {"first": DECLARATION, "second": DECLARATION})
    assert not api.writes


def test_deletion_only_removes_owned_services():
    api = TailscaleAPI(service(), service("svc:manual", comment="Mentioned " + MARKER),
                       service("svc:foreign", comment="nix-sync-other--app"))
    apply(*sync_plan(api, {}))
    assert set(api.services) == {"svc:manual", "svc:foreign"}
    assert api.writes == [("DELETE", "/tailnet/-/services/svc%3Aapp", {"expect_json": False})]


@pytest.mark.parametrize("selector", ["seattle", "seattle.tail.example.ts.net", "nHostCNTRL", "123"])
def test_device_selectors_resolve_to_stable_node_ids(selector):
    api = TailscaleAPI(service())
    apply(*sync_plan(api, {"app": DECLARATION | {"devices": [selector]}}))
    assert api.writes[0][1].endswith("/device/nHostCNTRL/approved")
    assert sum(path == "/tailnet/-/devices" for _, path, _ in api.calls) == 1


def test_services_can_be_created_before_hosts_join_then_approved_on_next_sync(capsys):
    api = TailscaleAPI(devices=[])
    remote, desired, diff = sync_plan(api)
    assert remote.desired["svc:app"]["devices"] == {}
    assert diff.summary()["create"] == 1
    assert not api.writes
    assert "deferring service approvals" in capsys.readouterr().err
    apply(remote, desired, diff)
    original = deepcopy(api.services["svc:app"])
    assert original["comment"] == MARKER
    assert [method for method, _, _ in api.writes] == ["PUT"]
    assert not any("/approved" in path for _, path, _ in api.calls)
    assert not sync_plan(api)[2].has_diffs()

    api.devices.append(device())
    api.calls.clear()
    remote, desired, diff = sync_plan(api)
    assert diff.summary()["update"] == 1
    apply(remote, desired, diff)
    assert api.writes == [
        ("POST", "/tailnet/-/services/svc%3Aapp/device/nHostCNTRL/approved", {"json": {"approved": True}}),
    ]
    assert api.services["svc:app"] == original
    assert not sync_plan(api)[2].has_diffs()


def test_missing_host_warns_once_and_does_not_block_other_hosts_or_service_updates(capsys):
    api = TailscaleAPI(service(displayName="Old label"), service("svc:second"))
    declaration = DECLARATION | {"devices": ["seattle", "kawai"]}
    declarations = {"app": declaration, "second": declaration | {"name": "svc:second"}}
    remote, desired, diff = sync_plan(api, declarations)
    assert capsys.readouterr().err.count("Tailscale device 'kawai' was not found") == 1
    assert all(settings["devices"] == {"seattle": True} for settings in remote.desired.values())
    apply(remote, desired, diff)
    assert api.services["svc:app"]["displayName"] == ""
    assert [method for method, _, _ in api.writes] == ["PUT", "POST", "POST"]
    assert all(api.approvals[name, "nHostCNTRL"]["approved"] for name in api.services)
    assert not sync_plan(api, declarations)[2].has_diffs()


def test_ambiguous_device_names_abort_before_writes():
    api = TailscaleAPI(service(), devices=[device(), device("nOtherCNTRL", id="456")])
    with pytest.raises(AssertionError, match="Multiple devices"):
        sync_plan(api)
    assert not api.writes


def test_shared_device_inventory_is_fetched_once_for_multiple_services():
    api = TailscaleAPI(service(), service("svc:second"))
    declarations = {"app": DECLARATION, "second": DECLARATION | {"name": "svc:second"}}
    apply(*sync_plan(api, declarations))
    assert sum(path == "/tailnet/-/devices" for _, path, _ in api.calls) == 1
    assert len(api.writes) == 2 and all(method == "POST" for method, _, _ in api.writes)


def test_cli_lists_service_inventory_without_reading_devices(tmp_path, monkeypatch, capsys):
    from remote_sync import cli

    config_file = tmp_path / "tailscale.json"
    config_file.write_text(json.dumps({"owner": "test", "resources": {}}))
    api = TailscaleAPI(service())
    monkeypatch.setitem(cli.PROVIDERS, "tailscale", lambda: Tailscale(api=api))
    cli.run(["tailscale", "--config-file", str(config_file), "--list"])
    assert json.loads(capsys.readouterr().out) == api.services
    assert api.calls == [("GET", "/tailnet/-/services", {})]


def test_oauth_client_secret_is_required_at_runtime(monkeypatch):
    monkeypatch.delenv("TAILSCALE_OAUTH_CLIENT_SECRET", raising=False)
    with pytest.raises(AssertionError, match="TAILSCALE_OAUTH_CLIENT_SECRET in sopsSecretsFile"):
        Tailscale()


def oauth_provider(monkeypatch, *responses):
    monkeypatch.delenv("TAILSCALE_OAUTH_CLIENT_ID", raising=False)
    monkeypatch.setenv("TAILSCALE_OAUTH_CLIENT_SECRET", "test-client-secret")
    session = Session(*responses)
    monkeypatch.setattr("remote_sync.http.requests.Session", lambda: session)
    return Tailscale(), session


def test_oauth_exchange_reuses_token_and_renews_before_expiry(monkeypatch, capsys):
    now = [1000]
    monkeypatch.setattr("remote_sync.http.time.monotonic", lambda: now[0])
    monkeypatch.setattr("remote_sync.http.time.sleep", lambda _: None)
    provider, session = oauth_provider(monkeypatch,
        Response(200, {"access_token": "first-access-token", "expires_in": 3600}),
        Response(200, {"vipServices": []}), Response(200, {"vipServices": []}),
        Response(200, {"access_token": "second-access-token", "expires_in": 3600}),
        Response(200, {"vipServices": []}),
    )
    assert session.calls == []
    assert provider.list() == []
    now[0] += 3500
    assert provider.list() == []
    now[0] += 71
    assert provider.list() == []
    assert [args[0] for args, _ in session.calls] == ["POST", "GET", "GET", "POST", "GET"]
    for index in (0, 3):
        args, kwargs = session.calls[index]
        assert args == ("POST", "https://api.tailscale.com/api/v2/oauth/token")
        assert kwargs["data"] == {
            "grant_type": "client_credentials", "scope": "services devices:core",
            "client_id": "some-client-id", "client_secret": "test-client-secret",
        }
        assert "Authorization" not in kwargs["headers"]
    for index, token in ((1, "first-access-token"), (2, "first-access-token"), (4, "second-access-token")):
        _, kwargs = session.calls[index]
        assert kwargs["headers"]["Authorization"] == f"Bearer {token}"
        assert "data" not in kwargs and "params" not in kwargs
    output = capsys.readouterr()
    assert "test-client-secret" not in output.out + output.err
    assert "access-token" not in output.out + output.err


def test_oauth_renews_between_retries_after_a_long_rate_limit_wait(monkeypatch):
    now = [1000]
    monkeypatch.setattr("remote_sync.http.time.monotonic", lambda: now[0])
    monkeypatch.setattr("remote_sync.http.time.sleep", lambda delay: now.__setitem__(0, now[0] + delay))
    provider, session = oauth_provider(monkeypatch,
        Response(200, {"access_token": "first-access-token", "expires_in": 3600}),
        Response(429, headers={"Retry-After": "3601"}),
        Response(200, {"access_token": "second-access-token", "expires_in": 3600}),
        Response(200, {"vipServices": []}),
    )
    assert provider.list() == []
    assert [args[0] for args, _ in session.calls] == ["POST", "GET", "POST", "GET"]
    assert session.calls[1][1]["headers"]["Authorization"] == "Bearer first-access-token"
    assert session.calls[3][1]["headers"]["Authorization"] == "Bearer second-access-token"


@pytest.mark.parametrize("response, error", [
    (Response(400, {"error": "test-client-secret"}), AssertionError),
    (requests.Timeout("test-client-secret"), RuntimeError),
])
def test_oauth_failure_stops_before_service_calls_without_exposing_secret(monkeypatch, capsys, response, error):
    provider, session = oauth_provider(monkeypatch, response)
    with pytest.raises(error) as exc:
        provider.list()
    assert len(session.calls) == 1
    output = capsys.readouterr()
    assert "test-client-secret" not in str(exc.value) + output.out + output.err
