from copy import deepcopy
import json

import pytest

from remote_sync.cli import reconcile
from remote_sync.http import API, APIStatusError
from remote_sync.providers.cloudflare import Cloudflare
from test_http import Response, Session


CACHE = "http_request_cache_settings"
REDIRECT = "http_request_dynamic_redirect"
ZONE = "example.com"
BASE = "/zones/zone-id/rulesets"
SPEC = {"action": "set_cache_settings", "expression": "true", "action_parameters": {"cache": True}}


def entry(key, *, owner="site", **overrides):
    return SPEC | {"id": f"id-{key}", "ref": f"nix-sync-{owner}--{key}" if owner else f"id-{key}",
                   "version": "2", "last_updated": "2025-10-01T00:00:00Z"} | overrides


def rules(*keys):
    return [SPEC | {"key": key} for key in keys]


class CloudflareAPI:
    """Model ruleset replacement, including ID-only references to retained rules."""

    def __init__(self, **phases):
        self.phases = phases
        self.calls = []
        self.next_id = 1
        self.zones = [{"name": ZONE, "id": "zone-id"}]
        self.fail_validation = self.fail_write = None

    def result(self, value):
        return {"success": True, "result": deepcopy(value), "errors": []}

    def request(self, method, path, *, params=None, json=None):
        self.calls.append((method, path, params, deepcopy(json)))
        if path == "/zones":
            assert method == "GET" and params["name"] == ZONE
            return self.result(self.zones)
        if method == "GET":
            phase = path.split("/")[-2]
            assert path == f"{BASE}/phases/{phase}/entrypoint"
            if phase not in self.phases:
                raise APIStatusError(Response(404, {"errors": [{"code": 10003}]}))
        else:
            if method == "POST":
                phase = json["phase"]
                assert path == BASE and json["kind"] == "zone" and phase not in self.phases
            else:
                phase = path.split("/")[-1].removeprefix("rs-")
                assert method == "PUT" and path == f"{BASE}/rs-{phase}" and phase in self.phases
            if params == {"dry_run": "true"}:
                if phase == self.fail_validation:
                    raise APIStatusError(Response(400, {"errors": [{"code": 20127, "message": "Invalid expression"}]}))
                return self.result(None)
            if phase == self.fail_write:
                raise RuntimeError("interrupted write")
            existing = {r["id"]: r for r in self.phases.get(phase, [])}
            updated = []
            for body in json["rules"]:
                if "id" in body:
                    updated.append(existing[body["id"]] | body)
                else:
                    updated.append(body | {"id": f"new-{self.next_id}", "version": "1"})
                    self.next_id += 1
            self.phases[phase] = updated
        return self.result({"id": f"rs-{phase}", "kind": "zone", "phase": phase, "rules": self.phases[phase]})

    @property
    def mutations(self):
        return [call for call in self.calls if call[0] != "GET"]


def sync(api, phases, *, adoptions=None, **flags):
    collections = Cloudflare(api=api).collections(
        owner="site", declarations={ZONE: {"phases": {p: {"rules": r} for p, r in phases.items()}}},
        adoptions=adoptions or {},
    )
    reconcile(collections, owner="site", **flags)


def test_one_phase_diff_and_write_for_edits_removals_reordering_and_adoption(capsys):
    manual = entry("dashboard", owner=None, description="Manual rule", logging={"enabled": True})
    foreign = entry("other", owner="elsewhere")
    api = CloudflareAPI(**{CACHE: [entry("late"), manual, entry("z"), entry("old", owner=None),
                                   foreign, entry("a"), entry("retired")]})
    config = {CACHE: rules("a", "adopted", "new-a", "new-b", "z") + [SPEC | {"key": "late", "enabled": False}]}
    adopt = {f"{ZONE}/{CACHE}/adopted": "id-old"}
    sync(api, config, adoptions=adopt)
    preview = capsys.readouterr().out
    assert f"UPDATE ({ZONE}) {CACHE}" in preview and f"Update (1):\n    {CACHE}" in preview
    assert all(ref in preview for ref in ("id-old", "nix-sync-site--adopted", "nix-sync-site--retired"))
    assert not api.mutations

    sync(api, config, adoptions=adopt, doit=True)
    dry_run, write = api.mutations
    assert dry_run == ("PUT", f"{BASE}/rs-{CACHE}", {"dry_run": "true"}, write[3])
    assert write[:3] == ("PUT", f"{BASE}/rs-{CACHE}", {})
    live = api.phases[CACHE]
    assert [r["ref"] for r in live] == [
        "nix-sync-site--a", manual["ref"], "nix-sync-site--adopted", "nix-sync-site--new-a",
        foreign["ref"], "nix-sync-site--new-b", "nix-sync-site--z", "nix-sync-site--late",
    ]
    assert live[1] == manual and live[4] == foreign
    assert all(write[3]["rules"][i] == {"id": live[i]["id"]} for i in (0, 1, 4, 6))
    assert live[2]["id"] == "id-old" and live[-1]["id"] == "id-late" and live[-1]["enabled"] is False
    api.calls.clear()
    sync(api, config, adoptions=adopt, doit=True)
    assert "No changes." in capsys.readouterr().out and not api.mutations


def test_adoption_alone_changes_the_ref_on_the_selected_rule(capsys):
    api = CloudflareAPI(**{CACHE: [entry("manual", owner=None, ref="dashboard-ref")]})
    config = {CACHE: rules("files")}
    adopt = {f"{ZONE}/{CACHE}/files": "id-manual"}
    sync(api, config, adoptions=adopt, doit=True)
    output = capsys.readouterr().out
    assert "dashboard-ref" in output and "nix-sync-site--files" in output
    rule, = api.phases[CACHE]
    assert rule["id"] == "id-manual" and rule["ref"] == "nix-sync-site--files"
    api.calls.clear()
    sync(api, config, adoptions=adopt, doit=True)
    assert not api.mutations


def test_order_only_change_uses_array_order_and_preserves_existing_ids(capsys):
    manual = entry("manual", owner=None)
    api = CloudflareAPI(**{CACHE: [entry("a"), manual, entry("z")]})
    config = {CACHE: rules("z", "a")}
    sync(api, config, doit=True)
    expected = {"rules": [{"id": "id-z"}, {"id": "id-manual"}, {"id": "id-a"}]}
    assert api.mutations == [("PUT", f"{BASE}/rs-{CACHE}", params, expected) for params in ({"dry_run": "true"}, {})]
    assert api.phases[CACHE] == [entry("z"), manual, entry("a")]
    api.calls.clear()
    sync(api, config, doit=True)
    assert "No changes." in capsys.readouterr().out and not api.mutations


def test_list_and_empty_scope_only_clear_owned_rules(capsys):
    manual = entry("manual", owner=None)
    api = CloudflareAPI(**{CACHE: [manual, entry("gone")], REDIRECT: [entry("outside")]})
    sync(api, {CACHE: [], REDIRECT: []}, list_only=True)
    inventory = json.loads(capsys.readouterr().out)[ZONE]
    assert inventory[f"rs-{CACHE}"]["rules"] == [manual, entry("gone")]
    assert sum(path == "/zones" for _, path, _, _ in api.calls) == 1
    sync(api, {CACHE: []}, doit=True)
    assert api.phases == {CACHE: [manual], REDIRECT: [entry("outside")]}
    assert api.mutations[-1][3] == {"rules": [{"id": "id-manual"}]}
    api.calls.clear()
    sync(api, {CACHE: []}, doit=True)
    assert not api.mutations
    api = CloudflareAPI()
    sync(api, {CACHE: []}, doit=True)
    assert not api.phases and not api.mutations


def test_validate_all_changed_phases_before_first_write_and_create_missing_phase():
    redirect = {"key": "sso", "action": "redirect", "expression": "true", "action_parameters": {
        "from_value": {"target_url": {"value": "https://example.com/"}, "status_code": 302},
    }}
    api = CloudflareAPI(**{CACHE: []})
    config = {CACHE: rules("z", "a"), REDIRECT: [redirect]}
    api.fail_validation = REDIRECT
    with pytest.raises(RuntimeError, match="Invalid expression"):
        sync(api, config, doit=True)
    assert api.phases == {CACHE: []}
    assert [(m, params) for m, _, params, _ in api.mutations] == [
        ("PUT", {"dry_run": "true"}), ("POST", {"dry_run": "true"}),
    ]
    api.calls.clear()
    api.fail_validation = None
    sync(api, config, doit=True)
    assert [(m, params) for m, _, params, _ in api.mutations] == [
        ("PUT", {"dry_run": "true"}), ("POST", {"dry_run": "true"}), ("PUT", {}), ("POST", {}),
    ]
    assert [r["ref"] for r in api.phases[CACHE]] == ["nix-sync-site--z", "nix-sync-site--a"]
    del api.phases[REDIRECT][0]["action_parameters"]["from_value"]["preserve_query_string"]
    api.calls.clear()
    sync(api, config, doit=True)
    assert not api.mutations


def test_retry_only_writes_the_remaining_phase():
    api = CloudflareAPI()
    config = {CACHE: rules("a"), REDIRECT: [{"key": "sso", "expression": "true", "action": "redirect"}]}
    api.fail_write = REDIRECT
    with pytest.raises(RuntimeError, match="interrupted"):
        sync(api, config, doit=True)
    assert set(api.phases) == {CACHE}
    original = deepcopy(api.phases[CACHE])
    api.fail_write = None
    api.calls.clear()
    sync(api, config, doit=True)
    assert api.phases[CACHE] == original
    assert len(api.mutations) == 2 and all(body["phase"] == REDIRECT for _, _, _, body in api.mutations)


@pytest.mark.parametrize("count", [0, 2])
def test_zone_lookup_requires_one_match(count):
    api = CloudflareAPI()
    api.zones *= count
    with pytest.raises(AssertionError, match="one visible"):
        sync(api, {CACHE: rules("files")}, doit=True)
    assert len(api.calls) == 1


@pytest.mark.parametrize(("live", "keys", "adoptions", "error"), [
    ([entry("key", owner="other")], ["key"], {"key": "id-key"}, "already belongs"),
    ([entry("old")], ["key"], {"key": "id-old"}, "already belongs"),
    ([entry("key"), entry("key", id="another")], [], {}, "Multiple owned"),
    ([entry("key"), entry("free", owner=None)], ["key"], {"key": "id-free"}, "Multiple owned"),
    ([entry("free", owner=None)], ["a", "b"], {"a": "id-free", "b": "id-free"}, "multiple keys"),
    ([], ["key"], {"key": "outside"}, "not found"),
    ([], ["key", "key"], {}, "Duplicate Cloudflare rule key"),
    ([], ["key"], {"typo": "unused"}, "declared Cloudflare rule"),
])
def test_ambiguous_ownership_or_adoption_stops_before_writes(live, keys, adoptions, error):
    api = CloudflareAPI(**{CACHE: live})
    with pytest.raises(AssertionError, match=error):
        sync(api, {CACHE: rules(*keys)}, adoptions={f"{ZONE}/{CACHE}/{k}": v for k, v in adoptions.items()}, doit=True)
    assert not api.mutations


@pytest.mark.parametrize(("status", "code"), [(403, 10003), (404, 7003)])
def test_only_missing_entrypoint_is_treated_as_empty(status, code):
    session = Session(
        Response(200, {"success": True, "result": [{"id": "zone-id", "name": ZONE}]}),
        Response(status, {"errors": [{"code": code, "message": "denied"}]}),
    )
    with pytest.raises(RuntimeError, match=f"HTTP {status}"):
        sync(API("https://example.test", session=session), {CACHE: rules("files")}, doit=True)
    assert [args[0] for args, _ in session.calls] == ["GET", "GET"]
