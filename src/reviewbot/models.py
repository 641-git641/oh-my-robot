from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Priority = Literal["P0", "P1", "P2", "P3"]
ReviewVerdict = Literal["clean", "needs_attention"]
EventState = Literal["queued", "running", "succeeded", "failed", "skipped", "superseded"]
EventSource = Literal["webhook", "refresh", "replay", "manual"]
FindingStatus = Literal["new", "active", "resolved", "relocated"]


class PullRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    repository: str
    number: int = Field(gt=0)
    title: str
    body: str = ""
    state: str = "open"
    draft: bool = False
    author: str = "unknown"
    head_sha: str
    head_ref: str = ""
    base_sha: str
    base_ref: str = "master"
    html_url: str = ""


class ChangedFile(BaseModel):
    model_config = ConfigDict(extra="ignore")

    filename: str = Field(min_length=1)
    status: str = "modified"
    patch: str = ""
    additions: int = Field(default=0, ge=0)
    deletions: int = Field(default=0, ge=0)


class ReviewFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    priority: Priority
    path: str = Field(min_length=1)
    line: int = Field(gt=0)
    end_line: int | None = Field(default=None, gt=0)
    symbol: str = "unknown"
    title: str = Field(min_length=1, max_length=180)
    problem: str = Field(min_length=1, max_length=2_000)
    impact: str = Field(min_length=1, max_length=1_000)
    suggestion: str = Field(min_length=1, max_length=2_000)
    confidence: float = Field(ge=0, le=1)


class ReviewResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1, max_length=4_000)
    verdict: ReviewVerdict
    rank: Priority
    findings: list[ReviewFinding] = Field(default_factory=list, max_length=20)
    test_suggestions: list[str] = Field(default_factory=list, max_length=10)


class PullRequestComment(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int
    body: str = ""


class EventRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    delivery_id: str
    event_type: str
    action: str
    repository: str
    pull_request_number: int = Field(gt=0)
    webhook_head_sha: str = ""
    state: EventState
    attempts: int = Field(ge=0)
    last_error: str | None = None
    error_type: str | None = None
    source: EventSource
    source_delivery_id: str | None = None
    available_at: str
    started_at: str | None = None
    finished_at: str | None = None
    created_at: str
    updated_at: str


class ReviewRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    repository: str
    pull_request_number: int = Field(gt=0)
    head_sha: str
    comment_id: int | None = None
    created_at: str


class FindingRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    fingerprint: str
    repository: str
    pull_request_number: int = Field(gt=0)
    head_sha: str
    path: str = Field(min_length=1)
    line: int = Field(gt=0)
    end_line: int | None = Field(default=None, gt=0)
    priority: Priority
    title: str = Field(min_length=1, max_length=180)
    status: FindingStatus
    first_seen_sha: str
    last_seen_sha: str
    created_at: str
    updated_at: str

class ReviewJob(BaseModel):
    model_config = ConfigDict(frozen=True)

    delivery_id: str = Field(min_length=1)
    event_type: str = Field(min_length=1)
    action: str = Field(min_length=1)
    repository: str = Field(min_length=1)
    pull_request_number: int = Field(gt=0)
    webhook_head_sha: str = ""
