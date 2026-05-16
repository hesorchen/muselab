"""Token-based auth boundary."""


def test_no_token_rejected(client):
    r = client.get("/api/files/list?path=")
    assert r.status_code == 401


def test_bad_token_rejected(client):
    r = client.get("/api/files/list?path=", headers={"X-Auth-Token": "wrong"})
    assert r.status_code == 401


def test_good_token_accepted(client, auth):
    r = client.get("/api/files/list?path=", headers=auth)
    assert r.status_code == 200
    assert r.json()["entries"]


def test_query_token_for_raw_endpoint(client):
    # /raw uses query-string token; missing => 401, present => 200
    bad = client.get("/api/files/raw?path=README.md&token=wrong")
    assert bad.status_code == 401


def test_query_token_correct(client):
    from .conftest import TEST_TOKEN
    r = client.get(f"/api/files/raw?path=README.md&token={TEST_TOKEN}")
    assert r.status_code == 200
