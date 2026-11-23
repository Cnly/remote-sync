from copy import deepcopy
import json
import sys

import pytest

from remote_sync import cli
from remote_sync.http import APIStatusError
from remote_sync.providers.cloudflare import Cloudflare
from test_http import Response


ACCOUNT = "account-a"
ZONE = "example.com"
EXTRA = "extra.net"
HIDDEN = "hidden.org"
BASE = f"/accounts/{ACCOUNT}/workers"
DOMAINS = f"{BASE}/domains"


def route(id, pattern, script):
    return {"id": id, "pattern": pattern, "script": script}


def domain(id, host, script, zone=ZONE, **extra):
    return {"id": id, "hostname": host, "service": script, "zone_name": zone, "zone_id": f"id-{zone}",
            "environment": "production", "enabled": True, "previews_enabled": False} | extra


def worker(name, *, routes=None, domains=None, account_id=ACCOUNT):
    return {"name": name, "account_id": account_id, "zoneRoutes": routes or {}, "customDomains": domains or {}}


def error(status):
    return APIStatusError(Response(status, {"errors": [{"code": 10000, "message": "denied or unavailable"}]}))


class WorkersAPI:
    def __init__(self):
        self.calls = []
        self.denied = set()
        self.denied_zone_names = set()
        self.denied_requests = set()
        self.broken = {}
        self.zones = {ZONE: ACCOUNT, EXTRA: ACCOUNT}
        self.accounts = [ACCOUNT]
        self.routes = {ZONE: [route("sso", "https://sso.example.com/", "sso"),
                              route("matrix", "example.com/.well-known/matrix/*", "matrix"),
                              route("other", "example.com/other", "other"),
                              route("bypass", "example.com/bypass", None)],
                       EXTRA: [route("stale", "extra.net/*", "sso")],
                       HIDDEN: [route("hidden", "hidden.org/*", "hidden-route-worker")]}
        self.domains = [domain("account", "account.example.com", "sso"),
                        domain("hidden-domain", "private.hidden.org", "hidden-domain-worker", HIDDEN),
                        domain("foreign", "www.example.com", "other")]
        self.environment = "production"
        self.next_id = 0

    def request(self, method, path, *, params=None, json=None):
        params = params or {}
        self.calls.append((method, path, dict(params), deepcopy(json)))
        if path in self.denied:
            raise error(403)
        if (method, path) in self.denied_requests:
            raise error(403)
        if path in self.broken:
            raise error(self.broken[path])
        if path == "/accounts":
            result = [{"id": a} for a in self.accounts]
        elif path == "/zones":
            zones = [{"name": z, "id": f"id-{z}", "account": {"id": a}} for z, a in self.zones.items()]
            if "name" in params:
                if params["name"] in self.denied_zone_names:
                    raise error(403)
                result = [z for z in zones
                          if z["name"] == params["name"] and z["account"]["id"] == params["account.id"]]
            else:
                assert params["order"] == "account.id" and "page" not in params
                zones.sort(key=lambda z: z["account"]["id"], reverse=params["direction"] == "desc")
                result = zones[:params["per_page"]]
        elif path.startswith("/zones/") and path.count("/") == 2:
            assert method == "GET"
            zone_id = path.split("/")[2]
            zone = zone_id.removeprefix("id-")
            if zone not in self.zones:
                raise error(404)
            result = {"id": zone_id, "name": zone, "account": {"id": self.zones[zone]}}
        elif "/services/" in path:
            name = path.split("/services/")[1].split("/")[0]
            if path.endswith("/routes"):
                assert path.endswith(f"/environments/{self.environment}/routes") and params["show_zonename"] == "true"
                result = [r | {"zone_name": z} for z, rows in self.routes.items() for r in rows
                          if r["script"] == name and r.get("environment", self.environment) == self.environment]
                page = params.get("page", 1)
                return {"success": True, "result": result[page - 1:page],
                        "result_info": {"page": page, "total_pages": len(result)}}
            else:
                result = {"default_environment": {"environment": self.environment}}
        elif path.startswith("/zones/"):
            zone = path.split("/")[2].removeprefix("id-")
            rows = self.routes[zone]
            if method == "POST":
                assert not any(r["pattern"] == json["pattern"] for r in rows)
                self.next_id += 1
                result = {"id": f"r-{self.next_id}"} | json
                rows.append(result)
            elif path.endswith("/routes"):
                result = rows
            else:
                result = next(r for r in rows if r["id"] == path.rsplit("/", 1)[1])
                if method == "DELETE":
                    rows.remove(result)
        elif path.endswith("/workers/domains"):
            if method == "GET":
                result = [d for d in self.domains if all(d.get(k) == v for k, v in params.items())]
            else:
                assert method == "PUT"
                result = next((d for d in self.domains if d["hostname"] == json["hostname"]), None)
                if not result:
                    self.next_id += 1
                    result = domain(f"d-{self.next_id}", json["hostname"], json["service"],
                                    json["zone_id"].removeprefix("id-"))
                    self.domains.append(result)
                result.update(json)
        elif "/workers/domains/" in path:
            result = next(d for d in self.domains if d["id"] == path.rsplit("/", 1)[1])
            if method == "DELETE":
                self.domains.remove(result)
        else:
            raise AssertionError(f"Unexpected {method} {path}")
        return {"success": True, "result": deepcopy(result)}

    @property
    def writes(self):
        return [(m, p) for m, p, _, _ in self.calls if m != "GET"]


def sync(api, workers, **flags):
    collections = Cloudflare(api=api, workers=workers).collections(owner=None, declarations={}, adoptions={})
    cli.reconcile(collections, owner=None, **flags)


def test_scoped_worker_routes_domains_preview_apply_and_repeat(capsys):
    api = WorkersAPI()
    workers = {"redirect": worker("sso", domains={"account.example.com": {}}),
               "matrix": worker("matrix", routes={ZONE: ["example.com/.well-known/matrix/*"]})}
    sync(api, workers)
    preview = capsys.readouterr()
    assert "DELETE (workers/redirect) route/example.com/https://sso.example.com/" in preview.out
    assert "DELETE (workers/redirect) route/extra.net/extra.net/*" in preview.out
    assert HIDDEN not in [params.get("name") for _, p, params, _ in api.calls if p == "/zones"]
    assert f"/zones/id-{HIDDEN}/workers/routes" not in [p for _, p, _, _ in api.calls]
    assert [p for _, p, _, _ in api.calls].count(f"/zones/id-{ZONE}/workers/routes") == 1
    assert not api.writes
    sync(api, workers, doit=True)
    assert set(api.writes) == {("DELETE", f"/zones/id-{ZONE}/workers/routes/sso"),
                               ("DELETE", f"/zones/id-{EXTRA}/workers/routes/stale")}
    assert [r["id"] for r in api.routes[ZONE]] == ["matrix", "other", "bypass"]
    assert [d["id"] for d in api.domains] == ["account", "hidden-domain", "foreign"]
    api.calls.clear()
    sync(api, workers, doit=True)
    assert not api.writes


def test_missing_maps_clear_last_domain_and_routes_for_just_one_worker():
    api = WorkersAPI()
    sync(api, {"sso": worker("sso")}, doit=True)
    assert ("DELETE", f"{DOMAINS}/account") in api.writes
    assert {d["id"] for d in api.domains} == {"hidden-domain", "foreign"}
    assert {r["id"] for r in api.routes[ZONE]} == {"other", "bypass", "matrix"}


@pytest.mark.parametrize("target", [
    worker("hidden-route-worker"),
    worker("hidden-domain-worker"),
    worker("new", routes={HIDDEN: ["hidden.org/new"]}),
    worker("new", domains={"new.hidden.org": {"zone_name": HIDDEN}}),
], ids=["live-route", "live-domain", "declared-route", "declared-domain"])
def test_invisible_managed_zone_stops_discovery_before_writes(target):
    api = WorkersAPI()
    with pytest.raises(AssertionError, match=HIDDEN):
        sync(api, {"matrix": worker("matrix"), "target": target}, doit=True)
    assert not api.writes


def test_worker_create_update_list_and_idempotence(capsys):
    api = WorkersAPI()
    config = {"sso": worker("sso", routes={ZONE: ["example.com/new"]}, domains={
        "account.example.com": {"enabled": False, "previews_enabled": True},
        "new.extra.net": {"zone_id": f"id-{EXTRA}"}})}
    sync(api, config, list_only=True)
    listed = json.loads(capsys.readouterr().out)["workers/sso"]
    assert set(listed) == {"sso", "stale", "account"}
    sync(api, config, doit=True)
    assert ("POST", f"/zones/id-{ZONE}/workers/routes") in api.writes and api.writes.count(("PUT", DOMAINS)) == 2
    assert [(d["enabled"], d["previews_enabled"]) for d in api.domains if d["id"] == "account"] == [(False, True)]
    api.calls.clear()
    sync(api, config, doit=True)
    assert not api.writes


@pytest.mark.parametrize(("routes", "domains", "error_text"), [
    ({ZONE: ["example.com/other"]}, {}, "another|other"),
    ({ZONE: ["example.com/bypass"]}, {}, "bypass"),
    ({}, {"www.example.com": {}}, "another Worker"),
])
def test_collisions_abort_before_any_writes(routes, domains, error_text):
    api = WorkersAPI()
    with pytest.raises(AssertionError, match=error_text):
        sync(api, {"first": worker("matrix"), "sso": worker("sso", routes=routes, domains=domains)}, doit=True)
    assert not api.writes


@pytest.mark.parametrize("status", [403, 500])
def test_managed_zone_route_read_error_stops_before_writes(status):
    api = WorkersAPI()
    api.broken[f"/zones/id-{EXTRA}/workers/routes"] = status
    with pytest.raises(RuntimeError, match=f"HTTP {status}"):
        sync(api, {"sso": worker("sso")}, doit=True)
    assert not api.writes


def test_account_resolution_and_duplicate_workers(monkeypatch):
    api = WorkersAPI()
    monkeypatch.delenv("CLOUDFLARE_ACCOUNT_ID", raising=False)
    api.accounts.append("second-account")
    with pytest.raises(AssertionError, match="account is ambiguous"):
        sync(api, {"sso": worker("sso", account_id=None)}, doit=True)
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", ACCOUNT)
    with pytest.raises(AssertionError, match="claimed by both"):
        sync(api, {"one": worker("sso"), "two": worker("sso", account_id=None)}, doit=True)
    assert not api.writes


def test_reassigned_binding_stops_deletion():
    api = WorkersAPI()
    collection = Cloudflare(api=api, workers={"sso": worker("sso")}).collections(
        owner=None, declarations={}, adoptions={})["workers/sso"]
    binding = next(b for b in collection.provider.list() if b.id == "sso")
    api.routes[ZONE][0]["script"] = "someone-else"
    with pytest.raises(AssertionError, match="changed ownership"):
        collection.provider.delete(binding)
    assert not api.writes


def test_route_reported_by_worker_must_be_verifiable_in_zone():
    api = WorkersAPI()
    collections = Cloudflare(api=api, workers={"sso": worker("sso")}).collections(
        owner=None, declarations={}, adoptions={})
    assert "https://sso.example.com/" in collections["workers/sso"].provider.scope[1][ZONE]
    api.routes[ZONE].pop(0)
    with pytest.raises(AssertionError, match="Cannot verify route https://sso.example.com/"):
        cli.reconcile(collections, owner=None, doit=True)
    assert not api.writes


def test_worker_lookup_uses_default_environment_and_skips_unrelated_zones():
    api = WorkersAPI()
    api.environment = "blue"
    api.routes[ZONE].append(route("other-env", "example.com/green/*", "matrix") | {"environment": "green"})
    api.domains.append(domain("green-domain", "green.example.com", "matrix", environment="green"))
    sync(api, {"matrix": worker("matrix")}, doit=True)
    assert api.writes == [("DELETE", f"/zones/id-{ZONE}/workers/routes/matrix")]
    assert [params["name"] for _, path, params, _ in api.calls if path == "/zones"] == [ZONE]
    assert not any(p == f"/zones/id-{EXTRA}/workers/routes" for _, p, _, _ in api.calls)


def test_account_discovery_checks_only_first_and_last_visible_accounts(monkeypatch):
    api = WorkersAPI()
    api.denied.add("/accounts")
    api.zones.update({f"zone-{i}.test": ACCOUNT for i in range(30)})
    monkeypatch.delenv("CLOUDFLARE_ACCOUNT_ID", raising=False)
    sync(api, {"matrix": worker("matrix", account_id=None)}, list_only=True)
    assert any(p == f"{BASE}/services/matrix" for _, p, _, _ in api.calls)
    assert [params for _, p, params, _ in api.calls if p == "/zones" and "name" not in params] == [
        {"order": "account.id", "direction": direction, "per_page": 5} for direction in ("asc", "desc")]
    api.zones["second.test"] = "account-b"
    with pytest.raises(AssertionError, match="account is ambiguous"):
        sync(api, {"matrix": worker("matrix", account_id=None)}, doit=True)
    assert not api.writes


def test_denied_worker_domain_inventory_stops_before_writes():
    api = WorkersAPI()
    api.denied.add(DOMAINS)
    with pytest.raises(RuntimeError, match="HTTP 403"):
        sync(api, {"sso": worker("sso")}, doit=True)
    domain_queries = [params for _, p, params, _ in api.calls if p == DOMAINS]
    assert domain_queries == [{"service": "sso", "environment": "production"}]
    assert not api.writes


def test_worker_with_no_bindings_does_not_read_zones():
    api = WorkersAPI()
    sync(api, {"unbound": worker("unbound")}, doit=True)
    assert not any(path.startswith("/zones") for _, path, _, _ in api.calls)
    assert not api.writes


@pytest.mark.parametrize(("options", "zone"), [({}, "sub.example.com"), ({"zone_id": f"id-{ZONE}"}, ZONE)])
def test_new_domain_resolves_only_enclosing_zones(options, zone):
    api = WorkersAPI()
    api.zones["sub.example.com"] = ACCOUNT
    if "zone_id" in options:
        api.denied_zone_names.add("sub.example.com")
    host = "app.sub.example.com"
    sync(api, {"new": worker("new", domains={host: options})}, doit=True)
    ids = [body["zone_id"] for method, path, _, body in api.calls if (method, path) == ("PUT", DOMAINS)]
    assert ids == [f"id-{zone}"]
    names = [params["name"] for _, path, params, _ in api.calls if path == "/zones"]
    assert names == ([host, "sub.example.com"] if not options else [])
    if "zone_id" in options:
        assert [p for _, p, _, _ in api.calls if p.startswith("/zones/")] == [f"/zones/id-{ZONE}"]


def test_forbidden_subzone_lookup_does_not_attach_domain_to_parent():
    api = WorkersAPI()
    api.denied_zone_names.add("sub.example.com")
    with pytest.raises(RuntimeError, match="HTTP 403"):
        sync(api, {"new": worker("new", domains={"app.sub.example.com": {}})}, doit=True)
    assert [params["name"] for _, path, params, _ in api.calls if path == "/zones"] == [
        "app.sub.example.com", "sub.example.com"]
    assert not api.writes


def test_unavailable_domain_destination_fails_before_writes():
    api = WorkersAPI()
    with pytest.raises(RuntimeError, match="GET /zones/invisible"):
        sync(api, {"sso": worker("sso", domains={"account.example.com": {"zone_id": "invisible"}})}, doit=True)
    assert not api.writes


@pytest.mark.parametrize(("method", "path"), [
    ("GET", f"/zones/id-{ZONE}/workers/routes/sso"),
    ("DELETE", f"/zones/id-{ZONE}/workers/routes/sso"),
    ("POST", f"/zones/id-{ZONE}/workers/routes"),
    ("PUT", DOMAINS),
    ("DELETE", f"{DOMAINS}/account"),
])
def test_forbidden_binding_request_stops_apply(tmp_path, monkeypatch, capsys, method, path):
    api = WorkersAPI()
    api.denied_requests.add((method, path))
    workers = {"sso": worker("sso", routes={ZONE: ["example.com/new"]}, domains={"new.example.com": {}})}
    config = tmp_path / "cloudflare.json"
    config.write_text(json.dumps({"owner": None, "resources": {}, "settings": {"workers": workers}}))
    monkeypatch.setitem(cli.PROVIDERS, "cloudflare", lambda **settings: Cloudflare(api=api, **settings))
    monkeypatch.setattr(sys, "argv", ["remote-sync", "cloudflare", "--config-file", str(config), "--doit"])

    assert cli.main() == 1
    output = capsys.readouterr()
    assert f"Error: Cloudflare {method} {path}" in output.err and "HTTP 403" in output.err
    assert "Applied." not in output.out
    assert api.calls[-1][:2] == (method, path)

    api.denied_requests.clear()
    assert cli.main() == 0
    assert "Applied." in capsys.readouterr().out
    assert [r["pattern"] for r in api.routes[ZONE] if r["script"] == "sso"] == ["example.com/new"]
    assert {d["hostname"] for d in api.domains if d["service"] == "sso"} == {"new.example.com"}


@pytest.mark.parametrize("status", [401, 403, 404, 503])
@pytest.mark.parametrize("suffix", ["", "/environments/production/routes"])
def test_worker_lookup_errors_stop_discovery(status, suffix):
    api = WorkersAPI()
    path = f"{BASE}/services/sso{suffix}"
    api.broken[path] = status
    with pytest.raises(RuntimeError, match=f"HTTP {status}"):
        sync(api, {"sso": worker("sso")}, doit=True)
    assert api.calls[-1][1] == path
    assert not any(p.startswith("/zones") for _, p, _, _ in api.calls)
    assert not api.writes


def test_dns_case_and_trailing_dot_keep_existing_bindings():
    api = WorkersAPI()
    sync(api, {"sso": worker("sso", routes={"EXAMPLE.COM.": ["https://sso.example.com/"]},
                             domains={"Account.Example.Com.": {"zone_name": "EXAMPLE.COM."}})}, doit=True)
    assert api.writes == [("DELETE", f"/zones/id-{EXTRA}/workers/routes/stale")]


def test_same_script_name_in_explicit_accounts_has_independent_ownership(monkeypatch):
    apis = {ACCOUNT: WorkersAPI(), "account-b": WorkersAPI()}
    other = apis["account-b"]
    other.accounts = ["account-b"]
    other.zones = {"second.test": "account-b"}
    other.routes = {"second.test": [route("second", "second.test/*", "sso")]}
    other.domains = []

    class MultiAccountAPI:
        def request(self, method, path, *, params=None, **kwargs):
            if path == "/zones":
                account_id = params["account.id"]
            elif path.startswith("/accounts/"):
                account_id = path.split("/")[2]
            else:
                account_id = "account-b" if path.startswith("/zones/id-second.test/") else ACCOUNT
            return apis[account_id].request(method, path, params=params, **kwargs)

    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", ACCOUNT)
    sync(MultiAccountAPI(), {"one": worker("sso", account_id=None), "two": worker(
        "sso", account_id="account-b", routes={"second.test": ["second.test/*"]})}, doit=True)
    assert apis[ACCOUNT].writes and not other.writes
    assert other.routes["second.test"][0]["id"] == "second"
