import json
import logging

from reviewbot.logging_config import JsonFormatter


def test_json_formatter_preserves_event_fields() -> None:
    record = logging.LogRecord(
        name="reviewbot.test",
        level=logging.INFO,
        pathname="test.py",
        lineno=1,
        msg="review_succeeded",
        args=(),
        exc_info=None,
    )
    record.delivery_id = "delivery-1"
    record.finding_count = 2

    payload = json.loads(JsonFormatter().format(record))

    assert payload["event"] == "review_succeeded"
    assert payload["delivery_id"] == "delivery-1"
    assert payload["finding_count"] == 2
    assert payload["level"] == "INFO"
