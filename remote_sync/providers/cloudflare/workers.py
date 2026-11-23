"""Reconcile the ordinary routes and Custom Domains of explicitly declared Workers."""

from dataclasses import dataclass, field
from functools import cached_property
import os
from urllib.parse import quote

from .. import Provider
from ...resources import RemoteResource
from .client import CloudflareError


def dns_keys(entries):
    result = {name.lower().removesuffix("."): value for name, value in entries.items()}
    assert len(result) == len(entries), "Duplicate DNS name in Worker routing declarations."
    return result


class WorkerAccounts:
    def __init__(self, client):
        self.client = client
        self.accounts = {}

    @cached_property
    def default_id(self):
        try:
            ids = {a["id"] for a in self.client.pages("/accounts")}
        except CloudflareError as exc:
            if exc.status != 403:
                raise
            ids = set()
        if not ids:
            # The first and last account IDs suffice to detect multiple accounts.
            for direction in ("asc", "desc"):
                params = {"order": "account.id", "direction": direction, "per_page": 5}
                zones = self.client.request("GET", "/zones", params=params)["result"]
                ids.update(zone["account"]["id"] for zone in zones)
        assert len(ids) == 1, (
            "Set the Worker's settings.account_id or CLOUDFLARE_ACCOUNT_ID when the account is ambiguous."
        )
        return ids.pop()

    def get(self, account_id):
        account_id = account_id or os.environ.get("CLOUDFLARE_ACCOUNT_ID") or self.default_id
        if account_id not in self.accounts:
            self.accounts[account_id] = Account(self.client, account_id)
        return self.accounts[account_id]


class Account:
    def __init__(self, client, account_id):
        self.client = client
        self.id = account_id
        self.workers_path = f"/accounts/{quote(account_id, safe='')}/workers"
        self.domains_path = f"{self.workers_path}/domains"
        self.zone_cache = {}
        self.route_cache = {}
        self.claims = {}

    def claim(self, key, worker):
        assert key not in self.claims, f"{key} claimed by both {self.claims[key]} and {worker} in account {self.id}."
        self.claims[key] = worker

    def lookup_zone(self, name):
        """Return the exact zone ID, or None when the name lookup succeeds with no matches."""
        if name not in self.zone_cache:
            rows = self.client.pages("/zones", name=name, **{"account.id": self.id})
            if rows:
                assert len(rows) == 1 and rows[0]["name"] == name and rows[0]["account"]["id"] == self.id, (
                    f"Unexpected zone returned for {name} in account {self.id}."
                )
            self.zone_cache[name] = rows[0]["id"] if rows else None
        return self.zone_cache[name]

    def zone_id(self, name):
        zone_id = self.lookup_zone(name)
        assert zone_id, f"Cannot find zone {name} in account {self.id}. Check the zone name and token access."
        return zone_id

    def zone_name(self, zone_id):
        zone = self.client.request("GET", f"/zones/{quote(zone_id, safe='')}")["result"]
        assert zone["id"] == zone_id and zone["account"]["id"] == self.id, (
            f"Unexpected zone returned for {zone_id} in account {self.id}."
        )
        self.zone_cache[zone["name"]] = zone_id
        return zone["name"]

    def routes_path(self, zone):
        return f"/zones/{quote(self.zone_id(zone), safe='')}/workers/routes"

    def routes(self, zone):
        if zone not in self.route_cache:
            self.route_cache[zone] = self.client.pages(self.routes_path(zone))
        return self.route_cache[zone]


class Binding(RemoteResource):
    def __init__(self, remote_id, key, settings, owner):
        super().__init__(id=remote_id, **settings)
        self.key = key
        self.owner = owner

    @property
    def ownership(self):
        # Membership in this Worker's live routing inventory establishes ownership.
        return self.owner, self.key

    @classmethod
    def format_ownership_marker(cls, owner, key):
        return None

    def normalize(self):
        return {k: v for k, v in self.items() if k != "id"} | {"ownership": None}


@dataclass
class WorkerCollection:
    provider: "WorkerRouting"
    adoptions: dict = field(default_factory=dict)

    @property
    def declarations(self):
        # Inventory-only runs do not need to resolve desired bindings.
        return self.provider.declarations


class WorkerRouting(Provider):
    resource_type = Binding

    def __init__(self, accounts, key, config, owner):
        self.accounts, self.key, self.owner = accounts, key, owner
        self.account_id = config.get("account_id")
        self.name = config["name"]
        self.zone_patterns = dns_keys(config["zoneRoutes"])
        self.custom_domains = dns_keys(config["customDomains"])
        self.client = accounts.client

    @cached_property
    def account(self):
        return self.accounts.get(self.account_id)

    @cached_property
    def scope(self):
        # Wrangler's Worker config download reads the default environment, then
        # its route inventory. Only candidate zones need a zone-API request.
        service = f"{self.account.workers_path}/services/{quote(self.name, safe='')}"
        metadata = self.client.request("GET", service)["result"]
        environment = metadata["default_environment"]["environment"]
        assert environment, f"Missing default environment for Worker {self.name}."
        path = f"{service}/environments/{quote(environment, safe='')}/routes"
        routes = {}
        for row in self.client.pages(path, show_zonename="true"):
            zone, pattern = row["zone_name"], row["pattern"]
            assert zone and pattern, f"Missing zone name or pattern in routes for Worker {self.name}."
            routes.setdefault(zone, set()).add(pattern)
        return environment, routes

    def owns_route(self, zone, row):
        environment, routes = self.scope
        return (row.get("script") == self.name and row.get("environment", environment) == environment
                and row["pattern"] in routes.get(zone, ()))

    @cached_property
    def zone_routes(self):
        _, scoped = self.scope
        routes = {}
        for zone in sorted(scoped.keys() | self.zone_patterns.keys()):
            rows = self.account.routes(zone)
            routes[zone] = {row["pattern"]: row for row in rows}
            assert len(routes[zone]) == len(rows), f"Duplicate live Worker route patterns in {zone}."
        return routes

    def domain_zone(self, hostname, options, existing):
        zone = options.get("zone_name")
        zone_id = options.get("zone_id")
        if not zone and existing and zone_id in (None, existing["zone_id"]):
            return existing["zone_name"]
        if not zone and zone_id:
            zone = self.account.zone_name(zone_id)
        if zone:
            zone = zone.lower().removesuffix(".")
            assert hostname == zone or hostname.endswith("." + zone), f"{hostname} is outside zone {zone}."
            actual_id = self.account.zone_id(zone)
            assert zone_id in (None, actual_id), f"Conflicting zone ID and name for {hostname}."
            return zone
        # A delegated subzone can be more specific than the registrable domain.
        zone = hostname
        while zone:
            if self.account.lookup_zone(zone) is not None:
                return zone
            zone = zone.partition(".")[2]
        raise ValueError(f"Cannot find a zone for Custom Domain {hostname} in account {self.account.id}. "
                         "Check the hostname and token access.")

    @cached_property
    def domains(self):
        environment, _ = self.scope
        rows = self.client.pages(self.account.domains_path, service=self.name, environment=environment)
        by_host = {}
        for row in rows:
            if row["service"] != self.name or row["environment"] != environment:
                continue
            host = row["hostname"]
            assert host not in by_host, f"Ambiguous Custom Domain {host} for {self.name}."
            assert self.account.zone_id(row["zone_name"]) == row["zone_id"], (
                f"Unexpected zone ID for Custom Domain {host} in {row['zone_name']}."
            )
            by_host[host] = row
        return by_host

    def check_domain_available(self, host):
        # The account-wide PUT can reassign a hostname. Check all services,
        # including environments outside this Worker's own inventory.
        matches = self.client.pages(self.account.domains_path, hostname=host)
        assert not matches, f"Custom Domain {host} is already attached to another Worker or environment."

    def domain_settings(self, hostname, zone, options):
        return {"hostname": hostname, "service": self.name, "zone": zone,
                "enabled": options.get("enabled", True), "previews_enabled": options.get("previews_enabled", False)}

    @cached_property
    def declarations(self):
        self.account.claim(f"worker/{self.name}", self.key)
        desired = {}
        for zone, patterns in self.zone_patterns.items():
            for pattern in patterns:
                key = f"route/{zone}/{pattern}"
                self.account.claim(key, self.key)
                existing = self.zone_routes[zone].get(pattern)
                assert existing is None or self.owns_route(zone, existing), (
                    f"{zone}: route {pattern} is assigned to {existing.get('script') or 'a scriptless bypass'} "
                    "outside this Worker's routing scope."
                )
                desired[key] = {"zone": zone, "pattern": pattern, "script": self.name}

        for host, options in self.custom_domains.items():
            existing = self.domains.get(host)
            key = f"domain/{host}"
            self.account.claim(key, self.key)
            zone = self.domain_zone(host, options, existing)
            if existing is None:
                self.check_domain_available(host)
            desired[key] = self.domain_settings(host, zone, options)
        return desired

    def list(self):
        live = []
        _, scoped = self.scope
        for zone, rows in self.zone_routes.items():
            for pattern in sorted(scoped.get(zone, ())):
                row = rows.get(pattern)
                assert row and self.owns_route(zone, row), (
                    f"Cannot verify route {pattern} for Worker {self.name} in {zone}. Check access and preview again."
                )
                settings = {"zone": zone, "pattern": pattern, "script": self.name}
                live.append(Binding(row["id"], f"route/{zone}/{pattern}", settings, self.owner))
        for host, row in self.domains.items():
            settings = self.domain_settings(host, row["zone_name"], row)
            live.append(Binding(row["id"], f"domain/{host}", settings, self.owner))
        return live

    def desired(self, declaration):
        return declaration

    def resolve_identity(self, key, declaration, resource, inventory):
        return resource

    def put_domain(self, settings):
        body = {k: settings[k] for k in ("hostname", "service", "enabled", "previews_enabled")}
        body["zone_id"] = self.account.zone_id(settings["zone"])
        return self.client.request("PUT", self.account.domains_path, json=body)["result"]["id"]

    def create(self, settings):
        if "hostname" in settings:
            self.check_domain_available(settings["hostname"])
            return self.put_domain(settings)
        path = self.account.routes_path(settings["zone"])
        body = {k: settings[k] for k in ("pattern", "script")}
        return self.client.request("POST", path, json=body)["result"]["id"]

    def live_path(self, binding):
        is_domain = "hostname" in binding
        base = self.account.domains_path if is_domain else self.account.routes_path(binding["zone"])
        path = f"{base}/{quote(binding.id, safe='')}"
        row = self.client.request("GET", path)["result"]
        keys = ("hostname", "service") if is_domain else ("pattern", "script")
        same = row["id"] == binding.id and all(row.get(k) == binding[k] for k in keys)
        if is_domain:
            same = same and row["zone_id"] == self.account.zone_id(binding["zone"])
            environment, _ = self.scope
            same = same and row.get("environment") == environment
        else:
            same = same and self.owns_route(binding["zone"], row)
        assert same, f"Binding {binding.key} changed ownership since discovery; preview again."
        return path

    def update(self, resource, settings):
        self.live_path(resource)
        return self.put_domain(settings)

    adopt = update

    def delete(self, resource):
        self.client.request("DELETE", self.live_path(resource))
