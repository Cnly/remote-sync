OWNERSHIP_MARKER_PREFIX = "nix-sync-"


class RemoteResource(dict):
    """An API record stored as a dictionary, with helpers for ownership and settings."""

    # The remote resource field to use to bind the resource to a Nix
    # declaration key. If None, the key in the ownership marker will be used.
    identity_field = None

    # These settings seed creation and retain their remote values on later syncs.
    create_only_fields = ()

    @property
    def id(self):
        return self["id"]

    @property
    def name(self):
        return self["name"]

    @classmethod
    def format_ownership_marker(cls, owner, key):
        """Builds an ownership marker. Includes the key if identity_field is None."""
        suffix = f"--{key}" if cls.identity_field is None else ""
        return f"{OWNERSHIP_MARKER_PREFIX}{owner}{suffix}"

    @classmethod
    def parse_ownership_marker(cls, value):
        """Returns an (owner, key) tuple, or None if the text is not a marker.

        The key is None when an API field identifies the resource.
        """
        if not value.startswith(OWNERSHIP_MARKER_PREFIX):
            return None
        owner, separator, key = value[len(OWNERSHIP_MARKER_PREFIX):].partition("--")
        # The reserved prefix must identify a complete marker, never an unowned resource.
        assert separator or cls.identity_field is not None, "Malformed ownership marker; correct it explicitly first."
        # Owner-only providers also recognize old keyed markers for migration.
        return owner, key if cls.identity_field is None else None

    def ownership_values(self):
        """Returns the strings to search for an ownership marker."""
        raise NotImplementedError

    @property
    def ownership_marker(self):
        """Returns the stored marker string, or None if there is no marker."""
        markers = [value for value in self.ownership_values()
                   if self.parse_ownership_marker(value) is not None]
        assert len(markers) <= 1, (
            f"Multiple ownership markers on resource {self.id}; refusing to choose one."
        )
        return markers[0] if markers else None

    @property
    def ownership(self):
        """Returns an (owner, key) tuple, or None if the resource is unowned.

        The key is None when an API field identifies the resource.
        """
        marker = self.ownership_marker
        return self.parse_ownership_marker(marker) if marker is not None else None

    def normalize(self):
        """Returns the resource's current settings for comparison with the config.

        This base implementation includes only ownership. Subclasses add the
        settings managed by their provider.
        """
        return {"ownership": self.ownership_marker}

    def needs_adoption(self, ownership_marker):
        """Checks whether the resource still needs an ownership marker.

        Returns True if it is unowned, or False if its ownership already matches.
        Raises AssertionError if it has conflicting ownership.
        """
        current = self.ownership
        assert current is None or current == self.parse_ownership_marker(ownership_marker), (
            "Refusing to replace another ownership marker."
        )
        return current is None
