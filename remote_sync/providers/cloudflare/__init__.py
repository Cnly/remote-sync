"""Zone rules managed through Cloudflare's v4 Rulesets API."""

import os
from functools import cached_property
from urllib.parse import quote

from .. import Collection, Provider
from ...http import API, APIStatusError
from ...resources import RemoteResource
from ...utils import progress


PHASES = {
    "http_request_dynamic_redirect", "http_request_cache_settings", "http_config_settings",
    "http_request_transform", "http_request_late_transform", "http_response_headers_transform",
    "http_request_origin", "http_request_firewall_custom", "http_ratelimit",
}
OPTIONAL_FIELDS = {"action_parameters", "ratelimit", "logging", "exposed_credential_check"}


def rule_settings(rule):
    settings = {
        "ref": rule.get("ref") or rule["id"],
        "action": rule["action"], "expression": rule["expression"],
        "description": rule.get("description", ""), "enabled": rule.get("enabled", True),
    }
    for field in OPTIONAL_FIELDS:
        if rule.get(field):
            settings[field] = rule[field]
    params = settings.get("action_parameters", {})
    if settings["action"] == "redirect" and "from_value" in params:
        defaults = {"status_code": 301, "preserve_query_string": False}
        settings["action_parameters"] = params | {"from_value": defaults | params["from_value"]}
    return settings


def declared_rule(rule, owner):
    unknown = rule.keys() - ({"key", "expression", "action", "description", "enabled"} | OPTIONAL_FIELDS)
    assert not unknown, f"Unknown Cloudflare rule fields: {', '.join(sorted(unknown))}"
    key = rule["key"]
    assert isinstance(key, str) and key and "/" not in key, f"Invalid Cloudflare rule key: {key!r}"
    assert all(isinstance(rule.get(k), str) and rule[k] for k in ("expression", "action")), (
        "A Cloudflare rule needs an expression and action."
    )
    assert isinstance(rule.get("enabled", True), bool), "Cloudflare rule enabled must be a boolean."
    assert isinstance(rule.get("description", ""), str), "Cloudflare rule description must be a string."
    for field in OPTIONAL_FIELDS & rule.keys():
        assert isinstance(rule[field], dict), f"Cloudflare rule {field} must be an object."
    return rule_settings(rule | {"ref": RemoteResource.format_ownership_marker(owner, key)})


class Ruleset(RemoteResource):
    identity_field = "phase"

    def __init__(self, data, owner, adoptions):
        super().__init__(data)
        self.owner = owner
        self.adoptions = adoptions

    def ownership_values(self):
        # DiffSync manages this owner's rules within the configured phase.
        # The entry point itself is shared and never receives an ownership marker.
        return [self.format_ownership_marker(self.owner, self["phase"])]

    @cached_property
    def managed(self):
        """Owned and explicitly adopted rules, keyed by their intended ref in live order."""
        result = {}
        ids = set()
        for rule in self.get("rules", []):
            rule_id = rule["id"]
            assert rule_id not in ids, f"Duplicate Cloudflare rule ID {rule_id}."
            ids.add(rule_id)
            ref = rule.get("ref") or rule_id
            marker = RemoteResource.parse_ownership_marker(ref)
            if rule_id in self.adoptions:
                target = self.adoptions[rule_id]
                assert marker is None or ref == target, f"{rule_id} already belongs to {ref}."
                ref = target
            elif marker is None or marker[0] != self.owner:
                continue
            assert ref not in result, f"Multiple owned or adopted rules for {ref}."
            result[ref] = rule
        missing = self.adoptions.keys() - ids
        assert not missing, f"Adoption IDs not found in {self['phase']}: {', '.join(sorted(missing))}"
        return result

    def normalize(self):
        return super().normalize() | {
            "phase": self["phase"], "rules": [rule_settings(rule) for rule in self.managed.values()],
        }

    def merge_rules(self, desired):
        """Fill surviving managed slots in declared order, then append overflow."""
        replacements = {}
        for rule in desired:
            ref = rule["ref"]
            current = self.managed.get(ref)
            if current is None:
                replacements[ref] = rule
            else:
                body = {"id": current["id"]}
                if rule_settings(current) != rule:
                    body.update(rule)
                replacements[ref] = body

        refs_by_id = {rule["id"]: ref for ref, rule in self.managed.items()}
        ordered = iter(replacements.values())
        merged = []
        for rule in self.get("rules", []):
            ref = refs_by_id.get(rule["id"])
            if ref is None:
                merged.append({"id": rule["id"]})
            elif ref in replacements:
                merged.append(next(ordered))
        merged.extend(ordered)
        return merged


class Cloudflare:
    def __init__(self, *, api=None):
        self.api = api or API("https://api.cloudflare.com/client/v4", os.environ["CLOUDFLARE_API_TOKEN"])

    def request(self, method, path, *, missing_entrypoint=False, **kwargs):
        try:
            response = self.api.request(method, path, **kwargs)
        except APIStatusError as exc:
            try:
                errors = exc.response.json()["errors"]
            except (ValueError, KeyError, TypeError):
                raise RuntimeError(f"Cloudflare {method} {path}: {exc}") from None
            if (missing_entrypoint and exc.response.status_code == 404 and
                    [error.get("code") for error in errors] == [10003]):
                return None
            detail = ", ".join(f"{error.get('code')}: {error.get('message')}" for error in errors)
            raise RuntimeError(f"Cloudflare {method} {path}: {exc} {detail}") from None
        assert response.get("success") is True, f"Cloudflare {method} {path} failed: {response.get('errors')}"
        return response

    def zone_id(self, name):
        page = self.request("GET", "/zones", params={"name": name, "per_page": 50})
        zones = page["result"]
        info = page.get("result_info", {})
        assert (len(zones) == 1 and zones[0]["name"].lower() == name and
                info.get("total_count", 1) == 1 and info.get("total_pages", 1) == 1), (
            f"Expected one visible Cloudflare zone named {name}; check the name and token scope."
        )
        return zones[0]["id"]

    def collections(self, *, owner, declarations, adoptions):
        assert owner and "--" not in owner, "Cloudflare owner must be nonempty and cannot contain '--'."
        collections = {}
        for zone, config in declarations.items():
            assert zone and zone == zone.lower().rstrip(".") and "/" not in zone, (
                f"Use the lowercase zone name without a trailing dot: {zone}"
            )
            phases = config["phases"]
            declared_phases = {}
            for phase, scope in phases.items():
                assert phase in PHASES, f"Unsupported Cloudflare phase: {phase}"
                rules = scope["rules"]
                assert isinstance(rules, list), f"Expected an ordered rule array in {zone}/{phase}. Regenerate the config."
                if rules:
                    declared_phases[phase] = {"phase": phase, "rules": rules}
            if phases:
                collections[zone] = Collection(ZoneRulesets(self, zone, owner, phases), declared_phases)
        for path, rule_id in adoptions.items():
            zone, _, rest = path.partition("/")
            phase, _, key = rest.partition("/")
            collection = collections.get(zone)
            assert collection and phase in collection.declarations and any(
                rule["key"] == key for rule in collection.declarations[phase]["rules"]
            ), f"Use --adopt ZONE/PHASE/KEY=RULE_ID for a declared Cloudflare rule: {path}"
            targets = collection.provider.adoptions.setdefault(phase, {})
            assert rule_id not in targets, "Cannot adopt one rule ID under multiple keys."
            targets[rule_id] = RemoteResource.format_ownership_marker(owner, key)
        return collections


class ZoneRulesets(Provider):
    resource_type = Ruleset

    def __init__(self, client, zone, owner, phases):
        self.client, self.zone, self.owner = client, zone, owner
        self.phases = phases
        self.adoptions = {}
        self.rulesets = {}

    def list(self):
        zone_id = self.client.zone_id(self.zone)
        self.base = f"/zones/{quote(zone_id, safe='')}/rulesets"
        self.rulesets.clear()
        for phase in self.phases:
            progress(f"  Reading {self.zone}/{phase}...", detail=True)
            response = self.client.request("GET", f"{self.base}/phases/{phase}/entrypoint", missing_entrypoint=True)
            adoptions = self.adoptions.get(phase, {})
            if response is None:
                assert not adoptions, f"Cannot adopt a rule in missing entry point {self.zone}/{phase}."
                continue
            data = response["result"]
            assert data["phase"] == phase and data["kind"] == "zone", f"Unexpected entry point for {self.zone}/{phase}."
            self.rulesets[phase] = Ruleset(data, self.owner, adoptions)
        return list(self.rulesets.values())

    def desired(self, declaration):
        rules = [declared_rule(rule, self.owner) for rule in declaration["rules"]]
        assert len({rule["ref"] for rule in rules}) == len(rules), "Duplicate Cloudflare rule key in phase."
        return {"phase": declaration["phase"], "rules": rules}

    def absent_settings(self, settings):
        # An explicitly empty phase clears owned rules while retaining the shared entry point.
        return settings | {"rules": []}

    def resolve_identity(self, key, declaration, resource, inventory):
        return resource

    def write(self, settings, *, dry_run=False):
        phase, rules = settings["phase"], settings["rules"]
        params = {"dry_run": "true"} if dry_run else {}
        if phase in self.rulesets:
            ruleset = self.rulesets[phase]
            path = f"{self.base}/{quote(ruleset.id, safe='')}"
            return self.client.request("PUT", path, params=params, json={"rules": ruleset.merge_rules(rules)})
        body = {"kind": "zone", "phase": phase, "name": phase, "rules": rules}
        return self.client.request("POST", self.base, params=params, json=body)

    def validate_change(self, key, settings):
        progress(f"Validating {self.zone}/{key} with Cloudflare...")
        self.write(settings, dry_run=True)

    def create(self, settings):
        return self.write(settings)["result"]["id"]

    def update(self, resource, settings):
        self.write(settings)

    adopt = update
