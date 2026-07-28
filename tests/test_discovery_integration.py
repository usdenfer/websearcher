"""End-to-end coverage for body-only structured discovery."""

from fastapi.testclient import TestClient

import server
from server import app


KEYWORD = "BODY-ONLY-8472"


async def _no_expansion(keywords, host):
    del keywords, host
    return []


def _search(client: TestClient, discovery_site: str, depth: int):
    return client.post("/api/search", json={
        "startUrl": discovery_site + "/",
        "keywords": [KEYWORD],
        "depth": depth,
        "render": "off",
    })


def test_body_only_keyword_is_found_independent_of_bfs_depth(
    discovery_site, monkeypatch,
):
    monkeypatch.setattr(server, "expand_keywords", _no_expansion)
    with TestClient(app) as client:
        for depth in (1, 2, 3):
            response = _search(client, discovery_site, depth)

            assert response.status_code == 200
            data = response.json()
            urls = {item["pageUrl"] for item in data["results"]}
            assert discovery_site + "/deep/article.html" in urls
            assert discovery_site + "/navigation.html" not in urls
            assert all(
                KEYWORD not in item["pageTitle"]
                for item in data["results"]
            )
            assert all(
                KEYWORD not in item["pageUrl"]
                for item in data["results"]
            )
            assert data["discovery"]["profile"] == "freecms"
            assert "category" in data["discovery"]["sourcesSucceeded"]
            assert data["discovery"]["partial"] is False


def test_failed_api_falls_back_to_category(discovery_site, monkeypatch):
    monkeypatch.setattr(server, "expand_keywords", _no_expansion)

    with TestClient(app) as client:
        response = _search(client, discovery_site, depth=1)

    assert response.status_code == 200
    data = response.json()
    urls = {item["pageUrl"] for item in data["results"]}
    assert discovery_site + "/deep/article.html" in urls
    assert discovery_site + "/navigation.html" not in urls
    discovery = data["discovery"]
    assert "category" in discovery["sourcesSucceeded"]
    assert any("业务失败" in item for item in discovery["warnings"])
    assert discovery["partial"] is False
