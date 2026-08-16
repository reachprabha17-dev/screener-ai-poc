"""Reviewer interface (build gate 20 step 18).

The gate names four properties: **polls status**, **survives worker restart**,
**worker survives UI restart**, and **no DB driver importable**.

Three of them are now properties of the browser rather than of a process this
repository starts. The interface is a React app (`web/`): it polls
`GET /runs/{id}/status` on a timer, a reload is a page refresh with no server
state to lose, and it cannot open a database under any circumstances — there is
no UI process to hold a connection. `tests/test_layering.py` keeps the fourth as
a source rule (`fetch` in exactly one module, no raw HTML, no absolute URLs), and
the client's own contract tests live beside it in `web/src/api/client.test.ts`,
run by `npm run test`.

What is left for Python is the **seam**: this process serves the bundle, and the
two ways that can break are both invisible until someone opens a browser. A
deep-linked client-side route must return `index.html` rather than a 404, and it
must never shadow an API path that shares its prefix.
"""

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from config.settings import settings
from screener.api.app import create_app

INDEX = "<!doctype html><title>Enterprise Talent Screener</title><div id=root></div>"


@pytest.fixture
def built_ui(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A stand-in for `npm run build` output."""
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(INDEX, encoding="utf-8")
    (dist / "assets" / "index.js").write_text("export const x = 1;\n", encoding="utf-8")
    monkeypatch.setattr(settings, "web_dist_dir", str(dist))
    return dist


@pytest.fixture
def client(built_ui: Path) -> Iterator[TestClient]:
    with TestClient(create_app(check_migrations=False)) as test_client:
        yield test_client


def test_the_bundle_is_served_at_ui(client: TestClient) -> None:
    response = client.get("/ui/")

    assert response.status_code == 200
    assert "Enterprise Talent Screener" in response.text


def test_a_client_side_route_deep_links(client: TestClient) -> None:
    """`/ui/runs/run-1/review` is a route in the browser, not a file on disk.

    Without `html=True` this is a 404, and the symptom is specific and confusing:
    the app works until someone reloads the page or shares a link.
    """
    response = client.get("/ui/runs/run-1/review")

    assert response.status_code == 200
    assert "id=root" in response.text


def test_assets_are_served(client: TestClient) -> None:
    assert client.get("/ui/assets/index.js").status_code == 200


@pytest.mark.parametrize("path", ["/audit", "/runs", "/health", "/positions"])
def test_the_ui_never_shadows_an_api_route(client: TestClient, path: str) -> None:
    """The reason the bundle is under `/ui/` and not `/`.

    `/audit` and `/runs` are both API endpoints *and* screens in the app. Served
    from the root, whichever was registered first would win — and a reviewer's
    bookmark would start returning JSON the day somebody added an endpoint.

    Asserted against the route table rather than by making the request: every one
    of these paths reaches the database, and what is being checked here is which
    handler owns the path, not what it answers.
    """
    scope = {"type": "http", "method": "GET", "path": path, "headers": []}
    matched = [
        route
        for route in client.app.routes  # type: ignore[attr-defined]
        if route.matches(scope)[0].value >= 2  # Match.FULL
    ]

    assert matched, f"no route serves {path}"
    assert all(getattr(r, "name", "") != "reviewer_ui" for r in matched)


def test_the_bundle_is_not_cached(client: TestClient) -> None:
    """The `no-store` middleware covers the mount too.

    The HTML names the hashed asset it needs; a proxy holding yesterday's copy
    serves a page that asks for a chunk which no longer exists.
    """
    assert client.get("/ui/").headers["Cache-Control"] == "no-store"


def test_the_api_serves_without_a_built_ui(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A source checkout that has not run `npm run build` is not a broken API.

    In development the Vite dev server holds the bundle and proxies here, so the
    absence of `web/dist` is the normal case rather than a misconfiguration.
    """
    monkeypatch.setattr(settings, "web_dist_dir", str(tmp_path / "never-built"))

    with TestClient(create_app(check_migrations=False)) as test_client:
        assert test_client.get("/ui/").status_code == 404
        assert test_client.get("/health").status_code in (200, 503)
