from collections import Counter
from copy import deepcopy

import pytest

from conftest import FakeAPI, het_page
from remote_sync.cli import print_plan
from remote_sync.providers.hetrixtools import HetrixTools
from remote_sync.http import API
from test_core import apply, plan
from test_http import Response, Session


OWNERSHIP_MARKER = "nix-sync-test--api"


class HetrixAPI:
    """Keep monitor settings and private notes separate, as the real APIs do."""

    token = "test-token"

    def __init__(self, website, *, notes=OWNERSHIP_MARKER, empty=False):
        self.template = deepcopy(website)
        self.monitors = {} if empty else {website["id"]: deepcopy(website)}
        self.notes = {} if empty else {website["id"]: notes}
        self.calls = []
        self.fail_append = False
        self.commit_append = False
        self.fail_update = False
        self.next_id = int("c" * 32, 16)

    @property
    def writes(self):
        return [call for call in self.calls if call[0] != "GET"]

    def request(self, method, path, **kwargs):
        self.calls.append((method, path, deepcopy(kwargs)))
        if path == "/v3/uptime-monitors":
            assert method == "GET"
            assert set(kwargs["params"]) == {"page", "per_page"}
            return het_page(deepcopy(list(self.monitors.values())))
        if path.endswith("/private-notes"):
            remote_id = path.split("/")[-2]
            if method == "GET":
                return {"private_notes": self.notes[remote_id]}
            assert method == "POST" and set(kwargs["json"]) == {"private_notes"}
            if not self.fail_append or self.commit_append:
                previous = self.notes[remote_id]
                self.notes[remote_id] = previous + ("\n" if previous else "") + kwargs["json"]["private_notes"]
            if self.fail_append:
                raise RuntimeError("Uncertain private-note response")
            return {"status": "success", "message": "private notes updated"}
        assert method == "POST"
        payload = kwargs["json"]
        if path.endswith("/uptime/delete/"):
            action = "deleted"
            remote_id = payload["MID"]
            del self.monitors[remote_id]
            del self.notes[remote_id]
        else:
            if path.endswith("/uptime/add/"):
                # The real add endpoint ignores MID and always creates a monitor.
                action = "added"
                remote_id = f"{self.next_id:032x}"
                self.next_id += 1
                self.monitors[remote_id] = {**self.template, "id": remote_id}
                self.notes[remote_id] = ""
            else:
                assert path.endswith("/uptime/edit/")
                action = "updated"
                remote_id = payload["MID"]
                assert remote_id in self.monitors
                if self.fail_update:
                    raise RuntimeError("Monitor update failed")
            self.monitors[remote_id].update(name=payload["Name"], target=payload["Target"],
                                            http_method=payload["Method"], keyword=payload["Keyword"])
        return {"status": "SUCCESS", "monitor_id": remote_id, "action": action}


def provider(api):
    return HetrixTools(api=api)


def test_readable_name_is_independent_of_private_ownership(website, monitor, capsys):
    api = HetrixAPI(website, notes="Operator-only context\n" + OWNERSHIP_MARKER)
    monitor["name"] = "A Better Public Name"
    remote, desired, diff = plan(provider(api), {"api": monitor})
    print_plan(diff)
    output = capsys.readouterr().out
    assert "A Better Public Name" in output and "Operator-only context" not in output
    assert not api.writes
    apply(remote, desired, diff)
    assert api.monitors[website["id"]]["name"] == monitor["name"]
    assert api.notes[website["id"]] == "Operator-only context\n" + OWNERSHIP_MARKER
    assert len(api.writes) == 1 and api.writes[0][1].endswith("/uptime/edit/")
    assert not plan(provider(api), {"api": monitor})[2].has_diffs()


def test_explicit_adoption_preserves_id_and_notes_while_reconciling_settings(website, monitor):
    website["name"] = "Manual Monitor"
    monitor["keyword"] = "healthy"
    notes = "  Operator note: café\nKeep this line.\n"
    api = HetrixAPI(website, notes=notes)
    remote, desired, diff = plan(provider(api), {"api": monitor}, adoptions={"api": website["id"]})
    assert not api.writes
    apply(remote, desired, diff)
    assert api.monitors == {website["id"]: {**website, "name": "Example API", "keyword": "healthy"}}
    assert api.notes[website["id"]] == notes + "\n" + OWNERSHIP_MARKER
    assert len(api.writes) == 2
    assert api.writes[0][1].endswith("/private-notes")
    assert api.writes[0][2]["json"] == {"private_notes": OWNERSHIP_MARKER}
    assert api.writes[1][1].endswith("/uptime/edit/")
    assert not plan(provider(api), {"api": monitor}, adoptions={"api": website["id"]})[2].has_diffs()
    assert not plan(provider(api), {"api": monitor})[2].has_diffs()


@pytest.mark.parametrize("explicit", [False, True])
@pytest.mark.parametrize("notes", ["nix-sync-another--api", "nix-sync-test--different"])
def test_adoption_cannot_take_over_note_ownership(website, monitor, notes, explicit):
    api = HetrixAPI(website, notes=notes)
    adoptions = {"api": website["id"]} if explicit else None
    with pytest.raises(AssertionError, match="refusing to take"):
        plan(provider(api), {"api": monitor}, adoptions=adoptions)
    assert not api.writes


@pytest.mark.parametrize("notes", [OWNERSHIP_MARKER + "\n" + OWNERSHIP_MARKER, "nix-sync-invalid"])
def test_ambiguous_or_malformed_note_markers_fail_closed(website, notes):
    api = HetrixAPI(website, notes=notes)
    with pytest.raises(AssertionError, match="ownership marker"):
        plan(provider(api), {})
    assert not api.writes


def test_display_names_are_not_ownership_markers(website, monitor):
    website["name"] = monitor["name"] = "nix-sync-another--api"
    api = HetrixAPI(website, notes="")
    assert not plan(provider(api), {})[2].has_diffs()
    apply(*plan(provider(api), {"api": monitor}))
    assert api.notes[website["id"]] == OWNERSHIP_MARKER
    assert not plan(provider(api), {"api": monitor})[2].has_diffs()


def test_note_substrings_do_not_grant_ownership(website):
    api = HetrixAPI(website, notes="Mentioned " + OWNERSHIP_MARKER + " in a sentence.")
    assert not plan(provider(api), {})[2].has_diffs()
    assert not api.writes


@pytest.mark.parametrize("response", [{}, {"private_notes": None}, AssertionError("API returned HTTP 403")])
def test_missing_or_unreadable_notes_abort_discovery(website, monitor, response):
    api = FakeAPI(het_page([website]), response)
    with pytest.raises((AssertionError, KeyError, AttributeError)):
        plan(provider(api), {"api": monitor})
    assert all(method == "GET" for method, _, _ in api.calls)


@pytest.mark.parametrize("mode", ["owned", "explicit", "automatic"])
def test_unrelated_note_edits_after_preview_are_preserved(website, monitor, mode):
    api = HetrixAPI(website, notes=OWNERSHIP_MARKER if mode == "owned" else "")
    if mode != "automatic":
        monitor["name"] = "Renamed"
    adoptions = {"api": website["id"]} if mode == "explicit" else None
    remote, desired, diff = plan(provider(api), {"api": monitor}, adoptions=adoptions)
    api.notes[website["id"]] += "\nNew operator note"
    apply(remote, desired, diff)
    expected = OWNERSHIP_MARKER + "\nNew operator note" if mode == "owned" else "\nNew operator note\n" + OWNERSHIP_MARKER
    assert api.notes[website["id"]] == expected


def test_duplicate_note_ownership_aborts_discovery(website, monitor):
    api = HetrixAPI(website)
    api.monitors["b" * 32] = {**website, "id": "b" * 32}
    api.notes["b" * 32] = OWNERSHIP_MARKER
    with pytest.raises(AssertionError, match="Multiple owned resources"):
        plan(provider(api), {"api": monitor})
    assert not api.writes


def test_deletion_only_prunes_note_owned_monitors(website):
    api = HetrixAPI(website)
    for remote_id, name, notes in [("b" * 32, "Manual", ""), ("c" * 32, "Foreign", "nix-sync-another--api")]:
        api.monitors[remote_id] = {**website, "id": remote_id, "name": name}
        api.notes[remote_id] = notes
    remote, desired, diff = plan(provider(api), {})
    assert not api.writes
    apply(remote, desired, diff)
    assert set(api.monitors) == {"b" * 32, "c" * 32}


def test_create_uses_readable_name_then_appends_ownership(website, monitor):
    api = HetrixAPI(website, empty=True)
    apply(*plan(provider(api), {"api": monitor}))
    assert api.monitors["c" * 32]["name"] == monitor["name"]
    assert api.notes["c" * 32] == OWNERSHIP_MARKER
    assert len(api.writes) == 2
    assert api.writes[0][2]["json"]["Name"] == monitor["name"]
    assert api.writes[1][2]["json"] == {"private_notes": OWNERSHIP_MARKER}
    assert not plan(provider(api), {"api": monitor})[2].has_diffs()


@pytest.mark.parametrize("committed", [False, True])
def test_partial_creation_is_recoverable_without_duplicates(website, monitor, committed):
    api = HetrixAPI(website, empty=True)
    api.fail_append, api.commit_append = True, committed
    with pytest.raises(RuntimeError, match="Created monitor " + "c" * 32):
        apply(*plan(provider(api), {"api": monitor}))
    assert list(api.monitors) == ["c" * 32] and len(api.writes) == 2
    if committed:
        assert not plan(provider(api), {"api": monitor})[2].has_diffs()
    else:
        api.fail_append = False
        apply(*plan(provider(api), {"api": monitor}))
        assert not plan(provider(api), {"api": monitor})[2].has_diffs()
    assert sum(call[1].endswith("/uptime/add/") for call in api.writes) == 1


@pytest.mark.parametrize("change_settings", [False, True])
@pytest.mark.parametrize("explicit", [False, True])
def test_exact_match_is_adopted_and_reconciled_in_one_apply(website, monitor, capsys, change_settings, explicit):
    api = HetrixAPI(website, notes="Operator context")
    if change_settings:
        monitor["name"] = "New Display Name"
        monitor["keyword"] = "healthy"
    adoptions = {"api": website["id"]} if explicit else None
    remote, desired, diff = plan(provider(api), {"api": monitor}, adoptions=adoptions)
    print_plan(diff, remote)
    output = capsys.readouterr().out
    assert f"ADOPT api ({website['id']})" in output
    assert '"ownership": null' in output and OWNERSHIP_MARKER in output
    assert "Operator context" not in output and "CREATE" not in output
    assert ("healthy" in output) == change_settings
    assert not api.writes
    apply(remote, desired, diff)
    assert list(api.monitors) == [website["id"]]
    assert api.notes[website["id"]] == "Operator context\n" + OWNERSHIP_MARKER
    assert api.monitors[website["id"]]["name"] == monitor["name"]
    assert api.monitors[website["id"]]["keyword"] == monitor["keyword"]
    assert len(api.writes) == (2 if change_settings else 1)
    assert api.writes[0][1].endswith("/private-notes")
    assert not plan(provider(api), {"api": monitor})[2].has_diffs()


@pytest.mark.parametrize("field, value", [("http_method", "HEAD"), ("target", "https://example.com/health/")])
def test_automatic_adoption_requires_target_and_method_to_match_exactly(website, monitor, field, value):
    website[field] = value
    api = HetrixAPI(website, notes="")
    remote, _, diff = plan(provider(api), {"api": monitor})
    assert not remote.pending_adoptions and diff.summary()["create"] == 1
    assert not api.writes


@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_adoption_distinguishes_methods_at_the_same_target(website, monitor, method):
    api = HetrixAPI(website, notes="")
    api.monitors["b" * 32] = {**website, "id": "b" * 32, "http_method": "HEAD", "keyword": ""}
    api.notes["b" * 32] = ""
    monitor["http_method"] = method
    if method == "HEAD":
        monitor["keyword"] = ""
    apply(*plan(provider(api), {"api": monitor}))
    chosen, other = (website["id"], "b" * 32) if method == "GET" else ("b" * 32, website["id"])
    assert api.notes[chosen] == OWNERSHIP_MARKER and api.notes[other] == ""
    assert len(api.writes) == 1
    assert not plan(provider(api), {"api": monitor})[2].has_diffs()


def test_duplicate_matching_monitors_require_explicit_adoption(website, monitor):
    api = HetrixAPI(website, notes="")
    api.monitors["b" * 32] = {**website, "id": "b" * 32, "name": "Another name"}
    api.notes["b" * 32] = ""
    with pytest.raises(AssertionError, match="Multiple monitors match"):
        plan(provider(api), {"api": monitor})
    assert not api.writes
    apply(*plan(provider(api), {"api": monitor}, adoptions={"api": website["id"]}))
    assert api.notes[website["id"]] == OWNERSHIP_MARKER and api.notes["b" * 32] == ""


def test_explicit_and_automatic_adoptions_are_reconciled_together(website, monitor):
    api = HetrixAPI(website, notes="")
    second = {**website, "id": "b" * 32, "target": "https://example.com/second"}
    api.monitors[second["id"]], api.notes[second["id"]] = second, ""
    declarations = {"api": monitor, "second": {**monitor, "target": second["target"], "name": "Second"}}
    remote, desired, diff = plan(provider(api), declarations, adoptions={"api": website["id"]})
    apply(remote, desired, diff)
    assert api.notes == {website["id"]: OWNERSHIP_MARKER, second["id"]: "nix-sync-test--second"}
    assert api.monitors[second["id"]]["name"] == "Second"
    assert not plan(provider(api), declarations)[2].has_diffs()


def test_one_monitor_cannot_be_automatically_adopted_under_multiple_keys(website, monitor):
    api = HetrixAPI(website, notes="")
    with pytest.raises(AssertionError, match="Multiple declarations"):
        plan(provider(api), {"api": monitor, "second": {**monitor, "name": "Another name"}})
    assert not api.writes


@pytest.mark.parametrize("field, value", [("target", "https://example.com/new-health"), ("http_method", "HEAD")])
def test_owned_monitor_identity_survives_target_and_method_changes(website, monitor, field, value):
    api = HetrixAPI(website)
    monitor.update({field: value, "keyword": ""})
    remote, desired, diff = plan(provider(api), {"api": monitor})
    assert not remote.pending_adoptions and diff.summary()["create"] == 0
    apply(remote, desired, diff)
    assert list(api.monitors) == [website["id"]]
    assert api.monitors[website["id"]][field] == value
    assert api.notes[website["id"]] == OWNERSHIP_MARKER
    assert len(api.writes) == 1 and api.writes[0][1].endswith("/uptime/edit/")
    assert not plan(provider(api), {"api": monitor})[2].has_diffs()


def test_create_does_not_reread_after_discovery(website, monitor):
    api = HetrixAPI(website, empty=True)
    remote, desired, diff = plan(provider(api), {"api": monitor})
    reads = list(api.calls)
    apply(remote, desired, diff)
    assert [call for call in api.calls if call[0] == "GET"] == reads
    assert not remote.diff_from(desired).has_diffs()


@pytest.mark.parametrize("count", [1, 7])
def test_batch_adoption_scans_once_and_preserves_ids(website, monitor, count):
    api = HetrixAPI(website, empty=True)
    declarations, adoptions = {}, {}
    for index in range(count):
        key, remote_id = f"api-{index}", f"{index + 1:032x}"
        target = f"https://example.com/{index}"
        api.monitors[remote_id] = {**website, "id": remote_id, "target": target}
        api.notes[remote_id] = "Operator context"
        declarations[key] = {**monitor, "target": target, "keyword": "healthy"}
        if index % 2:
            adoptions[key] = remote_id
    foreign_id = "f" * 32
    api.monitors[foreign_id] = {**website, "id": foreign_id}
    api.notes[foreign_id] = "nix-sync-other--api"
    original_ids = set(api.monitors)
    remote, desired, diff = plan(provider(api), declarations, adoptions=adoptions)
    apply(remote, desired, diff)
    listings = [kwargs["params"] for _, path, kwargs in api.calls if path == "/v3/uptime-monitors"]
    assert listings == [{"page": 1, "per_page": 200}]
    note_reads = Counter(path.split("/")[-2] for method, path, _ in api.calls
                         if method == "GET" and path.endswith("/private-notes"))
    assert note_reads == {remote_id: 1 for remote_id in original_ids}
    assert set(api.monitors) == original_ids
    assert sum(path.endswith("/uptime/edit/") for _, path, _ in api.writes) == count
    assert not any(path.endswith("/uptime/add/") for _, path, _ in api.writes)
    assert api.notes[foreign_id] == "nix-sync-other--api"
    assert not remote.diff_from(desired).has_diffs()


def test_rate_limited_adoption_resumes_without_rereading_or_replaying_previous_writes(website, monitor, monkeypatch):
    sleeps = []
    monkeypatch.setattr("remote_sync.http.time.sleep", sleeps.append)
    monitor["keyword"] = "healthy"
    existing = {**website, "id": "existing", "target": "https://example.com/existing"}
    ownership_marker = "nix-sync-test--misskey-d0nuts"
    session = Session(
        Response(200, het_page([existing, website])),
        Response(200, {"private_notes": "nix-sync-test--hookshot-d0nuts"}),
        Response(200, {"private_notes": "Operator context"}),
        Response(200, {"status": "SUCCESS", "monitor_id": existing["id"]}),
        Response(429, headers={"Retry-After": "9"}),
        Response(200, {"status": "success"}),
        Response(200, {"status": "SUCCESS", "monitor_id": website["id"]}),
    )
    provider = HetrixTools(api=API("https://example.com", "test-token", session=session))
    remote, desired, diff = plan(provider, {
        "hookshot-d0nuts": {**monitor, "target": existing["target"]},
        "misskey-d0nuts": monitor,
    })
    apply(remote, desired, diff)
    assert [args[0] for args, _ in session.calls] == ["GET"] * 3 + ["POST"] * 4
    assert session.calls[4] == session.calls[5]
    assert session.calls[5][1]["json"] == {"private_notes": ownership_marker}
    edits = [kwargs["json"]["MID"] for args, kwargs in session.calls if args[1].endswith("/uptime/edit/")]
    assert edits == [existing["id"], website["id"]]
    assert sum(sleeps) == 9
    assert not remote.pending_adoptions
    assert not remote.diff_from(desired).has_diffs()


@pytest.mark.parametrize("explicit", [False, True])
def test_failed_settings_update_after_adoption_is_recoverable(website, monitor, explicit):
    api = HetrixAPI(website, notes="")
    monitor["keyword"] = "healthy"
    api.fail_update = True
    adoptions = {"api": website["id"]} if explicit else None
    with pytest.raises(RuntimeError, match="Monitor update failed"):
        apply(*plan(provider(api), {"api": monitor}, adoptions=adoptions))
    assert api.notes[website["id"]] == OWNERSHIP_MARKER
    assert api.monitors[website["id"]] == website
    api.fail_update = False
    remote, desired, diff = plan(provider(api), {"api": monitor})
    assert not remote.pending_adoptions
    apply(remote, desired, diff)
    assert not plan(provider(api), {"api": monitor})[2].has_diffs()
    assert sum(call[1].endswith("/private-notes") for call in api.writes) == 1
