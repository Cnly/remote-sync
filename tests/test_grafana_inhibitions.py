from copy import deepcopy
from urllib.parse import unquote

import pytest

from conftest import FakeAPI
from remote_sync.core import APIAdapter, ConfigAdapter
from remote_sync.providers.grafana.inhibitions import Inhibitions


GROUP = "notifications.alerting.grafana.app"
DISCOVERY = f"/apis/{GROUP}"
PATH = f"/apis/{GROUP}/v1beta1/namespaces/default/inhibitionrules"


def declaration(**fields):
    return {
        "source_matchers": [{"label": "alertname", "type": "=", "value": "Unit failed"},
                            {"label": "managed_by", "type": "=", "value": "nix"}],
        "target_matchers": [{"label": "alertname", "type": "=~", "value": "Unit inactive|Unit restarting"}],
        "equal": ["host", "container", "name"],
        **fields,
    }


def rule(name="nix-sync-test--unit-failed", *, provenance=None, **fields):
    return {
        "apiVersion": f"{GROUP}/v1beta1", "kind": "InhibitionRule",
        "metadata": {"name": name, "uid": f"object-{name}", "resourceVersion": "1",
                     "annotations": {"grafana.com/provenance": provenance} if provenance else {}},
        "spec": declaration(**fields),
    }


class InhibitionsAPI:
    """Model Grafana's immutable names, discarded custom metadata and version checks."""

    def __init__(self, *rules, version="v1beta1"):
        self.version = version
        self.path = PATH.replace("v1beta1", version)
        self.objects = {obj["metadata"]["name"]: deepcopy(obj) for obj in rules}
        self.calls = []
        self.fail_write = None

    def request(self, method, path, **kwargs):
        self.calls.append((method, path, deepcopy(kwargs)))
        if path == DISCOVERY:
            assert method == "GET"
            return {"versions": [{"version": self.version}]}
        assert path == self.path or path.startswith(self.path + "/")
        if method == "GET":
            assert path == self.path
            return {"items": deepcopy(list(self.objects.values()))}
        if method == "DELETE":
            uid = unquote(path.removeprefix(self.path + "/"))
            assert kwargs["expect_json"] is False
            assert kwargs["json"]["kind"] == "DeleteOptions"
            expected = kwargs["json"]["preconditions"]
            assert "resourceVersion" in expected
            if any(self.objects[uid]["metadata"][key] != value for key, value in expected.items()):
                raise RuntimeError("API returned HTTP 409")
            del self.objects[uid]
            return None
        obj = deepcopy(kwargs["json"])
        uid = obj["metadata"]["name"]
        assert obj["apiVersion"] == f"{GROUP}/{self.version}" and obj["kind"] == "InhibitionRule"
        assert "status" not in obj and set(obj["spec"]) == {"source_matchers", "target_matchers", "equal"}
        assert not obj["metadata"].get("annotations", {}).get("remote-sync/owner")
        if uid == self.fail_write:
            self.fail_write = None
            raise RuntimeError("API returned HTTP 500")
        if method == "POST":
            assert path == self.path
            if uid in self.objects:
                raise RuntimeError("API returned HTTP 409")
            assert "resourceVersion" not in obj["metadata"]
            version = "1"
        else:
            assert method == "PUT" and unquote(path.removeprefix(self.path + "/")) == uid
            if obj["metadata"]["resourceVersion"] != self.objects[uid]["metadata"]["resourceVersion"]:
                raise RuntimeError("API returned HTTP 409")
            version = str(int(obj["metadata"]["resourceVersion"]) + 1)
        obj["metadata"] = {"name": uid, "uid": f"object-{uid}", "resourceVersion": version}
        self.objects[uid] = obj
        return deepcopy(obj)


def plan(api, config=None, *, owner="test", namespace="default", **kwargs):
    remote = APIAdapter(provider=Inhibitions(api=api, owner=owner, namespace=namespace), owner=owner,
                        declarations={"unit-failed": declaration()} if config is None else config, **kwargs)
    desired = ConfigAdapter(desired=remote.desired)
    return remote, desired, remote.diff_from(desired)


def apply(api, config=None, **kwargs):
    remote, desired, diff = plan(api, config, **kwargs)
    remote.sync_from(desired, diff=diff)
    return diff


@pytest.mark.parametrize("version", ["v1beta1", "v0alpha1"])
def test_preview_create_update_delete_converge_without_custom_annotations(version):
    api = InhibitionsAPI(version=version)
    remote, desired, diff = plan(api)
    assert diff.summary()["create"] == 1 and all(method == "GET" for method, _, _ in api.calls)
    remote.sync_from(desired, diff=diff)
    uid = "nix-sync-test--unit-failed"
    assert set(api.objects) == {uid}
    assert api.objects[uid]["metadata"]["uid"] != uid
    assert not plan(api)[2].has_diffs()
    config = {"unit-failed": declaration(equal=["host"])}
    apply(api, config)
    assert api.objects[uid]["spec"]["equal"] == ["host"]
    assert not plan(api, config)[2].has_diffs()
    apply(api, {})
    assert api.objects == {} and not plan(api, {})[2].has_diffs()
    assert [(method, path) for method, path, _ in api.calls if method != "GET"] == [
        ("POST", api.path), ("PUT", api.path + "/" + uid), ("DELETE", api.path + "/" + uid),
    ]


def test_manual_foreign_and_imported_rules_are_preserved():
    untouched = [rule("manual"), rule("nix-sync-other--unit-failed"),
                 rule("nix-sync-test--imported", provenance="converted_prometheus")]
    api = InhibitionsAPI(*untouched, rule("nix-sync-test--obsolete"))
    apply(api)
    assert {uid: api.objects[uid] for uid in [obj["metadata"]["name"] for obj in untouched]} == {
        obj["metadata"]["name"]: obj for obj in untouched
    }
    assert "nix-sync-test--obsolete" not in api.objects
    assert not plan(api)[2].has_diffs()


def test_provisioned_name_collision_fails_before_writes():
    api = InhibitionsAPI(rule(provenance="api"))
    with pytest.raises(AssertionError, match="managed externally"):
        plan(api)
    assert all(method == "GET" for method, _, _ in api.calls)


def test_adoption_cannot_rename_or_take_over_an_existing_rule():
    api = InhibitionsAPI(rule("manual"))
    with pytest.raises(AssertionError, match="names are immutable"):
        plan(api, adoptions={"unit-failed": "manual"})
    assert all(method == "GET" for method, _, _ in api.calls)


@pytest.mark.parametrize("equal", [[], None])
def test_server_order_and_empty_equal_defaults_do_not_cause_updates(equal):
    obj = rule(equal=equal)
    obj["spec"]["source_matchers"].reverse()
    config = {"unit-failed": declaration()}
    del config["unit-failed"]["equal"]
    before = deepcopy(config)
    assert not plan(InhibitionsAPI(obj), config)[2].has_diffs()
    assert config == before


def test_equal_order_is_irrelevant():
    assert not plan(InhibitionsAPI(rule(equal=["name", "host", "container"])))[2].has_diffs()


@pytest.mark.parametrize("operation", ["create", "update", "delete"])
def test_concurrent_changes_stop_without_overwriting_or_retrying(operation):
    api = InhibitionsAPI(*([] if operation == "create" else [rule()]))
    config = {} if operation == "delete" else {"unit-failed": declaration(equal=["host"])}
    remote, desired, diff = plan(api, config)
    concurrent = rule(equal=["cluster"])
    concurrent["metadata"]["resourceVersion"] = "999"
    api.objects[concurrent["metadata"]["name"]] = concurrent
    with pytest.raises(Exception, match="HTTP 409"):
        remote.sync_from(desired, diff=diff)
    assert api.objects[concurrent["metadata"]["name"]] == concurrent
    assert len([call for call in api.calls if call[0] != "GET"]) == 1


def test_partial_apply_recovers_without_recreating_successful_rules():
    api = InhibitionsAPI()
    config = {"first": declaration(), "second": declaration(equal=["host"])}
    api.fail_write = "nix-sync-test--second"
    with pytest.raises(Exception, match="HTTP 500"):
        apply(api, config)
    api.calls.clear()
    apply(api, config)
    assert [(method, path) for method, path, _ in api.calls if method != "GET"] == [("POST", api.path)]
    assert not plan(api, config)[2].has_diffs()


@pytest.mark.parametrize("fields", [
    {"source_matchers": []}, {"target_matchers": []}, {"source_matchers": "bad"},
    {"source_matchers": [{"label": "severity", "type": "==", "value": "critical"}]},
    {"source_matchers": [{"label": "", "type": "=", "value": "critical"}]},
    {"source_matchers": [{"label": "severity", "type": "=", "value": 1}]},
    {"target_matchers": [{"label": "severity", "value": "warning"}]},
    {"equal": "host"}, {"equal": [""]}, {"name": "unexpected"},
])
def test_invalid_specs_fail_before_writes(fields):
    api = InhibitionsAPI()
    with pytest.raises(AssertionError):
        plan(api, {"invalid": declaration(**fields)})
    assert all(method == "GET" for method, _, _ in api.calls)


@pytest.mark.parametrize("owner,key", [("BadOwner", "rule"), ("test", "BadKey"), ("test", "a" * 40),
                                       ("test", "a..b"), ("test", "a.-b"), ("test", "a/b"),
                                       ("test--other", "rule"), ("test", "")])
def test_invalid_resource_names_fail_before_writes(owner, key):
    api = InhibitionsAPI()
    with pytest.raises(AssertionError):
        plan(api, {key: declaration()}, owner=owner)
    assert all(method == "GET" for method, _, _ in api.calls)


@pytest.mark.parametrize("versions", [["v0alpha1", "v1beta1"], ["v1beta1", "v0alpha1"]])
def test_discovery_prefers_beta_and_uses_the_configured_namespace(versions):
    api = FakeAPI({"versions": [{"version": v} for v in versions]}, {"items": []})
    assert Inhibitions(api=api, owner="test", namespace="stacks-123").list() == []
    assert [path for _, path, _ in api.calls] == [DISCOVERY, PATH.replace("/default/", "/stacks-123/")]


@pytest.mark.parametrize("path", [DISCOVERY, PATH])
def test_unavailable_api_reports_endpoint_and_stops(path):
    responses = [] if path == DISCOVERY else [{"versions": [{"version": "v1beta1"}]}]
    api = FakeAPI(*responses, AssertionError("API returned HTTP 404"))
    with pytest.raises(RuntimeError, match=f"Grafana GET {path}: API returned HTTP 404"):
        Inhibitions(api=api, owner="test").list()
    assert all(method == "GET" for method, _, _ in api.calls)


def test_pagination_reads_all_rules():
    api = FakeAPI({"versions": [{"version": "v1beta1"}]},
                  {"items": [rule()], "metadata": {"continue": "next"}},
                  {"items": [rule("manual")]})
    items = Inhibitions(api=api, owner="test").list()
    assert [item.id for item in items] == ["nix-sync-test--unit-failed", "manual"]
    assert api.calls[-1] == ("GET", PATH, {"params": {"limit": 100, "continue": "next"}})
