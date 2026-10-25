from copy import deepcopy

import pytest

from remote_sync.core import APIAdapter, ConfigAdapter, Resource, print_plan
from remote_sync.providers import Provider as BaseProvider
from remote_sync.resources import RemoteResource


class Record(RemoteResource):
    def ownership_values(self):
        return [self.name]

    def normalize(self):
        return super().normalize() | {"value": self["value"]}

    def matches(self, declaration):
        return True


class Provider(BaseProvider):
    """An independent remote data store for testing the real DiffSync lifecycle."""

    def __init__(self, *resources):
        self.resources = {r["id"]: deepcopy(r) for r in resources}
        self.reads = []
        self.writes = []

    def list(self):
        self.reads.append(("list",))
        return [Record(deepcopy(resource)) for resource in self.resources.values()]

    def desired(self, declaration):
        return {"value": declaration["value"]}

    def resolve_identity(self, key, declaration, resource, inventory):
        return resource

    def create(self, settings):
        self.writes.append(("create", settings))
        self.resources["new"] = {"id": "new", "name": settings["ownership"], "value": settings["value"]}
        return "new"

    def update(self, resource, settings):
        self.writes.append(("update", resource.id))
        self.resources[resource.id]["value"] = settings["value"]

    def delete(self, resource):
        self.writes.append(("delete", resource.id))
        del self.resources[resource.id]

    def adopt(self, resource, settings):
        self.writes.append(("adopt", resource.id))
        self.resources[resource.id]["name"] = settings["ownership"]


def resource(key, remote_id="id", value=1, owner="test"):
    return {"id": remote_id, "name": RemoteResource.format_ownership_marker(owner, key), "value": value}


def plan(provider, declarations, **kwargs):
    remote = APIAdapter(provider=provider, owner="test", declarations=declarations, **kwargs)
    desired = ConfigAdapter(desired=remote.desired)
    return remote, desired, remote.diff_from(desired)


def apply(remote, desired, diff):
    remote.sync_from(desired, diff=diff)


def test_preview_then_apply_converges_without_touching_foreign_resources(capsys):
    provider = Provider(resource("api", value=1), resource("other", "foreign", owner="another"),
                        {"id": "manual", "name": "my manual monitor", "value": 3})
    declarations = {"api": {"value": 2}, "new": {"value": 4}}
    remote, desired, diff = plan(provider, declarations)
    print_plan(diff)
    assert "UPDATE api" in capsys.readouterr().out
    assert not provider.writes
    apply(remote, desired, diff)
    assert provider.reads == [("list",)]
    assert provider.resources["foreign"]["value"] == 1
    assert provider.resources["manual"]["value"] == 3
    assert not remote.diff_from(desired).has_diffs()
    assert not plan(provider, declarations)[2].has_diffs()


def test_noop_produces_no_api_writes():
    provider = Provider(resource("api"))
    remote, desired, diff = plan(provider, {"api": {"value": 1}})
    assert not diff.has_diffs()
    apply(remote, desired, diff)
    assert provider.writes == []


def test_deletion_is_limited_to_owned_resources():
    provider = Provider(resource("api"), resource("foreign", "foreign", owner="someone-else"))
    remote, desired, diff = plan(provider, {})
    assert provider.writes == []
    apply(remote, desired, diff)
    assert provider.reads == [("list",)]
    assert list(provider.resources) == ["foreign"]
    assert not remote.diff_from(desired).has_diffs()


def test_explicit_adoption_reconciles_settings_and_prunes_removed_declarations():
    provider = Provider({"id": "old", "name": "manual", "value": 99}, resource("unmentioned", "pruned"))
    remote, desired, diff = plan(provider, {"api": {"value": 1}}, adoptions={"api": "old"})
    assert provider.writes == []
    apply(remote, desired, diff)
    assert provider.resources == {"old": {"id": "old", "name": "nix-sync-test--api", "value": 1}}
    assert sorted(provider.writes) == [("adopt", "old"), ("delete", "pruned"), ("update", "old")]
    assert not plan(provider, {"api": {"value": 1}}, adoptions={"api": "old"})[2].has_diffs()


@pytest.mark.parametrize("record", [resource("api", owner="another"), resource("different")])
def test_adoption_never_steals_another_owner_or_key(record):
    provider = Provider(record)
    with pytest.raises(AssertionError, match="already belongs"):
        plan(provider, {"api": {"value": 1}}, adoptions={"api": record["id"]})
    assert not provider.writes


def test_adoption_requires_declared_key_and_existing_id():
    provider = Provider()
    with pytest.raises(AssertionError, match="Declare"):
        plan(provider, {}, adoptions={"api": "missing"})
    with pytest.raises(AssertionError, match="not found"):
        plan(provider, {"api": {"value": 1}}, adoptions={"api": "missing"})


def test_duplicate_owned_keys_fail_closed():
    with pytest.raises(AssertionError, match="Multiple owned"):
        plan(Provider(resource("api", "1"), resource("api", "2")), {})


def test_create_uses_discovery_snapshot_and_tracks_new_id():
    provider = Provider()
    remote, desired, diff = plan(provider, {"api": {"value": 1}})
    apply(remote, desired, diff)
    assert provider.reads == [("list",)]
    assert remote.get(Resource, "api").remote_id == "new"
    assert not remote.diff_from(desired).has_diffs()


def test_adoption_uses_discovery_snapshot_and_tracks_changes():
    provider = Provider({"id": "old", "name": "manual", "value": 1})
    remote, desired, diff = plan(provider, {"api": {"value": 2}}, adoptions={"api": "old"})
    apply(remote, desired, diff)
    assert provider.reads == [("list",)]
    assert remote.get(Resource, "api").remote_id == "old"
    assert not remote.diff_from(desired).has_diffs()
