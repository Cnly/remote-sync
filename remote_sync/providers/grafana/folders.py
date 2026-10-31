"""Manages dedicated Grafana folders and their AlertRule resources.

Folder API: https://grafana.com/docs/grafana/latest/developer-resources/api-reference/http-api/folder/
Rule schema: https://github.com/grafana/grafana/blob/main/packages/grafana-openapi/src/apis/rules.alerting.grafana.app-v1beta1.json
"""

from copy import deepcopy
import re
from urllib.parse import quote

from .. import Provider
from .client import GrafanaClient
from ...resources import RemoteResource
from ...utils import progress


OWNERSHIP_ANNOTATION = "remote-sync/owner"
FOLDER_ANNOTATION = "grafana.app/folder"
RULE_API_GROUP = "rules.alerting.grafana.app"
RULE_DEFAULTS = {
    "for": "0s", "keepFiringFor": "0s", "noDataState": "NoData", "execErrState": "Error",
    "paused": False, "labels": {}, "annotations": {},
}


def duration(value):
    """Compare equivalent Prometheus durations independently of API formatting."""
    assert isinstance(value, str), "Grafana durations must be strings, such as 1m or 0s."
    if value == "0":
        return "0s"
    assert value and re.fullmatch(r"(?:\d+y)?(?:\d+w)?(?:\d+d)?(?:\d+h)?(?:\d+m)?(?:\d+s)?(?:\d+ms)?", value), (
        f"Invalid Grafana duration: {value}"
    )
    units = {"y": 31536000000, "w": 604800000, "d": 86400000, "h": 3600000,
             "m": 60000, "s": 1000, "ms": 1}
    parts = re.findall(r"(\d+)(ms|[ywdhms])", value)
    remaining = sum(int(number) * units[unit] for number, unit in parts)
    result = ""
    for unit in ("d", "h", "m", "s", "ms"):
        amount, remaining = divmod(remaining, units[unit])
        if amount:
            result += f"{amount}{unit}"
    return result or "0s"


def normalize_rule(original):
    """Compare the desired spec only; metadata and runtime status are not settings."""
    rule = deepcopy(original)
    for field, default in RULE_DEFAULTS.items():
        if rule.get(field) is None:
            rule[field] = deepcopy(default)
    for field in ("for", "keepFiringFor"):
        rule[field] = duration(rule[field])
    rule["trigger"]["interval"] = duration(rule["trigger"]["interval"])
    for field in ("notificationSettings", "panelRef", "missingSeriesEvalsToResolve"):
        if rule.get(field) is None:
            rule.pop(field, None)
    if settings := rule.get("notificationSettings"):
        for field in ("groupWait", "groupInterval", "repeatInterval"):
            if field in settings:
                settings[field] = duration(settings[field])
        for field in ("groupBy", "muteTimeIntervals", "activeTimeIntervals"):
            if not settings.get(field):
                settings.pop(field, None)
    for ref_id, query in rule["expressions"].items():
        # Match Grafana's AlertQuery.PreSave/InitDefaults and the v1beta1
        # converter, which omits default expression fields on read.
        model = query["model"]
        model["refId"] = ref_id
        for field, default in (("intervalMs", 1000), ("maxDataPoints", 43200)):
            value = model.get(field)
            if not isinstance(value, (int, float)) or isinstance(value, bool) or value == 0:
                model[field] = default
        query["datasourceUID"] = query.get("datasourceUID") or "__expr__"
        query["queryType"] = model.get("queryType", query.get("queryType")) or ""
        query["source"] = query.get("source") or False
        query["relativeTimeRange"] = {
            field: duration((query.get("relativeTimeRange") or {}).get(field, "0s"))
            for field in ("from", "to")
        }
    return rule


class Folder(RemoteResource):
    identity_field = "uid"

    def ownership_values(self):
        marker = self["object"]["metadata"].get("annotations", {}).get(OWNERSHIP_ANNOTATION)
        return [marker] if marker else []

    def normalize(self):
        return super().normalize() | {
            "uid": self["uid"], "title": self["object"]["spec"]["title"],
            "rules": {uid: normalize_rule(obj["spec"]) for uid, obj in self["rules"].items()},
        }

    def matches(self, declaration):
        return self["uid"] == declaration["uid"]


class Folders(GrafanaClient, Provider):
    resource_type = Folder

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.folders_path = f"/apis/folder.grafana.app/v1/namespaces/{quote(self.namespace, safe='')}/folders"
        self.rule_locations = {}
        self.declared_rules = {}

    def list(self):
        self.rule_api_version = self.discover_version(RULE_API_GROUP)
        self.rules_path = f"/apis/{self.rule_api_version}/namespaces/{quote(self.namespace, safe='')}/alertrules"
        progress(f"  Using Grafana alert API {self.rule_api_version}.", detail=True)
        progress("  Fetching Grafana folders and alert rules...", detail=True)
        folders = {
            uid: Folder(id=uid, uid=uid, name=obj["spec"]["title"], object=obj, rules={})
            for uid, obj in self.list_objects(self.folders_path).items()
        }
        self.rule_locations.clear()
        self.declared_rules.clear()
        for uid, obj in self.list_objects(self.rules_path).items():
            folder_uid = obj["metadata"]["annotations"][FOLDER_ANNOTATION]
            self.rule_locations[uid] = folder_uid
            if folder_uid in folders:
                folders[folder_uid]["rules"][uid] = obj
        return list(folders.values())

    def desired(self, declaration):
        uid = declaration["uid"]
        assert "groups" not in declaration, "Declare folder rules and ruleDefaults using the v1beta1 schema."
        rules = {}
        for original in declaration["rules"]:
            spec = deepcopy(original)
            rule_uid = spec.pop("uid")
            assert rule_uid and rule_uid not in self.declared_rules, "Rule UIDs must be nonempty and globally unique."
            assert self.rule_locations.get(rule_uid, uid) == uid, (
                f"Rule UID {rule_uid} already belongs to another folder; move it explicitly before syncing."
            )
            fields = RULE_DEFAULTS.keys() | {"title", "trigger", "expressions", "notificationSettings",
                                            "panelRef", "missingSeriesEvalsToResolve"}
            assert not spec.keys() - fields, f"Unsupported AlertRule fields: {', '.join(sorted(spec.keys() - fields))}"
            spec = normalize_rule(spec)
            assert spec["trigger"]["interval"] != "0s" and not spec["trigger"]["interval"].endswith("ms"), (
                "Rule evaluation interval must be a positive whole number of seconds."
            )
            assert sum(query["source"] is True for query in spec["expressions"].values()) == 1, (
                "Exactly one expression must have source = true."
            )
            assert spec["noDataState"] in ("NoData", "Ok", "Alerting", "KeepLast"), "Invalid noDataState."
            assert spec["execErrState"] in ("Error", "Ok", "Alerting", "KeepLast"), "Invalid execErrState."
            if "notificationSettings" in spec:
                settings = spec["notificationSettings"]
                assert settings.get("type") in ("SimplifiedRouting", "NamedRoutingTree"), (
                    "notificationSettings.type must be SimplifiedRouting or NamedRoutingTree."
                )
            self.declared_rules[rule_uid] = uid
            rules[rule_uid] = spec
        return {"uid": uid, "title": declaration["title"], "rules": rules}

    def resolve_identity(self, key, declaration, folder, inventory):
        if folder is None and (folder := inventory.get(declaration["uid"])) is not None:
            assert folder.ownership is None, f"Folder {folder.id} already has an ownership marker."
            assert not folder["rules"], (
                f"Folder {folder.id} already contains alerts; use --adopt KEY=ID to explicitly manage all of them."
            )
        return folder

    def absent_settings(self, settings):
        # Deleting a folder would also delete dashboards. Keep it and its marker
        # so clearing alerts converges, and a returning declaration reuses it.
        return settings | {"rules": {}}

    def write_folder(self, folder, settings):
        obj = deepcopy(folder["object"])
        assert obj["metadata"].get("resourceVersion"), "Grafana omitted the folder resourceVersion; preview again."
        obj.pop("status", None)
        obj["spec"]["title"] = settings["title"]
        obj["metadata"].setdefault("annotations", {})[OWNERSHIP_ANNOTATION] = settings["ownership"]
        response = self.request("PUT", f"{self.folders_path}/{quote(folder.id, safe='')}", json=obj)
        assert response["metadata"]["name"] == folder.id, "Grafana returned a different folder UID."
        folder["object"] = response

    def sync_rules(self, uid, current, desired):
        for rule_uid, spec in sorted(desired.items()):
            previous = current.get(rule_uid)
            if previous is not None and normalize_rule(previous["spec"]) == spec:
                continue
            metadata = deepcopy(previous["metadata"]) if previous is not None else {"name": rule_uid}
            if previous is not None:
                assert metadata.get("resourceVersion"), "Grafana omitted the rule resourceVersion; preview again."
            metadata.setdefault("annotations", {})[FOLDER_ANNOTATION] = uid
            obj = {"apiVersion": self.rule_api_version, "kind": "AlertRule", "metadata": metadata, "spec": spec}
            path = f"{self.rules_path}/{quote(rule_uid, safe='')}" if previous is not None else self.rules_path
            response = self.request("PUT" if previous is not None else "POST", path, json=obj)
            assert response["metadata"]["name"] == rule_uid, "Grafana returned a different rule UID."
        for rule_uid in sorted(current.keys() - desired.keys()):
            metadata = current[rule_uid]["metadata"]
            assert metadata.get("resourceVersion"), "Grafana omitted the rule resourceVersion; preview again."
            self.request("DELETE", f"{self.rules_path}/{quote(rule_uid, safe='')}", json={
                "apiVersion": "v1", "kind": "DeleteOptions",
                "preconditions": {field: metadata[field] for field in ("uid", "resourceVersion") if field in metadata},
            }, expect_json=False)

    def create(self, settings):
        uid = settings["uid"]
        response = self.request("POST", self.folders_path, json={
            "metadata": {"name": uid, "annotations": {OWNERSHIP_ANNOTATION: settings["ownership"]}},
            "spec": {"title": settings["title"]},
        })
        assert response["metadata"]["name"] == uid, "Grafana returned a different folder UID."
        self.sync_rules(uid, {}, settings["rules"])
        return uid

    def update(self, folder, settings):
        current = folder.normalize()
        if any(current[field] != settings[field] for field in ("title", "ownership")):
            self.write_folder(folder, settings)
        self.sync_rules(folder.id, folder["rules"], settings["rules"])

    def adopt(self, folder, settings):
        if folder.needs_adoption(settings["ownership"]):
            # Mark ownership before writing rules, so a partial apply is recoverable.
            self.write_folder(folder, folder.normalize() | {"ownership": settings["ownership"]})
