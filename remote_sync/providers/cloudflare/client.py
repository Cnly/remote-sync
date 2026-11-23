from ...http import API, APIStatusError
from ...utils import require_env


class CloudflareError(RuntimeError):
    def __init__(self, method, path, error):
        self.status = error.response.status_code
        try:
            errors = error.response.json()["errors"]
        except (ValueError, KeyError, TypeError):
            errors = []
        errors = [item for item in errors if isinstance(item, dict)] if isinstance(errors, list) else []
        self.codes = [item.get("code") for item in errors]
        detail = ", ".join(f"{item.get('code')}: {item.get('message')}" for item in errors)
        super().__init__(f"Cloudflare {method} {path}: {error} {detail}".rstrip())


class CloudflareClient:
    def __init__(self, *, api=None):
        self.api = api or API("https://api.cloudflare.com/client/v4", require_env("CLOUDFLARE_API_TOKEN"))

    def request(self, method, path, *, missing_entrypoint=False, **kwargs):
        try:
            response = self.api.request(method, path, **kwargs)
        except APIStatusError as exc:
            error = CloudflareError(method, path, exc)
            if missing_entrypoint and error.status == 404 and error.codes == [10003]:
                return None
            raise error from None
        assert response.get("success") is True, f"Cloudflare {method} {path} failed: {response.get('errors')}"
        return response

    def pages(self, path, **params):
        records = []
        page = 1
        while True:
            response = self.request("GET", path, params=params)
            rows = response["result"]
            assert isinstance(rows, list), f"Expected a Cloudflare list from {path}."
            records.extend(rows)
            info = response.get("result_info") or {}
            assert info.get("page", page) == page, f"Unexpected Cloudflare page for {path}."
            total_pages, total_count = info.get("total_pages"), info.get("total_count")
            if total_pages is None and total_count is None:
                if not rows or len(rows) != info.get("per_page"):
                    return records
            elif page >= (total_pages or 0) and len(records) >= (total_count or 0):
                return records
            assert rows, f"Incomplete Cloudflare list from {path}."
            page += 1
            params = params | {"page": page}

    def zone_id(self, name):
        page = self.request("GET", "/zones", params={"name": name, "per_page": 50})
        zones = page["result"]
        info = page.get("result_info", {})
        assert (len(zones) == 1 and zones[0]["name"].lower() == name and
                info.get("total_count", 1) == 1 and info.get("total_pages", 1) == 1), (
            f"Expected one visible Cloudflare zone named {name}; check the name and token scope."
        )
        return zones[0]["id"]
