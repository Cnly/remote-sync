"""Manages Healthchecks.io checks. Uses tags for ownership and slugs to match checks."""

from urllib.parse import quote

from . import Provider
from ..http import API
from ..resources import RemoteResource
from ..utils import progress, require_env


ENV_VARS = {
    "api_key": "HEALTHCHECKS_API_KEY",
}

FIELDS = ("name", "slug", "timeout", "grace")


class Check(RemoteResource):
    identity_field = "slug"
    create_only_fields = ("name",)

    def ownership_values(self):
        """Returns the check's tags as separate strings."""
        return self["tags"].split()

    def normalize(self):
        # Scheduled checks omit timeout; setting it switches them to heartbeat mode.
        settings = super().normalize() | {key: self[key] for key in FIELDS if key != "timeout"}
        settings["timeout"] = self.get("timeout")
        settings.update(paused=self["status"] == "paused", manual_resume=self["manual_resume"])
        return settings

    def matches(self, declaration):
        return self["slug"] == declaration["slug"]

    def tags_with_ownership(self, marker):
        """Returns the tags as a string with the ownership marker added or replaced.

        Keeps all other tags.
        """
        current = self.ownership_marker
        return " ".join([tag for tag in self["tags"].split() if tag != current] + [marker])


class Healthchecks(Provider):
    resource_type = Check

    def __init__(self, *, api=None):
        self.api = api or API("https://healthchecks.io/api/v3", interval=0.6,
                             headers={"X-Api-Key": require_env(ENV_VARS["api_key"])})

    def path(self, remote_id):
        return f"/checks/{quote(remote_id, safe='')}"

    def list(self):
        progress("  Fetching checks...", detail=True)
        checks = self.api.request("GET", "/checks/")["checks"]
        assert all("uuid" in check for check in checks), (
            f"Healthchecks discovery requires a read-write API key in {ENV_VARS['api_key']}."
        )
        return [Check(check, id=check["uuid"]) for check in checks]

    def desired(self, declaration):
        return {key: declaration[key] for key in FIELDS} | {"paused": False, "manual_resume": True}

    def absent_settings(self, settings):
        # Keep the check and its ownership so returning hosts reuse its history.
        return settings | {"paused": True, "manual_resume": True}

    def resolve_identity(self, key, declaration, check, inventory):
        matches = [resource for resource in inventory.values() if resource.matches(declaration)]
        assert len(matches) <= 1, (
            f"Multiple checks match {key}'s slug; resolve the duplicate slugs before syncing."
        )
        if check is None and matches:
            check = matches[0]
            assert check.ownership is None, (
                f"Check {check.id} matching {key} already has an ownership marker; refusing to take it over."
            )
        return check

    def payload(self, settings):
        return {key: settings[key] for key in (*FIELDS, "manual_resume")}

    def create(self, settings):
        # Creation attaches the ownership tag atomically and enables the project's
        # existing integrations. Updates leave integrations and other tags alone.
        body = self.api.request("POST", "/checks/", json={
            **self.payload(settings), "tags": settings["ownership"], "channels": "*",
        })
        return body["uuid"]

    def update(self, check, settings):
        path = self.path(check.id)
        current = check.normalize()
        changes = {key: value for key, value in self.payload(settings).items()
                   if key not in check.create_only_fields and value != current[key]}
        if current["ownership"] != settings["ownership"]:
            changes["tags"] = check.tags_with_ownership(settings["ownership"])
        status = check["status"]
        if changes:
            # Set manual_resume before pausing so lingering pings cannot wake it.
            # The write response also gives us the status without another GET.
            status = self.api.request("POST", path, json=changes)["status"]
        if (status == "paused") != settings["paused"]:
            operation = "pause" if settings["paused"] else "resume"
            self.api.request("POST", f"{path}/{operation}")

    def adopt(self, check, settings):
        """Adds an ownership tag to the check and keeps its existing tags."""
        ownership_marker = settings["ownership"]
        if not check.needs_adoption(ownership_marker):
            return
        progress(f"  Adding ownership tag to check {check.id}...", detail=True)
        tags = check.tags_with_ownership(ownership_marker)
        self.api.request("POST", self.path(check.id), json={"tags": tags})
        check["tags"] = tags
