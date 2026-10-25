"""Manages HTTP monitors using v3 to read them and v2 to create, update, or delete them."""

from functools import cached_property
from urllib.parse import quote

from . import Provider
from ..http import API
from ..resources import RemoteResource
from ..utils import progress, require_env


ENV_VARS = {
    "api_token": "HETRIXTOOLS_API_TOKEN",
    "contact_list_id": "HETRIXTOOLS_CONTACT_LIST_ID",  # Required when contact_list_id is null.
}

# Use v3 names in declarations, except contact_list_id (v2 supports one list).
FIELDS = {
    "name": "Name", "target": "Target", "http_method": "Method",
    "keyword": "Keyword", "timeout": "Timeout", "check_frequency": "Frequency",
    "number_of_tries": "FailsBeforeAlert", "triggering_locations": "FailedLocations",
    "category": "Category", "alert_after_minutes": "AlertAfter",
    "repeat_alert_times": "RepeatTimes", "repeat_alert_frequency": "RepeatEvery",
    "public_report": "Public", "public_target": "ShowTarget",
    "verify_ssl_certificate": "VerSSLCert", "verify_ssl_hostname": "VerSSLHost",
    "max_redirects": "MaxRedirects",
}
# v3 location names to v2 write codes.
LOCATION_CODES = {
    "new_york": "nyc", "san_francisco": "sfo", "dallas": "dal",
    "amsterdam": "ams", "london": "lon", "frankfurt": "fra",
    "singapore": "sgp", "sydney": "syd", "sao_paulo": "sao",
    "tokyo": "tok", "mumbai": "mba", "warsaw": "waw",
}


class Monitor(RemoteResource):
    def __init__(self, data, *, api):
        super().__init__(data)
        self.api = api

    @property
    def notes_path(self):
        return f"/v3/uptime-monitors/{quote(self.id, safe='')}/private-notes"

    @cached_property
    def notes(self):
        # Cache notes for this invocation; they never enter the public plan
        # or a state file.
        progress(f"  Reading ownership notes for {self.get('name', self.id)!r} ({self.id})...", detail=True)
        body = self.api.request("GET", self.notes_path)
        return body["private_notes"]

    def ownership_values(self):
        """Returns each line of the private notes with surrounding whitespace removed."""
        return (line.strip() for line in self.notes.splitlines())

    def normalize(self):
        settings = super().normalize() | {key: self[key] for key in FIELDS}
        settings["keyword"] = settings["keyword"] or ""
        settings.update(
            contact_list_id=next(iter(self["contact_lists"]), ""),
            locations=sorted(self["locations"]), accepted_http_codes=sorted(set(self["accepted_http_codes"])),
        )
        return settings

    def matches(self, declaration):
        return (self["type"] == "website" and self["target"] == declaration["target"]
                and self["http_method"] == declaration["http_method"])


class HetrixTools(Provider):
    def __init__(self, *, api=None):
        self.api = api or API("https://api.hetrixtools.com", require_env(ENV_VARS["api_token"]), interval=0.55)

    def list(self):
        items, page = [], 1
        while page is not None:
            progress(f"  Fetching monitors, page {page}...", detail=True)
            body = self.api.request("GET", "/v3/uptime-monitors", params={
                "page": page, "per_page": 200,
            })
            items.extend(Monitor(item, api=self.api) for item in body["monitors"])
            page = body["meta"]["pagination"]["next"]
        return items

    def desired(self, declaration):
        settings = dict(declaration)
        if settings["contact_list_id"] is None:
            settings["contact_list_id"] = require_env(ENV_VARS["contact_list_id"])
        settings["locations"] = sorted(set(settings["locations"]))
        settings["accepted_http_codes"] = sorted(set(settings["accepted_http_codes"]))
        if settings["triggering_locations"] is None:
            settings["triggering_locations"] = len(settings["locations"]) // 2 + 1
        return settings

    def resolve_identity(self, key, declaration, monitor, inventory):
        if monitor is None:
            matches = [resource for resource in inventory.values() if resource.matches(declaration)]
            assert len(matches) <= 1, (
                f"Multiple monitors match {key}'s target and method; use --list and explicitly adopt the intended ID."
            )
            if matches:
                monitor = matches[0]
                assert monitor.ownership is None, (
                    f"Monitor {monitor.id} matching {key} already has an ownership marker; refusing to take it over."
                )
        return monitor

    def payload(self, settings, monitor=None):
        payload = {api_key: settings[key] for key, api_key in FIELDS.items()}
        payload.update(
            Type=1, ContactList=settings["contact_list_id"],
            Locations={LOCATION_CODES[name]: True for name in settings["locations"]},
            HTTPCodes=",".join(map(str, settings["accepted_http_codes"])),
        )
        for key in ("AlertAfter", "RepeatTimes", "RepeatEvery"):
            if payload[key] == 0:
                payload[key] = ""
        if monitor is not None:
            payload["MID"] = monitor.id
            for prefix, key in (("ssl", "SSLExpiryReminder"), ("domain", "DomainExpiryReminder")):
                payload[key] = monitor[f"{prefix}_expiration_warn_days"] if monitor[f"{prefix}_expiration_warn"] else 0
            payload["NSChangeAlert"] = int(monitor["nameservers_change_warn"])
        return payload

    def write(self, operation, payload):
        body = self.api.request("POST", f"/v2/{self.api.token}/uptime/{operation}/",
                                bearer=False, json=payload)
        assert body.get("status") == "SUCCESS", "HetrixTools rejected the mutation; check API permissions, quota, and settings."
        return body["monitor_id"]

    def create(self, settings):
        remote_id = self.write("add", self.payload(settings))
        try:
            # Newly created monitors have no ownership notes to discover.
            monitor = Monitor({"id": remote_id}, api=self.api)
            monitor.notes = ""
            self.adopt(monitor, settings)
        except (AssertionError, RuntimeError) as exc:
            raise RuntimeError(
                f"Created monitor {remote_id}, but ownership tagging was not confirmed: {exc} "
                "Preview again to reconcile this monitor by target and method."
            ) from None
        return remote_id

    def update(self, monitor, settings):
        remote_id = self.write("edit", self.payload(settings, monitor))
        assert remote_id == monitor.id, "HetrixTools returned a different monitor ID for an edit; preview again."

    def adopt(self, monitor, settings):
        """Appends an ownership marker to the monitor's existing private notes."""
        ownership_marker = settings["ownership"]
        if not monitor.needs_adoption(ownership_marker):
            return
        # Append preserves all user notes. Never retry an uncertain POST: another
        # preview reads the marker and makes a successful append a no-op.
        progress(f"  Appending ownership marker to monitor {monitor.id}...", detail=True)
        body = self.api.request("POST", monitor.notes_path, json={"private_notes": ownership_marker})
        assert body.get("status") == "success", "HetrixTools did not confirm the private-note append; preview again."

    def delete(self, monitor):
        self.write("delete", {"MID": monitor.id})
