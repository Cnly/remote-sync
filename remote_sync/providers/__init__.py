from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from ..resources import RemoteResource


@dataclass
class Collection:
    """One resource handler with its declarations and explicit adoptions."""

    provider: "Provider"
    declarations: dict
    adoptions: dict = field(default_factory=dict)


class Provider(ABC):
    """Reads and changes one resource type, exposed as a single collection by default."""

    resource_type = RemoteResource

    def collections(self, *, owner, declarations, adoptions) -> dict[str, Collection]:
        """Prepare collections without API reads or writes.

        The empty name denotes a sole collection with flat inventory and no plan
        heading. Services with multiple resource types provide named collections
        and route declarations and adoptions to their respective handlers.
        """
        return {"": Collection(self, declarations, adoptions)}

    @abstractmethod
    def list(self) -> list[RemoteResource]:
        """Fetches the provider's resources and returns them as RemoteResource objects."""

    @abstractmethod
    def desired(self, declaration: dict) -> dict:
        """Converts a declaration into settings to compare against the remote resource."""

    @abstractmethod
    def resolve_identity(self, key: str, declaration: dict, resource: RemoteResource | None,
                         inventory: dict[str, RemoteResource]) -> RemoteResource | None:
        """Finds the resource for a declaration, or returns None if one must be created."""

    @abstractmethod
    def create(self, settings: dict) -> str:
        """Creates a resource, sets its ownership marker, and returns its remote ID."""

    @abstractmethod
    def update(self, resource: RemoteResource, settings: dict) -> None:
        """Applies settings to an existing resource."""

    @abstractmethod
    def adopt(self, resource: RemoteResource, settings: dict) -> None:
        """Adds the ownership marker to an existing resource."""

    def absent_settings(self, settings: dict) -> dict | None:
        """Chooses what happens to an owned resource missing from the config.

        Returns new settings to keep the resource, or None to delete it.
        """
        return None

    def delete(self, resource: RemoteResource) -> None:
        """Deletes an owned resource that is no longer declared in the config."""
        raise NotImplementedError("This provider does not support deletion.")
