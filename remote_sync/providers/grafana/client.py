from ...http import API
from ...utils import require_env


class GrafanaClient:
    def __init__(self, *, url=None, namespace="default", api=None):
        if api is None:
            assert url, "Configure the Grafana instance URL."
            api = API(url.rstrip("/"), require_env("GRAFANA_SERVICE_ACCOUNT_TOKEN"))
        self.api = api
        self.namespace = namespace

    def request(self, method, path, **kwargs):
        try:
            return self.api.request(method, path, **kwargs)
        except (AssertionError, RuntimeError) as exc:
            # Grafana paths contain resource IDs, never credentials. Keep the
            # shared client's sanitized error rather than dumping response bodies.
            raise RuntimeError(f"Grafana {method} {path}: {exc}") from None

    def list_objects(self, path):
        objects, seen_tokens = {}, set()
        params = {"limit": 100}
        while True:
            page = self.request("GET", path, params=params)
            for obj in page["items"]:
                uid = obj["metadata"]["name"]
                assert uid not in objects, "Grafana returned duplicate resource names; preview again."
                objects[uid] = obj
            token = page.get("metadata", {}).get("continue")
            if not token:
                break
            assert token not in seen_tokens, "Grafana pagination repeated; preview again."
            seen_tokens.add(token)
            params = {"limit": 100, "continue": token}
        return objects

    def discover_version(self, group, versions=("v1beta1", "v0alpha1")):
        discovery = self.request("GET", f"/apis/{group}")
        available = {item["version"] for item in discovery["versions"]}
        version = next((v for v in versions if v in available), None)
        assert version, f"Grafana {group} must serve {' or '.join(versions)}."
        return f"{group}/{version}"
