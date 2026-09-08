from __future__ import annotations

import asyncio
import io
import shlex
import sys
import threading
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from zipfile import BadZipFile, ZipFile

from reviewbot.diff import DiffContext
from reviewbot.models import PullRequest, ReviewResult
from reviewbot.reviewer import ReviewExecution, ReviewFormatError, normalize_result

_SENSITIVE_ENV_KEYS = frozenset(
    {
        "GITHUB_TOKEN",
        "GITHUB_WEBHOOK_SECRET",
        "ROBOT_ADMIN_TOKEN",
        "DEEPSEEK_API_KEY",
        "ROBOMP_GH_PROXY_HMAC_KEY",
    }
)
_MAX_ARCHIVE_BYTES = 50_000_000
_MAX_EXTRACTED_BYTES = 200_000_000
_MAX_ARCHIVE_MEMBERS = 5_000


class OmpReviewError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


class OmpReadOnlyReviewer:
    def __init__(self, *, command: str, model: str | None, timeout_seconds: float) -> None:
        self._command = tuple(shlex.split(command))
        self._model = model
        self._timeout_seconds = timeout_seconds
        self._active_lock = threading.Lock()
        self._active_clients: set[object] = set()

    async def review_with_metadata(
        self,
        pull_request: PullRequest,
        diff: DiffContext,
        rules: str,
        archive: bytes,
    ) -> ReviewExecution:
        try:
            return await asyncio.to_thread(self._run, pull_request, diff, rules, archive)
        except asyncio.CancelledError:
            self._stop_active_clients()
            raise


    def _stop_active_clients(self) -> None:
        with self._active_lock:
            clients = tuple(self._active_clients)
        for client in clients:
            stop = getattr(client, "stop", None)
            if callable(stop):
                stop()

    def _register_client(self, client: object) -> None:
        with self._active_lock:
            self._active_clients.add(client)

    def _unregister_client(self, client: object) -> None:
        with self._active_lock:
            self._active_clients.discard(client)
    def _run(
        self,
        pull_request: PullRequest,
        diff: DiffContext,
        rules: str,
        archive: bytes,
    ) -> ReviewExecution:
        try:
            from omp_rpc import RpcClient, RpcError
        except ImportError as exc:
            raise OmpReviewError("deep review requires the omp-rpc package", retryable=False) from exc

        if not self._command:
            raise OmpReviewError("deep review command is empty", retryable=False)
        if len(archive) > _MAX_ARCHIVE_BYTES:
            raise OmpReviewError("PR archive exceeds the deep-review size limit", retryable=False)
        env_wrapper = (
            "'DEEPSEEK_API_KEY','ROBOMP_GH_PROXY_HMAC_KEY'}; "
            "[os.environ.pop(k,None) for k in list(os.environ) if k.upper() in blocked]; "
            "raise SystemExit(subprocess.call(sys.argv[1:], env=os.environ))"
        )
        with TemporaryDirectory(prefix="oh-my-robot-omp-") as temporary:
            worktree = _extract_archive(archive, Path(temporary))
            audit: list[dict[str, object]] = []
            session_id: str | None = None

            def on_tool_start(event: object) -> None:
                audit.append(
                    {
                        "event": "start",
                        "tool": str(getattr(event, "tool_name", "unknown")),
                        "tool_call_id": str(getattr(event, "tool_call_id", "")),
                    }
                )

            def on_tool_end(event: object) -> None:
                audit.append(
                    {
                        "event": "end",
                        "tool": str(getattr(event, "tool_name", "unknown")),
                        "tool_call_id": str(getattr(event, "tool_call_id", "")),
                        "is_error": bool(getattr(event, "is_error", False)),
                    }
                )

            client = RpcClient(
                executable=sys.executable,
                extra_args=("-c", env_wrapper, *self._command),
                cwd=worktree,
                session_dir=Path(worktree) / ".omp-session",
                model=self._model,
                no_skills=True,
                no_rules=True,
                tools=("read", "glob", "grep", "lsp"),
                startup_timeout=min(self._timeout_seconds, 30.0),
                request_timeout=min(self._timeout_seconds, 60.0),
            )
            client.on_tool_execution_start(on_tool_start)
            client.on_tool_execution_end(on_tool_end)
            self._register_client(client)
            try:
                try:
                    with client:
                        try:
                            state = client.get_state()
                            session_id = str(getattr(state, "session_id", "")) or None
                        except Exception:
                            session_id = None
                        turn = client.prompt_and_wait(
                            _review_prompt(pull_request, diff, rules),
                            timeout=self._timeout_seconds,
                        )
                        content = turn.require_assistant_text().strip()
                except RpcError as exc:
                    raise OmpReviewError("OMP RPC review failed", retryable=True) from exc
            finally:
                self._unregister_client(client)

        try:
            result = ReviewResult.model_validate_json(_strip_json_fence(content))
        except Exception as exc:
            raise ReviewFormatError("OMP response did not match the ReviewResult schema") from exc
        return ReviewExecution(
            normalize_result(result, diff),
            session_id=session_id,
            tool_audit=tuple(audit),
        )


def _extract_archive(archive: bytes, destination: Path) -> Path:
    try:
        with ZipFile(io.BytesIO(archive)) as bundle:
            members = bundle.infolist()
            if len(members) > _MAX_ARCHIVE_MEMBERS:
                raise ValueError("archive contains too many files")
            extracted_bytes = 0
            for member in members:
                relative = PurePosixPath(member.filename)
                if relative.is_absolute() or ".." in relative.parts:
                    raise ValueError("archive contains an unsafe path")
                if member.file_size > _MAX_EXTRACTED_BYTES:
                    raise ValueError("archive member exceeds the deep-review size limit")
                extracted_bytes += member.file_size
                if extracted_bytes > _MAX_EXTRACTED_BYTES:
                    raise ValueError("archive exceeds the deep-review extraction limit")
                target = (destination / Path(*relative.parts)).resolve()
                if destination.resolve() not in target.parents and target != destination.resolve():
                    raise ValueError("archive escaped its destination")
                if member.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(bundle.read(member))
    except BadZipFile as exc:
        raise ValueError("GitHub returned an invalid PR archive") from exc

    children = list(destination.iterdir())
    if len(children) == 1 and children[0].is_dir():
        return children[0]
    return destination


def _review_prompt(pull_request: PullRequest, diff: DiffContext, rules: str) -> str:
    return f"""You are a read-only GitHub Pull Request reviewer.

The repository is available in the current working directory. Use only read, glob, grep, and lsp tools.
Do not modify files, execute commands, access the network, or perform Git operations.

PR metadata:
repository: {pull_request.repository}
number: {pull_request.number}
title: {pull_request.title}
head: {pull_request.head_sha}
base: {pull_request.base_sha}

Review rules:
{rules[:8_000]}

Current PR Diff:
{diff.text}

Inspect the repository when needed, then return only valid JSON with this shape:
{{
  "summary": "简体中文技术总结",
  "verdict": "clean" or "needs_attention",
  "rank": "P0" or "P1" or "P2" or "P3",
  "findings": [{{
    "priority": "P0"|"P1"|"P2"|"P3",
    "path": "changed/path",
    "line": 1,
    "end_line": 1,
    "symbol": "symbol",
    "title": "标题",
    "problem": "具体问题",
    "impact": "可观察影响",
    "suggestion": "修复建议",
    "confidence": 0.0
  }}],
  "test_suggestions": ["测试建议"]
}}
Only report concrete issues that can be located in the current PR Diff.
"""


def _strip_json_fence(content: str) -> str:
    candidate = content.strip()
    if candidate.startswith("```") and candidate.endswith("```"):
        candidate = candidate[3:-3].strip()
        if candidate.lower().startswith("json"):
            candidate = candidate[4:].strip()
    return candidate
