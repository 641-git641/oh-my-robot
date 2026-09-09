from robomp.github_events import route

ALLOWLIST = frozenset({"owner/repo"})


def test_issue_open_event_queues_triage() -> None:
    decision = route(
        "issues",
        {
            "action": "opened",
            "repository": {"full_name": "owner/repo"},
            "issue": {
                "number": 12,
                "title": "Login fails",
                "user": {"login": "contributor"},
                "author_association": "CONTRIBUTOR",
            },
        },
        allowlist=ALLOWLIST,
        bot_login="roboomp",
    )

    assert decision.should_queue
    assert decision.task == "triage_issue"
    assert decision.issue_key == "owner/repo#12"
    assert decision.submitter == "contributor"


def test_issue_comment_requires_maintainer_mention_for_directive() -> None:
    decision = route(
        "issue_comment",
        {
            "action": "created",
            "repository": {"full_name": "owner/repo"},
            "issue": {"number": 12, "user": {"login": "contributor"}},
            "comment": {
                "body": "@roboomp explain the failing login flow",
                "user": {"login": "maintainer"},
                "author_association": "MEMBER",
            },
        },
        allowlist=ALLOWLIST,
        bot_login="roboomp",
        maintainers=frozenset({"maintainer"}),
    )

    assert decision.should_queue
    assert decision.task == "handle_comment"
    assert decision.directive
    assert decision.directive_body == "explain the failing login flow"
    assert decision.directive_authorizes_impl


def test_issue_comment_from_untrusted_user_is_still_read_only() -> None:
    decision = route(
        "issue_comment",
        {
            "action": "created",
            "repository": {"full_name": "owner/repo"},
            "issue": {"number": 12, "user": {"login": "contributor"}},
            "comment": {
                "body": "@roboomp fix this now",
                "user": {"login": "contributor"},
                "author_association": "CONTRIBUTOR",
            },
        },
        allowlist=ALLOWLIST,
        bot_login="roboomp",
    )

    assert decision.should_queue
    assert decision.task == "handle_comment"
    assert not decision.directive
    assert decision.directive_body is None
