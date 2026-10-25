import pytest

from conftest import FakeAPI, het_page
from remote_sync.core import APIAdapter, ConfigAdapter
from remote_sync.providers.hetrixtools import HetrixTools, Monitor


def test_hetrix_v3_normalization_matches_declaration(website, monitor):
    monitor["locations"].append("new_york")
    api = FakeAPI(het_page([website]), {"private_notes": "nix-sync-test--api"})
    remote = APIAdapter(provider=HetrixTools(api=api), owner="test", declarations={"api": monitor})
    assert not remote.diff_from(ConfigAdapter(desired=remote.desired)).has_diffs()
    assert all(method == "GET" for method, _, _ in api.calls)


def test_hetrix_update_clears_values_and_preserves_unmanaged_warnings(website, monitor):
    api = FakeAPI({"status": "SUCCESS", "monitor_id": website["id"]})
    provider = HetrixTools(api=api)
    monitor.update(keyword="", category="", contact_list_id="", locations=["amsterdam", "singapore"])
    settings = {"name": website["name"], **provider.desired(monitor)}
    provider.update(Monitor(website, api=api), settings)
    method, path, kwargs = api.calls[0]
    assert method == "POST" and path.endswith("/uptime/edit/")
    assert kwargs["bearer"] is False
    payload = kwargs["json"]
    assert payload["MID"] == website["id"]
    assert payload["Keyword"] == payload["Category"] == payload["ContactList"] == ""
    assert payload["Locations"] == {"ams": True, "sgp": True}
    assert payload["HTTPCodes"] == "200"
    assert payload["SSLExpiryReminder"] == 15
    assert payload["DomainExpiryReminder"] == 30
    assert payload["NSChangeAlert"] == 1
    assert "PrivateNotes" not in payload


def test_hetrix_edit_rejects_a_different_monitor_id(website, monitor):
    api = FakeAPI({"status": "SUCCESS", "monitor_id": "c" * 32})
    provider = HetrixTools(api=api)
    with pytest.raises(AssertionError, match="different monitor ID"):
        provider.update(Monitor(website, api=api), provider.desired(monitor))
    assert len(api.calls) == 1
    assert api.calls[0][1].endswith("/uptime/edit/")


def test_hetrix_unowned_matching_target_and_method_is_adopted_despite_name(website, monitor):
    website["name"] = "Manual Monitor"
    api = FakeAPI(het_page([website]), {"private_notes": ""})
    remote = APIAdapter(provider=HetrixTools(api=api), owner="test", declarations={"api": monitor})
    assert remote.pending_adoptions == {"api"}
    assert remote.diff_from(ConfigAdapter(desired=remote.desired)).summary()["create"] == 0
    assert all(method == "GET" for method, _, _ in api.calls)


@pytest.mark.parametrize("notes", ["", "nix-sync-test--api"])
def test_hetrix_incomplete_response_does_not_become_a_create(website, monitor, notes):
    del website["http_method"]
    with pytest.raises(KeyError, match="http_method"):
        APIAdapter(provider=HetrixTools(api=FakeAPI(het_page([website]), {"private_notes": notes})),
                   owner="test", declarations={"api": monitor})


def test_hetrix_defers_field_rules_to_the_api(website, monitor):
    api = FakeAPI({"status": "SUCCESS", "monitor_id": website["id"]})
    provider = HetrixTools(api=api)
    monitor.update(name="API / health", timeout=17, http_method="OPTIONS", keyword="x" * 200,
                   locations=["new_york", "san_francisco", "singapore", "tokyo"], triggering_locations=1)
    provider.update(Monitor(website, api=api), provider.desired(monitor))
    payload = api.calls[0][2]["json"]
    assert (payload["Name"], payload["Timeout"], payload["Method"]) == ("API / health", 17, "OPTIONS")
    assert payload["Keyword"] == "x" * 200
    assert payload["Locations"] == {"nyc": True, "sfo": True, "sgp": True, "tok": True}


@pytest.mark.parametrize("field, value", [("target", "https://different.example/"), ("http_method", "HEAD")])
def test_hetrix_explicit_adoption_checks_target_and_method(website, monitor, field, value):
    monitor[field] = value
    api = FakeAPI(het_page([website]), {"private_notes": ""})
    with pytest.raises(AssertionError, match="declared adoption target"):
        APIAdapter(provider=HetrixTools(api=api), owner="test", declarations={"api": monitor},
                   adoptions={"api": website["id"]})
    assert all(method == "GET" for method, _, _ in api.calls)


def test_hetrix_contact_list_must_be_explicit_or_supplied_at_runtime(monitor, monkeypatch):
    monitor["contact_list_id"] = None
    monkeypatch.delenv("HETRIXTOOLS_CONTACT_LIST_ID", raising=False)
    provider = HetrixTools(api=FakeAPI())
    with pytest.raises(AssertionError, match="HETRIXTOOLS_CONTACT_LIST_ID"):
        provider.desired(monitor)
    monkeypatch.setenv("HETRIXTOOLS_CONTACT_LIST_ID", "c" * 32)
    assert provider.desired(monitor)["contact_list_id"] == "c" * 32


def test_discovery_reads_all_pages(website):
    second = {**website, "id": "second", "name": "second"}
    api = FakeAPI(het_page([website], total=2, next_page=2), het_page([second], page=2, total=2))
    assert HetrixTools(api=api).list() == [website, second]
    assert [c[2]["params"]["page"] for c in api.calls] == [1, 2]
