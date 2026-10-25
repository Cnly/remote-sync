from copy import deepcopy
import json
from pathlib import Path

import pytest


@pytest.fixture
def website():
    # Shape follows the official v3 OpenAPI GET /uptime-monitors example.
    return json.loads((Path(__file__).parent / "fixtures/website.json").read_text())


@pytest.fixture
def monitor():
    return {
        "name": "Example API", "target": "https://example.com/health", "keyword": "ready",
        "category": "production", "timeout": 10, "check_frequency": 1,
        "contact_list_id": "b" * 32, "locations": ["new_york", "amsterdam", "singapore"],
        "public_report": False, "public_target": False, "max_redirects": 5,
        "http_method": "GET", "accepted_http_codes": [200],
        "verify_ssl_certificate": True, "verify_ssl_hostname": True,
        "number_of_tries": 3, "triggering_locations": None, "alert_after_minutes": 0,
        "repeat_alert_times": 0, "repeat_alert_frequency": 0,
    }


class FakeAPI:
    token = "test-secret-never-print"

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, path, **kwargs):
        self.calls.append((method, path, deepcopy(kwargs)))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return deepcopy(response)


def het_page(items, *, page=1, total=None, next_page=None):
    return {"monitors": items, "meta": {
        "total_filtered": len(items) if total is None else total,
        "returned": len(items), "pagination": {"current": page, "next": next_page},
    }}
