import json

import httpx
import pytest

from reviewbot.github_client import GitHubClient


@pytest.mark.asyncio
async def test_github_client_posts_inline_review_comment_and_check_run() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/repos/owner/repo/pulls/7/comments":
            assert json.loads(request.content) == {
                "body": "finding",
                "commit_id": "head-7",
                "path": "src/app.py",
                "line": 12,
                "side": "RIGHT",
                "start_line": 10,
                "start_side": "RIGHT",
            }
            return httpx.Response(201, json={"id": 501})
        if request.url.path == "/repos/owner/repo/check-runs":
            assert json.loads(request.content) == {
                "name": "oh-my-robot review",
                "head_sha": "head-7",
                "status": "completed",
                "conclusion": "failure",
                "output": {"title": "oh-my-robot review", "summary": "summary"},
            }
            return httpx.Response(201, json={"id": 601})
        return httpx.Response(404, json={"message": "unexpected path"})

    client = GitHubClient(
        base_url="https://api.github.test",
        token="github-token",
        transport=httpx.MockTransport(handler),
    )
    try:
        inline_id = await client.create_pull_request_review_comment(
            "owner/repo",
            7,
            body="finding",
            commit_id="head-7",
            path="src/app.py",
            line=12,
            start_line=10,
        )
        check_id = await client.create_check_run(
            "owner/repo",
            head_sha="head-7",
            name="oh-my-robot review",
            status="completed",
            conclusion="failure",
            summary="summary",
        )
    finally:
        await client.close()

    assert inline_id == 501
    assert check_id == 601
    assert [request.method for request in requests] == ["POST", "POST"]
