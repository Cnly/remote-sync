import pytest
import requests

from remote_sync.http import API


class Response:
    def __init__(self, status, body=None, *, headers=None):
        self.status_code, self.body = status, body
        self.headers = requests.structures.CaseInsensitiveDict(headers or {})

    def json(self):
        return self.body


class Session:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def request(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def test_api_key_header_authentication_does_not_add_a_bearer_token():
    session = Session(Response(200, {"checks": []}))
    api = API("https://healthchecks.io/api/v3", session=session, headers={"X-Api-Key": "secret"})
    assert api.request("GET", "/checks/") == {"checks": []}
    assert session.calls[0][1]["headers"] == {"Accept": "application/json", "X-Api-Key": "secret"}


@pytest.mark.parametrize("status", [200, 204, 403])
def test_empty_responses_can_skip_json_but_still_check_http_status(status):
    response = requests.Response()
    response.status_code, response._content = status, b""
    session = Session(response)
    api = API("https://example.com", "secret", session=session)
    if status == 403:
        with pytest.raises(AssertionError, match="403"):
            api.request("DELETE", "/service", expect_json=False)
    else:
        assert api.request("DELETE", "/service", expect_json=False) is None
    assert "expect_json" not in session.calls[0][1]


@pytest.mark.parametrize("status", [400, 403, 409, 500, 502, 503, 504])
def test_mutations_are_not_retried_after_other_failures(status):
    session = Session(Response(status))
    with pytest.raises(AssertionError, match=str(status)):
        API("https://example.com", "secret", session=session).request("POST", "/create")
    assert len(session.calls) == 1


@pytest.mark.parametrize("statuses", [
    (503, 503, 503),
    (429,) * 10 + (503, 503, 503),
    (503,) + (429,) * 10 + (503, 503),
])
def test_transient_read_retries_are_bounded_independently_of_rate_limits(monkeypatch, statuses):
    monkeypatch.setattr("remote_sync.http.time.sleep", lambda _: None)
    session = Session(*(Response(status) for status in statuses))
    with pytest.raises(AssertionError, match=str(statuses[-1])):
        API("https://example.com", "secret", session=session).request("GET", "/resource")
    assert len(session.calls) == len(statuses)


@pytest.mark.parametrize("method", ["GET", "POST"])
@pytest.mark.parametrize(("headers", "delay"), [
    ({"Retry-After": "45"}, 45),
    ({"Retry-After": "Thu, 01 Jan 1970 00:17:25 GMT"}, 45),
    ({"ratelimit-remaining-user": "0", "ratelimit-reset-user": "1045"}, 45),
    ({"ratelimit-remaining-endpoint": "0", "ratelimit-reset-endpoint": "1045",
      "ratelimit-remaining-user": "10", "ratelimit-reset-user": "4600"}, 45),
    ({"ratelimit-remaining-endpoint": "0", "ratelimit-reset-endpoint": "1030",
      "ratelimit-remaining-user": "0", "ratelimit-reset-user": "1045"}, 45),
    ({"Retry-After": "30", "ratelimit-reset-endpoint": "1045"}, 45),
    ({"ratelimit-reset-user": "4600"}, 3600),
    ({}, 60),
    ({"Retry-After": "invalid", "ratelimit-reset-user": "NaN"}, 60),
])
def test_rate_limited_requests_keep_retrying_with_cooldown(monkeypatch, capsys, method, headers, delay):
    sleeps = []
    monkeypatch.setattr("remote_sync.http.time.time", lambda: 1000)
    monkeypatch.setattr("remote_sync.http.time.sleep", sleeps.append)
    session = Session(*(Response(429, headers=headers) for _ in range(10)), Response(200, {"status": "success"}))
    kwargs = {"json": {"private_notes": "nix-sync-test--api"}} if method == "POST" else {}
    assert API("https://example.com", "secret", session=session).request(method, "/resource", **kwargs) == {"status": "success"}
    assert sum(sleeps) == delay * 10
    assert len(session.calls) == 11
    assert session.calls == [session.calls[0]] * 11
    output = capsys.readouterr()
    assert output.out == ""
    operation = "read" if method == "GET" else "write"
    assert f"retrying {operation} in {delay} seconds" in output.err


def test_write_stops_on_server_error_after_a_rate_limit(monkeypatch):
    sleeps = []
    monkeypatch.setattr("remote_sync.http.time.sleep", sleeps.append)
    session = Session(Response(429, headers={"Retry-After": "9"}), Response(503))
    with pytest.raises(AssertionError, match="503"):
        API("https://example.com", "secret", session=session).request("POST", "/resource")
    assert len(session.calls) == 2
    assert sum(sleeps) == 9


@pytest.mark.parametrize("error_type", [requests.ConnectionError, requests.Timeout])
def test_requests_errors_do_not_retry_or_expose_tokens(error_type):
    session = Session(error_type("https://example.com/v2/secret-token/uptime/add"))
    with pytest.raises(RuntimeError) as error:
        API("https://example.com", "secret-token", session=session).request("POST", "/v2/secret-token/add")
    assert "secret-token" not in str(error.value)
    assert error.value.__suppress_context__
    assert len(session.calls) == 1
    assert session.calls[0][1]["allow_redirects"] is False
