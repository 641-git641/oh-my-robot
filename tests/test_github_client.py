import json

import httpx
import pytest

from reviewbot.github_client import GitHubApiError, GitHubClient


@pytest.mark.asyncio
async def test_github_adapter_reads_pull_request_and_posts_issue_comment() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/repos/owner/repo/pulls/1/files":
            return httpx.Response(
                200,
                json=[
                    {
                        "filename": "src/example.ts",
                        "status": "modified",
                        "patch": "@@ -1 +1,2 @@\n+const value = 1",
                        "additions": 1,
                        "deletions": 0,
                    },
                    {
                        "filename": "README.md",
                        "status": "added",
                        "patch": "@@ -0,0 +1 @@\n+readme",
                        "additions": 1,
                        "deletions": 0,
                    },
                ],
            )
        if request.url.path == "/repos/owner/repo/issues/1/comments":
            if request.method == "GET":
                return httpx.Response(200, json=[])
            assert request.headers["content-type"].startswith("application/json")
            assert json.loads(request.content) == {"body": "Review"}
            return httpx.Response(201, json={"id": 123})
        return httpx.Response(
            200,
            json={
                "title": "Example",
                "body": "description",
                "state": "open",
                "draft": False,
                "head": {"sha": "head-1", "ref": "feature"},
                "base": {"sha": "base-1", "ref": "main"},
                "user": {"login": "contributor"},
                "html_url": "https://github.com/owner/repo/pull/1",
            },
        )

    client = GitHubClient(
        base_url="https://api.github.test",
        token="github-token",
        transport=httpx.MockTransport(handler),
    )
    try:
        pull_request = await client.get_pull_request("owner/repo", 1)
        comments = await client.list_pull_request_comments("owner/repo", 1)
        files = await client.list_pull_request_files("owner/repo", 1)
        comment_id = await client.create_pull_request_comment("owner/repo", 1, "Review")
    finally:
        await client.close()

    assert pull_request.head_sha == "head-1"
    assert pull_request.base_sha == "base-1"
    assert pull_request.base_ref == "main"
    assert files[0].filename == "src/example.ts"
    assert files[0].patch == "@@ -1 +1,2 @@\n+const value = 1"
    assert files[0].additions == 1
    assert files[0].deletions == 0
    assert files[1].patch == "@@ -0,0 +1 @@\n+readme"
    assert comments == []
    assert comment_id == 123
    assert all(request.url.params.get("access_token") is None for request in requests)
    assert all(request.headers["authorization"] == "Bearer github-token" for request in requests)
    assert all(request.headers["accept"] == "application/vnd.github+json" for request in requests)
    assert all(request.headers["x-github-api-version"] == "2022-11-28" for request in requests)


@pytest.mark.asyncio
async def test_github_adapter_paginates_changed_files() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        page = int(request.url.params["page"])
        if page == 1:
            return httpx.Response(
                200,
                json=[
                    {
                        "filename": f"src/file-{index}.py",
                        "status": "modified",
                        "patch": "",
                        "additions": 0,
                        "deletions": 0,
                    }
                    for index in range(100)
                ],
            )
        return httpx.Response(
            200,
            json=[
                {
                    "filename": "src/last.py",
                    "status": "modified",
                    "patch": "",
                    "additions": 0,
                    "deletions": 0,
                }
            ],
        )

    client = GitHubClient(
        base_url="https://api.github.test",
        token="github-token",
        transport=httpx.MockTransport(handler),
    )
    try:
        files = await client.list_pull_request_files("owner/repo", 1)
    finally:
        await client.close()

    assert len(files) == 101
    assert [request.url.params["page"] for request in requests] == ["1", "2"]
    assert all(request.url.params["per_page"] == "100" for request in requests)


@pytest.mark.asyncio
async def test_github_adapter_preserves_rate_limit_metadata() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            403,
            headers={
                "retry-after": "7",
                "x-ratelimit-remaining": "0",
                "x-ratelimit-reset": "2000000000",
            },
            json={"message": "API rate limit exceeded"},
        )

    client = GitHubClient(
        base_url="https://api.github.test",
        token="github-token",
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(GitHubApiError) as raised:
            await client.get_pull_request("owner/repo", 1)
    finally:
        await client.close()

    error = raised.value
    assert error.status_code == 403
    assert error.rate_limited
    assert error.retry_after == 7
    assert error.rate_limit_remaining == 0
    assert error.rate_limit_reset == 2_000_000_000
