from copy import deepcopy
from urllib.parse import unquote

import pytest

from conftest import FakeAPI
from remote_sync.core import APIAdapter, ConfigAdapter
from remote_sync.providers.grafana import Grafana


FOLDERS = "/apis/folder.grafana.app/v1/namespaces/default/folders"
DISCOVERY = "/apis/rules.alerting.grafana.app"
RULES = "/apis/rules.alerting.grafana.app/v1beta1/namespaces/default/alertrules"


def discovery(*versions):
    return {"versions": [{"version": version, "groupVersion": f"rules.alerting.grafana.app/{version}"}
                         for version in versions]}


def folder(uid="infra", *, owner=None, **annotations):
    if owner:
        annotations["remote-sync/owner"] = f"nix-sync-{owner}"
    return {
        "apiVersion": "folder.grafana.app/v1", "kind": "Folder",
        "metadata": {"name": uid, "resourceVersion": "123", "annotations": annotations},
        "spec": {"title": "Infrastructure", "description": "Keep this description"},
    }


def declaration(uid="host-down", **fields):
    return {
        "uid": uid, "title": "Host missing", "trigger": {"interval": "1m"}, "for": "5m",
        "expressions": {
            "A": {"datasourceUID": "prometheus", "relativeTimeRange": {"from": "10m", "to": "0s"},
                  "model": {"expr": 'absent_over_time(up{host="example"}[5m])', "instant": True}},
            "B": {"source": True, "model": {"type": "math", "expression": "$A > 0"}},
        },
        **fields,
    }


def rule(uid="host-down", *, folder_uid="infra", **fields):
    spec = declaration(uid, **fields)
    spec.pop("uid")
    spec.update(noDataState=spec.get("noDataState", "NoData"), execErrState=spec.get("execErrState", "Error"))
    # Real v1beta1 reads omit false/zero optional fields and add query-model defaults.
    for ref_id, query in spec["expressions"].items():
        query["model"].update(refId=ref_id, intervalMs=1000, maxDataPoints=43200)
    return {
        "apiVersion": "rules.alerting.grafana.app/v1beta1", "kind": "AlertRule",
        "metadata": {
            "name": uid, "uid": f"object-id-{uid}", "resourceVersion": "456",
            "annotations": {"grafana.app/folder": folder_uid, "grafana.app/updatedBy": "user:1"},
            "labels": {"grafana.app/folder": folder_uid},
        },
        "spec": spec,
        "status": {"health": "Ok", "lastEvaluationTime": "2026-10-30T00:00:00Z"},
    }


def declarations(rules=None, *, uid="infra"):
    return {"monitoring": {"uid": uid, "title": "Infrastructure", "rules":
                           [declaration()] if rules is None else rules}}


class GrafanaAPI:
    """In-memory resource API; reject stale updates and deletes like HTTP 409."""

    def __init__(self, *folders, rules=(), version="v1beta1"):
        self.version = version
        self.rules_path = RULES.replace("v1beta1", version)
        self.folders = {obj["metadata"]["name"]: deepcopy(obj) for obj in folders}
        self.rules = {obj["metadata"]["name"]: deepcopy(obj) for obj in rules}
        self.calls = []
        self.fail_rule_write = None

    def request(self, method, path, **kwargs):
        self.calls.append((method, path, deepcopy(kwargs)))
        if path == DISCOVERY:
            assert method == "GET"
            return discovery(self.version)
        base = FOLDERS if path == FOLDERS or path.startswith(FOLDERS + "/") else self.rules_path
        assert path == base or path.startswith(base + "/"), "Unexpected or legacy API endpoint"
        objects = self.folders if base == FOLDERS else self.rules
        if method == "GET":
            assert path == base
            return {"metadata": {}, "items": deepcopy(list(objects.values()))}
        if method == "DELETE":
            assert base == self.rules_path  # A folder DELETE would also delete dashboards.
            uid = unquote(path.removeprefix(base + "/"))
            assert kwargs["expect_json"] is False
            assert kwargs["json"]["kind"] == "DeleteOptions"
            expected = kwargs["json"]["preconditions"]
            assert "resourceVersion" in expected
            if any(objects[uid]["metadata"][key] != value for key, value in expected.items()):
                raise RuntimeError("API returned HTTP 409")
            del objects[uid]
            return None
        obj = deepcopy(kwargs["json"])
        uid = obj["metadata"]["name"]
        assert "status" not in obj
        if base == self.rules_path:
            assert obj["apiVersion"] == f"rules.alerting.grafana.app/{self.version}"
            assert obj["kind"] == "AlertRule"
            assert obj["metadata"]["annotations"]["grafana.app/folder"] in self.folders
            if uid == self.fail_rule_write:
                self.fail_rule_write = None
                raise RuntimeError("API returned HTTP 500")
        if method == "POST":
            assert path == base
            if uid in objects:
                raise RuntimeError("API returned HTTP 409")
            assert "resourceVersion" not in obj["metadata"]
            obj["metadata"].update(resourceVersion="1", uid=f"object-id-{uid}")
        else:
            assert method == "PUT" and unquote(path.removeprefix(base + "/")) == uid
            if obj["metadata"]["resourceVersion"] != objects[uid]["metadata"]["resourceVersion"]:
                raise RuntimeError("API returned HTTP 409")
            obj["metadata"]["resourceVersion"] = str(int(obj["metadata"]["resourceVersion"]) + 1)
        objects[uid] = obj
        return deepcopy(obj)


def plan(api, config=None, **kwargs):
    remote = APIAdapter(provider=Grafana(api=api), owner="test",
                        declarations=declarations() if config is None else config, **kwargs)
    desired = ConfigAdapter(desired=remote.desired)
    return remote, desired, remote.diff_from(desired)


def apply(api, config=None, **kwargs):
    remote, desired, diff = plan(api, config, **kwargs)
    remote.sync_from(desired, diff=diff)
    return diff


@pytest.mark.parametrize("version", ["v1beta1", "v0alpha1"])
def test_preview_does_not_write_and_creation_converges_with_server_metadata(version):
    api = GrafanaAPI(version=version)
    remote, desired, diff = plan(api)
    assert diff.summary()["create"] == 1
    assert all(method == "GET" for method, _, _ in api.calls)
    remote.sync_from(desired, diff=diff)
    assert [(method, path) for method, path, _ in api.calls if method != "GET"] == [
        ("POST", FOLDERS), ("POST", api.rules_path),
    ]
    assert api.folders["infra"]["metadata"]["annotations"] == {"remote-sync/owner": "nix-sync-test"}
    obj = api.rules["host-down"]
    assert obj["metadata"]["name"] == "host-down" and obj["metadata"]["uid"] != "host-down"
    assert obj["spec"]["expressions"]["B"]["source"] is True
    assert "uid" not in obj["spec"] and "condition" not in obj["spec"]
    assert not plan(api)[2].has_diffs()


def test_alpha_only_stack_updates_and_deletes_using_the_discovered_version():
    api = GrafanaAPI(version="v0alpha1")
    apply(api)
    api.calls.clear()
    apply(api, declarations([declaration(**{"for": "10m"})]))
    assert api.rules["host-down"]["spec"]["for"] == "10m"
    assert api.rules["host-down"]["apiVersion"] == "rules.alerting.grafana.app/v0alpha1"
    assert not plan(api, declarations([declaration(**{"for": "10m"})]))[2].has_diffs()
    apply(api, declarations([]))
    assert not api.rules
    assert [(method, path) for method, path, _ in api.calls if method != "GET"] == [
        ("PUT", api.rules_path + "/host-down"), ("DELETE", api.rules_path + "/host-down"),
    ]


@pytest.mark.parametrize("versions", [("v0alpha1", "v1beta1"), ("v1beta1", "v0alpha1"), ("v2", "v1beta1")])
def test_discovery_prefers_beta_over_alpha_or_unknown_versions(versions):
    api = FakeAPI(discovery(*versions), {"items": []}, {"items": []})
    assert Grafana(api=api).list() == []
    assert [path for _, path, _ in api.calls] == [DISCOVERY, FOLDERS, RULES]


@pytest.mark.parametrize("versions", [(), ("v2",)])
def test_no_supported_api_version_stops_before_reading_resources_or_writing(versions):
    api = FakeAPI(discovery(*versions))
    with pytest.raises(AssertionError, match="must serve v1beta1 or v0alpha1"):
        Grafana(api=api).list()
    assert api.calls == [("GET", DISCOVERY, {})]


@pytest.mark.parametrize("path", [DISCOVERY, FOLDERS, RULES])
def test_api_read_error_identifies_the_endpoint_and_stops(path):
    responses = [] if path == DISCOVERY else [discovery("v1beta1")]
    if path == RULES:
        responses.append({"items": []})
    api = FakeAPI(*responses, AssertionError("API returned HTTP 404"))
    with pytest.raises(RuntimeError) as error:
        Grafana(api=api).list()
    assert str(error.value) == f"Grafana GET {path}: API returned HTTP 404"
    assert api.token not in str(error.value)
    assert api.calls[-1][:2] == ("GET", path)


def test_server_defaults_durations_and_status_do_not_cause_perpetual_updates():
    live = rule(**{"for": "1m", "keepFiringFor": "2m", "notificationSettings": {
        "type": "SimplifiedRouting", "receiver": "email", "groupWait": "1m",
    }})
    declared = declaration(**{"for": "60s", "keepFiringFor": "120s", "trigger": {"interval": "60s"},
                             "notificationSettings": {"type": "SimplifiedRouting", "receiver": "email",
                                                      "groupWait": "60s", "groupBy": []}})
    declared["expressions"]["A"]["relativeTimeRange"] = {"from": "600s", "to": "0"}
    declared["expressions"]["B"].update(datasourceUID="__expr__", queryType="",
                                       relativeTimeRange={"from": "0s", "to": "0s"})
    live["spec"]["panelRef"] = None
    api = GrafanaAPI(folder(owner="test"), rules=[live])
    original = deepcopy(declared)
    assert not plan(api, declarations([declared]))[2].has_diffs()
    assert declared == original and api.rules["host-down"] == live
    assert all(method == "GET" for method, _, _ in api.calls)


def test_reconciliation_only_writes_changed_rules_and_preserves_other_folders_and_metadata():
    api = GrafanaAPI(folder(owner="test", custom="keep"), folder("manual"), folder("foreign", owner="other"),
                     rules=[rule(), rule("unchanged"), rule("old/rule"),
                            rule("manual", folder_uid="manual"), rule("foreign", folder_uid="foreign")])
    before = deepcopy(api.folders)
    untouched = deepcopy({uid: api.rules[uid] for uid in ("unchanged", "manual", "foreign")})
    config = declarations([declaration(**{"for": "10m", "trigger": {"interval": "2m"}}),
                           declaration("new-rule"), declaration("unchanged")])
    apply(api, config)
    assert api.folders == before
    assert {uid: api.rules[uid] for uid in untouched} == untouched
    assert "old/rule" not in api.rules
    assert api.rules["host-down"]["spec"]["trigger"]["interval"] == "2m"
    assert api.rules["host-down"]["metadata"]["annotations"]["grafana.app/updatedBy"] == "user:1"
    assert [(method, path) for method, path, _ in api.calls if method != "GET"] == [
        ("PUT", RULES + "/host-down"), ("POST", RULES), ("DELETE", RULES + "/old%2Frule"),
    ]
    assert not plan(api, config)[2].has_diffs()


@pytest.mark.parametrize("config", [{}, declarations([])])
def test_removed_folder_or_empty_rules_clears_alerts_but_keeps_folder_and_converges(config):
    obj = folder(owner="test")
    api = GrafanaAPI(obj, rules=[rule()])
    diff = apply(api, config)
    assert diff.summary()["update"] == 1 and diff.summary()["delete"] == 0
    assert api.folders == {"infra": obj} and api.rules == {}
    assert not plan(api, config)[2].has_diffs()
    apply(api)
    assert len(api.folders) == 1 and "host-down" in api.rules
    assert not plan(api)[2].has_diffs()


def test_empty_folder_is_adopted_without_losing_metadata():
    api = GrafanaAPI(folder(custom="keep"))
    remote, desired, diff = plan(api)
    assert remote.pending_adoptions == {"infra"}
    remote.sync_from(desired, diff=diff)
    obj = api.folders["infra"]
    assert obj["metadata"]["annotations"] == {"custom": "keep", "remote-sync/owner": "nix-sync-test"}
    assert obj["spec"]["description"] == "Keep this description"
    assert not plan(api)[2].has_diffs()


def test_nonempty_folder_requires_explicit_adoption_and_previews_all_removals():
    api = GrafanaAPI(folder(), rules=[rule("manual")])
    with pytest.raises(AssertionError, match="already contains alerts"):
        plan(api)
    assert all(method == "GET" for method, _, _ in api.calls)
    remote, desired, diff = plan(api, adoptions={"monitoring": "infra"})
    assert remote.pending_adoptions == {"infra"}
    assert "manual" in remote.get("resource", "infra").settings["rules"]
    remote.sync_from(desired, diff=diff)
    assert set(api.rules) == {"host-down"}
    assert next(path for method, path, _ in api.calls if method != "GET") == FOLDERS + "/infra"
    assert not plan(api)[2].has_diffs()


@pytest.mark.parametrize("explicit", [False, True])
def test_other_owner_cannot_be_adopted(explicit):
    api = GrafanaAPI(folder(owner="other"))
    with pytest.raises(AssertionError, match="ownership marker|already belongs"):
        plan(api, adoptions={"monitoring": "infra"} if explicit else {})
    assert all(method == "GET" for method, _, _ in api.calls)


def test_partial_create_is_recovered_without_recreating_the_folder_or_successful_rules():
    api = GrafanaAPI()
    config = declarations([declaration("first"), declaration("second")])
    api.fail_rule_write = "second"
    with pytest.raises(Exception, match="HTTP 500"):
        apply(api, config)
    assert api.folders["infra"]["metadata"]["annotations"]["remote-sync/owner"] == "nix-sync-test"
    assert set(api.rules) == {"first"}
    api.calls.clear()
    apply(api, config)
    writes = [(method, path, kwargs) for method, path, kwargs in api.calls if method != "GET"]
    assert len(writes) == 1 and writes[0][:2] == ("POST", RULES)
    assert writes[0][2]["json"]["metadata"]["name"] == "second"
    assert not plan(api, config)[2].has_diffs()


@pytest.mark.parametrize("operation", ["update", "delete", "create"])
def test_concurrent_rule_changes_stop_apply_without_overwriting_or_retrying(operation):
    api = GrafanaAPI(folder(owner="test"), rules=[] if operation == "create" else [rule()])
    config = declarations([] if operation == "delete" else [declaration(**{"for": "10m"})])
    remote, desired, diff = plan(api, config)
    api.rules["host-down"] = rule(**{"for": "15m"})
    api.rules["host-down"]["metadata"]["resourceVersion"] = "999"
    with pytest.raises(Exception, match="HTTP 409"):
        remote.sync_from(desired, diff=diff)
    assert api.rules["host-down"]["spec"]["for"] == "15m"
    assert len([call for call in api.calls if call[0] != "GET"]) == 1


def test_concurrent_folder_change_stops_adoption_before_rule_writes():
    api = GrafanaAPI(folder())
    remote, desired, diff = plan(api)
    api.folders["infra"]["metadata"]["resourceVersion"] = "999"
    with pytest.raises(Exception, match="HTTP 409"):
        remote.sync_from(desired, diff=diff)
    assert not api.rules


@pytest.mark.parametrize("config", [
    declarations([declaration(), declaration()]),
    declarations() | {"second": {"uid": "second", "title": "Second", "rules": [declaration()]}},
    declarations([declaration(trigger={"interval": "0s"})]),
    declarations([declaration(trigger={"interval": "1ms"})]),
    declarations([declaration(noDataState="OK")]),
    declarations([declaration(condition="B")]),
    declarations([declaration(record={"metric": "example"})]),
    declarations([declaration(expressions={})]),
    declarations([declaration(expressions={"A": {"source": True, "model": {}},
                                          "B": {"source": True, "model": {}}})]),
    declarations([declaration(notificationSettings={"receiver": "email"})]),
    declarations([declaration(notificationSettings={})]),
    {"monitoring": {"uid": "infra", "title": "Infrastructure", "groups": {}}},
])
def test_invalid_rule_configuration_fails_before_any_writes(config):
    api = GrafanaAPI()
    with pytest.raises(AssertionError):
        plan(api, config)
    assert all(method == "GET" for method, _, _ in api.calls)


def test_rule_uid_cannot_move_between_folders_implicitly():
    api = GrafanaAPI(folder("manual"), rules=[rule(folder_uid="manual")])
    with pytest.raises(AssertionError, match="already belongs to another"):
        plan(api)
    assert all(method == "GET" for method, _, _ in api.calls)


def test_both_lists_follow_pagination_and_use_resource_names_as_identity():
    first = folder(owner="test")
    first["metadata"]["uid"] = "kubernetes-object-id"
    api = FakeAPI(discovery("v1beta1"), {"items": [first], "metadata": {"continue": "folders-next"}},
                  {"items": [folder("second")], "metadata": {}},
                  {"items": [rule()], "metadata": {"continue": "rules-next"}},
                  {"items": [rule("second-alert")], "metadata": {}})
    items = Grafana(api=api).list()
    assert [item.id for item in items] == ["infra", "second"]
    assert set(items[0]["rules"]) == {"host-down", "second-alert"}
    assert api.calls[2] == ("GET", FOLDERS, {"params": {"limit": 100, "continue": "folders-next"}})
    assert api.calls[4] == ("GET", RULES, {"params": {"limit": 100, "continue": "rules-next"}})


@pytest.mark.parametrize("path", [FOLDERS, RULES])
@pytest.mark.parametrize("duplicate", [False, True])
def test_list_rejects_repeated_pagination_tokens_or_resource_names(path, duplicate):
    obj = folder() if path == FOLDERS else rule()
    page = {"items": [obj] if duplicate else [], "metadata": {"continue": "same"}}
    api = FakeAPI(discovery("v1beta1"), *([{"items": []}] if path == RULES else []), page, page)
    with pytest.raises(AssertionError, match="duplicate resource names|pagination repeated"):
        Grafana(api=api).list()


def test_namespace_is_used_for_both_resource_apis():
    api = FakeAPI(discovery("v1beta1"), {"items": []}, {"items": []})
    Grafana(api=api, namespace="stacks-123").list()
    assert [path for _, path, _ in api.calls] == [
        DISCOVERY,
        FOLDERS.replace("/default/", "/stacks-123/"), RULES.replace("/default/", "/stacks-123/"),
    ]
