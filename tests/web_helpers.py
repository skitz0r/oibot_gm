"""Shared helpers for the web tests."""


def post_confirmed(client, url: str, json: dict, **kw):
    """POST to an admin translator route the way a reviewed Save does (D51): a warn-class or consequence-bearing batch
    first answers `review_required` with the section revs; the second POST sends `confirmed: true` with those revs."""
    r = client.post(url, json=json, **kw)
    if r.status_code == 200 and r.json().get("review_required"):
        r = client.post(url, json={**json, "confirmed": True, "revs": {**(json.get("revs") or {}), **r.json()["revs"]}}, **kw)
    return r
