"""Manages Tailscale Services and approves devices to host them."""

from functools import cached_property
import time
from urllib.parse import quote

from . import Provider
from ..http import API
from ..resources import RemoteResource
from ..utils import progress, require_env


ENV_VARS = {
    "client_secret": "TAILSCALE_OAUTH_CLIENT_SECRET",
}

API_URL = "https://api.tailscale.com/api/v2"
FIELDS = ("name", "displayName", "ports", "tags")


class OAuthToken:
    """Caches an OAuth access token and renews it shortly before it expires."""

    def __init__(self):
        self.client_secret = require_env(ENV_VARS["client_secret"])
        self.api = API(API_URL)
        self.token = None
        self.expires_at = 0

    def __call__(self):
        if time.monotonic() >= self.expires_at:
            progress("  Requesting Tailscale OAuth access token...", detail=True)
            body = self.api.request("POST", "/oauth/token", data={
                "grant_type": "client_credentials", "scope": "services devices:core",
                # Tailscale ignores client_id; its CLI uses this same placeholder.
                "client_id": "some-client-id", "client_secret": self.client_secret,
            })
            self.token = body["access_token"]
            self.expires_at = time.monotonic() + body["expires_in"] - 30
        return self.token


class Service(RemoteResource):
    identity_field = "name"

    def __init__(self, data):
        super().__init__(data)
        self.approvals = {}

    @property
    def id(self):
        return self.name

    def ownership_values(self):
        """Returns each comment line with surrounding whitespace removed."""
        return (line.strip() for line in (self.get("comment") or "").splitlines())

    def normalize(self):
        return super().normalize() | {
            "name": self.name,
            "displayName": self.get("displayName") or "",
            "ports": sorted(set(self.get("ports") or [])),
            "tags": sorted(set(self.get("tags") or [])),
            "devices": dict(self.approvals),
        }

    def matches(self, declaration):
        return self.name == declaration["name"]

    def comment_with_ownership(self, marker):
        """Returns a copy of the comment with the ownership marker added or replaced.

        Keeps all other text, including whitespace and line endings.
        """
        comment = self.get("comment") or ""
        current = self.ownership_marker
        if current is not None:
            return "".join(line.replace(current, marker, 1) if line.strip() == current else line
                           for line in comment.splitlines(keepends=True))
        return comment + ("\n" if comment and not comment.endswith("\n") else "") + marker


class Tailscale(Provider):
    resource_type = Service

    def __init__(self, *, api=None):
        self.api = api or API(API_URL, OAuthToken())

    def path(self, name):
        # '-' selects the token's tailnet.
        return f"/tailnet/-/services/{quote(name, safe='')}"

    def approval_path(self, name, device_id):
        return f"{self.path(name)}/device/{quote(device_id, safe='')}/approved"

    def list(self):
        progress("  Fetching services...", detail=True)
        return [Service(service) for service in self.api.request("GET", "/tailnet/-/services")["vipServices"]]

    @cached_property
    def devices(self):
        progress("  Fetching devices to resolve service hosts...", detail=True)
        return self.api.request("GET", "/tailnet/-/devices")["devices"]

    def device_id(self, selector):
        matches = [device for device in self.devices if selector in {
            device["nodeId"], device["id"], device["hostname"], device["name"], device["name"].split(".", 1)[0],
        }]
        assert matches, f"Tailscale device {selector!r} was not found."
        assert len(matches) == 1, f"Multiple devices match {selector!r}; use a stable node ID."
        return matches[0]["nodeId"]

    def desired(self, declaration):
        return {
            "name": declaration["name"],
            "displayName": declaration.get("displayName", ""),
            "ports": sorted(set(declaration["ports"])),
            "tags": sorted(set(declaration.get("tags", []))),
            "devices": {device: True for device in declaration.get("devices", [])},
        }

    def resolve_identity(self, key, declaration, service, inventory):
        name = declaration["name"]
        if service is None:
            service = inventory.get(name)
            if service is not None:
                assert service.ownership is None, f"Service {name} already has an ownership marker; refusing to take it over."

        for selector in dict.fromkeys(declaration.get("devices", [])):
            device_id = self.device_id(selector)
            if service is not None:
                progress(f"  Reading approval for {selector} on {service.name}...", detail=True)
                approval = self.api.request("GET", self.approval_path(service.name, device_id))
                service.approvals[selector] = approval["approved"] or approval.get("autoApproved", False)
        return service

    def payload(self, settings, service=None):
        payload = {key: settings[key] for key in FIELDS}
        if service is None:
            payload["comment"] = settings["ownership"]
        else:
            # PUT replaces these fields. Preserve existing comments and IPs.
            payload.update(comment=service.comment_with_ownership(settings["ownership"]), addrs=service["addrs"])
        return payload

    def approve_devices(self, service, settings):
        for selector in settings["devices"]:
            if service.approvals.get(selector):
                continue
            progress(f"  Approving {selector} to host {service.name}...")
            approval = self.api.request("POST", self.approval_path(service.name, self.device_id(selector)),
                                        json={"approved": True})
            assert approval["approved"] or approval.get("autoApproved", False), (
                f"Tailscale did not confirm approval of {selector} for {service.name}; preview again."
            )
            service.approvals[selector] = True

    def create(self, settings):
        service = Service(self.api.request("PUT", self.path(settings["name"]), json=self.payload(settings)))
        self.approve_devices(service, settings)
        return service.id

    def update(self, service, settings):
        current = service.normalize()
        if any(current[key] != settings[key] for key in (*FIELDS, "ownership")):
            service.update(self.api.request("PUT", self.path(service.name), json=self.payload(settings, service)))
        self.approve_devices(service, settings)

    def adopt(self, service, settings):
        """Adds an ownership marker to the service's comment.

        Preserves the existing comment text, service settings, and IP addresses.
        """
        ownership_marker = settings["ownership"]
        if not service.needs_adoption(ownership_marker):
            return
        payload = self.payload(service.normalize() | {"ownership": ownership_marker}, service)
        progress(f"  Adding ownership marker to {service.name}...", detail=True)
        service.update(self.api.request("PUT", self.path(service.name), json=payload))

    def delete(self, service):
        self.api.request("DELETE", self.path(service.name), expect_json=False)
