from diffsync import Adapter, DiffSyncModel

from .providers import Provider
from .utils import progress


def unique_inventory(items):
    """Returns a dictionary of resources keyed by ID. Rejects duplicate IDs."""
    result = {}
    for item in items:
        assert item.id not in result, "Inventory contains duplicate IDs; preview again."
        result[item.id] = item
    return result


class Resource(DiffSyncModel):
    _modelname = "resource"
    _identifiers = ("key",)
    _attributes = ("settings",)

    key: str
    settings: dict
    remote_id: str | None = None

    @classmethod
    def create(cls, adapter, ids, attrs):
        remote_id = adapter.create_remote(ids["key"], attrs["settings"])
        obj = super().create(adapter=adapter, ids=ids, attrs=attrs)
        obj.remote_id = remote_id
        progress(f"Created {ids['key']}: {remote_id}")
        return obj

    def update(self, attrs):
        self.adapter.update_remote(self, attrs["settings"])
        return super().update(attrs)

    def delete(self):
        self.adapter.delete_remote(self)
        return super().delete()


class ConfigAdapter(Adapter):
    resource = Resource
    top_level = ["resource"]

    def __init__(self, *, desired):
        super().__init__(name="config")
        for key, settings in desired.items():
            self.add(Resource(key=key, settings=settings))


class APIAdapter(Adapter):
    """Matches configured resources to API records and prepares them for DiffSync.

    The provider lists existing resources and converts declarations into desired
    settings. Each RemoteResource supplies its ownership and current settings.
    DiffSync compares these settings and calls the provider to apply changes.

    Resources are matched by identity_field when set, or by the key in their
    ownership marker otherwise. The provider resolves adoption candidates and
    rejects ambiguous matches before any writes. Adoption adds ownership and
    applies the declared settings in the same plan.

    For owned resources missing from the config, absent_settings() can return
    settings that keep the resource, such as paused=True. Returning None
    schedules deletion.

    Resources are fetched once per run. Applying a plan uses that snapshot and
    assumes no concurrent changes to the account. DiffSync tracks successful
    changes in memory. If declarations is None, it only fetches resources for listing.
    """

    resource = Resource
    top_level = ["resource"]

    def __init__(self, *, provider: Provider, owner, declarations=None, adoptions=None):
        super().__init__(name="remote")
        self.provider = provider
        self.pending_adoptions = set()
        self.inventory = unique_inventory(provider.list())
        self.desired = {}
        if declarations is None:
            return
        adoptions = adoptions or {}
        resource_type = provider.resource_type
        identity_field = resource_type.identity_field
        for key in adoptions:
            assert key in declarations, f"Declare {key} before adopting it."
        if identity_field is not None:
            identities = {key: declaration[identity_field] for key, declaration in declarations.items()}
            assert len(set(identities.values())) == len(identities), (
                f"Multiple declarations use the same {identity_field}; declare each resource only once."
            )
            declarations = {identities[key]: declaration for key, declaration in declarations.items()}
            adoptions = {identities[key]: remote_id for key, remote_id in adoptions.items()}
        managed = {}
        progress(f"Checking ownership of {len(self.inventory)} remote resources...")
        for remote_resource in self.inventory.values():
            ownership_marker = remote_resource.ownership
            if ownership_marker and ownership_marker[0] == owner:
                key = remote_resource[identity_field] if identity_field is not None else ownership_marker[1]
                assert key not in managed, f"Multiple owned resources for {key}; refusing to choose one."
                managed[key] = remote_resource

        assert len(set(adoptions.values())) == len(adoptions), "Cannot adopt one ID under multiple keys."
        for key, remote_id in adoptions.items():
            assert remote_id in self.inventory, f"Adoption ID {remote_id} was not found."
            remote_resource = self.inventory[remote_id]
            ownership_marker = remote_resource.ownership
            expected_owner = resource_type.parse_ownership_marker(resource_type.format_ownership_marker(owner, key))
            assert ownership_marker is None or ownership_marker == expected_owner, (
                f"{remote_id} already belongs to {ownership_marker}; refusing to take it over."
            )
            assert key not in managed or managed[key].id == remote_id, f"{key} already has an owned resource."
            assert remote_resource.matches(declarations[key]), f"{key} does not match the declared adoption target."

        for key, declaration in declarations.items():
            selected = self.inventory[adoptions[key]] if key in adoptions else managed.get(key)
            remote_resource = provider.resolve_identity(key, declaration, selected, self.inventory)
            self.desired[key] = provider.desired(declaration) | {
                "ownership": resource_type.format_ownership_marker(owner, key),
            }
            if remote_resource is not None and key not in managed:
                assert all(resource.id != remote_resource.id for resource in managed.values()), (
                    f"Multiple declarations match resource {remote_resource.id}; declare it only once."
                )
                assert remote_resource.matches(declaration), f"{key} does not match the declared adoption target."
                self.pending_adoptions.add(key)
                managed[key] = remote_resource
        for key, remote_resource in managed.items():
            settings = remote_resource.normalize()
            if key in self.desired:
                for field in remote_resource.create_only_fields:
                    self.desired[key][field] = settings[field]
            elif (absent_settings := provider.absent_settings(settings)) is not None:
                self.desired[key] = absent_settings | {"ownership": resource_type.format_ownership_marker(owner, key)}
            self.add(Resource(
                key=key, remote_id=remote_resource.id,
                settings=settings,
            ))

    def create_remote(self, key, settings):
        progress(f"Creating {key}...")
        return self.provider.create(settings)

    def update_remote(self, resource, settings):
        remote_resource = self.inventory[resource.remote_id]
        if resource.key in self.pending_adoptions:
            progress(f"Adopting {resource.key} ({resource.remote_id})...")
            self.provider.adopt(remote_resource, settings)
            self.pending_adoptions.remove(resource.key)
            resource.settings["ownership"] = settings["ownership"]
        if resource.settings != settings:
            progress(f"Updating {resource.key}...")
            self.provider.update(remote_resource, settings)

    def delete_remote(self, resource):
        progress(f"Deleting {resource.key} ({resource.remote_id})...")
        self.provider.delete(self.inventory[resource.remote_id])
