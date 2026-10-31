"""Manages Grafana InhibitionRule resources through the notifications API.

Grafana discards custom metadata annotations on these resources. Their immutable
names therefore carry the ownership marker: nix-sync-<owner>--<declaration-key>.
"""

from copy import deepcopy
import re
from urllib.parse import quote

from .. import Provider
from .client import GrafanaClient
from ...resources import RemoteResource
from ...utils import progress


API_GROUP = "notifications.alerting.grafana.app"
FIELDS = ("source_matchers", "target_matchers", "equal")


def normalize_inhibition(spec):
    # Matchers are ANDed and equal is a set of label names. Their order and
    # omitted/empty equal lists do not change which notifications are inhibited.
    return {
        **{field: sorted(spec.get(field) or [], key=lambda m: (m["label"], m["type"], m["value"]))
           for field in FIELDS[:2]},
        "equal": sorted(spec.get("equal") or []),
    }


class InhibitionRule(RemoteResource):
    def ownership_values(self):
        if self["object"]["metadata"].get("annotations", {}).get("grafana.com/provenance"):
            return []  # Imported/provisioned rules belong to their external manager.
        return [self.id]

    def normalize(self):
        return super().normalize() | normalize_inhibition(self["object"]["spec"])

    def matches(self, declaration):
        raise AssertionError("Inhibition rule names are immutable; adoption is not supported. Declare a new rule instead.")


class Inhibitions(GrafanaClient, Provider):
    resource_type = InhibitionRule

    def __init__(self, *, owner, **kwargs):
        super().__init__(**kwargs)
        assert owner and "--" not in owner, "Inhibition-rule owner must be nonempty and must not contain --."
        self.owner = owner

    def list(self):
        self.api_version = self.discover_version(API_GROUP)
        self.path = f"/apis/{self.api_version}/namespaces/{quote(self.namespace, safe='')}/inhibitionrules"
        progress(f"  Fetching Grafana inhibition rules using {self.api_version}...", detail=True)
        return [InhibitionRule(id=uid, name=uid, object=obj) for uid, obj in self.list_objects(self.path).items()]

    def resolve_identity(self, key, declaration, rule, inventory):
        uid = InhibitionRule.format_ownership_marker(self.owner, key)
        assert len(uid) <= 40 and re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", uid), (
            f"Inhibition rule name {uid!r} must be a lowercase DNS name of at most 40 characters."
        )
        assert all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", part) for part in uid.split(".")), (
            f"Inhibition rule name {uid!r} must be a valid DNS name."
        )
        assert rule is not None or uid not in inventory, f"Inhibition rule {uid} is managed externally."
        assert rule is None or rule.id == uid, "Inhibition rule ownership does not match its declared name."
        return rule

    def desired(self, declaration):
        assert not declaration.keys() - set(FIELDS), "Unsupported InhibitionRule fields."
        for field in FIELDS[:2]:
            matchers = declaration.get(field)
            assert isinstance(matchers, list) and matchers, f"{field} must contain at least one matcher."
            for matcher in matchers:
                assert isinstance(matcher, dict) and matcher.keys() == {"label", "type", "value"}, (
                    "Each inhibition matcher must have label, type and value."
                )
                assert isinstance(matcher["label"], str) and matcher["label"], "Matcher label must be nonempty."
                assert matcher["type"] in ("=", "!=", "=~", "!~"), "Invalid inhibition matcher operator."
                assert isinstance(matcher["value"], str), "Matcher value must be a string."
        equal = declaration.get("equal", [])
        assert isinstance(equal, list) and all(isinstance(label, str) and label for label in equal), (
            "equal must be a list of nonempty label names."
        )
        return normalize_inhibition(declaration)

    def write(self, settings, rule=None):
        uid = settings["ownership"]
        assert InhibitionRule.parse_ownership_marker(uid)[0] == self.owner, "Inhibition-rule owner settings disagree."
        metadata = {"name": uid} if rule is None else deepcopy(rule["object"]["metadata"])
        if rule is not None:
            assert uid == rule.id, "Inhibition rule names cannot change."
            assert metadata.get("resourceVersion"), "Grafana omitted the inhibition resourceVersion; preview again."
        response = self.request("POST" if rule is None else "PUT",
                                self.path if rule is None else f"{self.path}/{quote(uid, safe='')}", json={
            "apiVersion": self.api_version, "kind": "InhibitionRule", "metadata": metadata,
            "spec": {field: settings[field] for field in FIELDS},
        })
        assert response["metadata"]["name"] == uid, "Grafana returned a different inhibition rule name."
        return uid

    def create(self, settings):
        return self.write(settings)

    def update(self, rule, settings):
        self.write(settings, rule)

    def adopt(self, rule, settings):
        raise AssertionError("Inhibition rule names are immutable; adoption is not supported.")

    def delete(self, rule):
        metadata = rule["object"]["metadata"]
        assert metadata.get("resourceVersion"), "Grafana omitted the inhibition resourceVersion; preview again."
        self.request("DELETE", f"{self.path}/{quote(rule.id, safe='')}", json={
            "apiVersion": "v1", "kind": "DeleteOptions",
            "preconditions": {field: metadata[field] for field in ("uid", "resourceVersion") if field in metadata},
        }, expect_json=False)
