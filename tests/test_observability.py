"""Structured logging and request correlation.

Two properties matter and are easy to regress:

* A `ContextVar`, not a thread-local. Starlette runs sync endpoints in a
  threadpool, so a thread-local would leave every request id blank for exactly
  the endpoints that are hardest to debug.
* The formatter replaces root handlers rather than adding one. Uvicorn installs
  its own, and adding a second handler means every line is emitted twice — once
  structured and once not.
"""

import json
import logging
from contextvars import ContextVar
from pathlib import Path

from app.core.observability import (
    JsonFormatter,
    TextFormatter,
    bind_request_id,
    configure_logging,
    get_logger,
    new_request_id,
    request_id_var,
)


def _text() -> TextFormatter:
    return TextFormatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s", "%H:%M:%S")


def _record(msg: str, level: int = logging.INFO) -> logging.LogRecord:
    return logging.LogRecord(
        name="couponbase.test",
        level=level,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=(),
        exc_info=None,
    )


# ---- Formatting ----


def test_json_formatter_emits_one_line_of_valid_json() -> None:
    # One object per line, no wrapping: this is the format a log shipper tails.
    payload = json.loads(JsonFormatter().format(_record("hello")))
    assert payload["message"] == "hello"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "couponbase.test"
    assert payload["ts"]
    assert "\n" not in JsonFormatter().format(_record("hello"))


def test_json_formatter_includes_the_request_id() -> None:
    # This is the field that ties an application log line to a trace, and the
    # reason the ContextVar exists at all.
    bind_request_id("req-abc")
    try:
        assert json.loads(JsonFormatter().format(_record("hi")))["request_id"] == "req-abc"
    finally:
        request_id_var.set("")


def test_json_formatter_omits_the_request_id_when_there_is_none() -> None:
    # Background work has no request. Emitting `"request_id": ""` on every line
    # would make "find me the requests with no id" a filter over empty strings
    # rather than a missing-field query.
    request_id_var.set("")
    assert "request_id" not in json.loads(JsonFormatter().format(_record("hi")))


def test_extra_fields_are_included() -> None:
    record = _record("job finished")
    record.job = "send_alerts"
    record.detail = "sent 3 alert(s)"
    payload = json.loads(JsonFormatter().format(record))
    assert payload["job"] == "send_alerts"
    assert payload["detail"] == "sent 3 alert(s)"


def test_json_formatter_survives_an_unserialisable_extra() -> None:
    # A `Decimal`, an exception object, a SQLAlchemy instance — none of these
    # should be able to take down the logger, which would silently stop all
    # observability precisely when something is already broken.
    record = _record("oops")
    record.thing = object()
    assert json.loads(JsonFormatter().format(record))["message"] == "oops"


def test_text_formatter_includes_level_and_message() -> None:
    # Constructed the way `configure_logging` does; a bare `TextFormatter()`
    # inherits the stdlib default of "%(message)s" and says nothing about the
    # level, which is the field you scan for.
    line = _text().format(_record("plain text"))
    assert "INFO" in line
    assert "plain text" in line


def test_text_formatter_prefixes_the_request_id() -> None:
    bind_request_id("req-abc")
    try:
        assert _text().format(_record("hello")).startswith("[req-abc] ")
    finally:
        request_id_var.set("")


def test_text_formatter_appends_extra_fields() -> None:
    record = _record("job finished")
    record.job = "send_alerts"
    assert "job=send_alerts" in _text().format(record)


def test_both_formatters_agree_on_the_core_fields() -> None:
    # An operator switching LOG_FORMAT between deploys must not have to relearn
    # how to read the line.
    assert json.loads(JsonFormatter().format(_record("same")))["message"] == "same"
    assert "same" in _text().format(_record("same"))


# ---- Configuration ----


def test_configure_replaces_root_handlers_rather_than_adding() -> None:
    # Uvicorn installs its own handlers at startup. Appending would mean every
    # line ships twice, once structured and once not, which is expensive and
    # makes log aggregation ambiguous.
    root = logging.getLogger()
    existing = list(root.handlers)
    try:
        configure_logging(level="INFO", fmt="json")
        once = len(root.handlers)
        configure_logging(level="INFO", fmt="json")
        assert len(root.handlers) == once
    finally:
        root.handlers = existing


def test_configure_respects_the_requested_level() -> None:
    root = logging.getLogger()
    existing = list(root.handlers)
    existing_level = root.level
    try:
        configure_logging(level="WARNING", fmt="text")
        assert root.level == logging.WARNING
    finally:
        root.handlers = existing
        root.setLevel(existing_level)


def test_configure_does_not_disable_existing_library_loggers() -> None:
    # Only uvicorn's access log is silenced, and only because it duplicates what
    # our own middleware now records with a request id attached.
    configure_logging(level="INFO", fmt="text")
    assert logging.getLogger("uvicorn.access").disabled is True
    assert logging.getLogger("sqlalchemy.engine").disabled is False


# ---- Request ids ----


def test_new_request_ids_are_unique() -> None:
    assert len({new_request_id() for _ in range(100)}) == 100


def test_new_request_ids_are_hex_and_url_safe() -> None:
    # The id travels in a header and in a JSON log field; anything needing
    # quoting in either is a nuisance to grep.
    value = new_request_id()
    assert value
    assert all(c in "0123456789abcdef" for c in value)


def test_request_id_is_a_context_var_not_a_thread_local() -> None:
    # Starlette runs sync endpoints in a threadpool. A thread-local would leave
    # the id blank on exactly the endpoints that are hardest to debug, and would
    # leak one request's id into whatever the pooled thread serves next.
    assert isinstance(request_id_var, ContextVar)


def test_bind_returns_a_token_that_restores_the_previous_value() -> None:
    bind_request_id("outer")
    token = bind_request_id("inner")
    assert request_id_var.get() == "inner"
    request_id_var.reset(token)
    assert request_id_var.get() == "outer"
    request_id_var.set("")


def test_get_logger_returns_a_named_logger() -> None:
    assert get_logger("couponbase.example").name == "couponbase.example"


# ---- Prometheus alert rules ----
#
# A rule that names a metric the application does not register never fires, and
# Prometheus says nothing about it: no error, no warning, the alert just sits
# there looking configured. Every metric referenced by the rules file is
# therefore checked against the real registry here, which is the only place the
# two can be compared.


def _registered_metric_names() -> set[str]:
    """Every metric name the application actually exposes.

    Parsed out of the rendered exposition payload rather than out of
    `metrics.py` or `registry.collect()`, because that payload is literally what
    Prometheus scrapes. It matters: `prometheus_client` strips the `_total`
    suffix from a Counter's internal name and re-adds it on output, so
    `collect()` reports `couponbase_job_runs` where the wire format says
    `couponbase_job_runs_total`. Checking against `collect()` would reject rules
    that are perfectly correct.
    """
    import re

    from app.core.metrics import render

    payload, _ = render()
    names = re.findall(r"^# TYPE (\S+)", payload.decode(), re.MULTILINE)
    # `_created` is an internal bookkeeping gauge prometheus_client adds to every
    # counter. Nothing should ever alert on it.
    return {name for name in names if not name.endswith("_created")}


def _metrics_named_in_rules() -> set[str]:
    import re

    rules = Path(__file__).resolve().parent.parent / "ops" / "prometheus-rules.yml"
    if not rules.exists():
        return set()
    text = rules.read_text()
    # Histogram sub-series (`_bucket`, `_sum`, `_count`) are generated by the
    # client library from the histogram's own name, so the base name is what
    # has to be registered.
    return {
        re.sub(r"_(bucket|sum|count)$", "", name) for name in re.findall(r"couponbase_[a-z_]+", text)
    }


def test_every_metric_named_in_the_alert_rules_exists() -> None:
    named = _metrics_named_in_rules()
    assert named, "the rules file is empty or has moved — this test should find it"
    missing = sorted(named - _registered_metric_names())
    assert not missing, (
        f"prometheus-rules.yml references metrics the app never registers: {missing}. "
        "Such a rule never fires, and nothing reports that."
    )


def test_the_alert_rules_parse() -> None:
    import yaml

    rules = Path(__file__).resolve().parent.parent / "ops" / "prometheus-rules.yml"
    document = yaml.safe_load(rules.read_text())
    assert document["groups"], "no rule groups defined"
    # Every group must actually contain rules; an empty group is a placeholder
    # that reads as coverage in a review.
    for group in document["groups"]:
        assert group.get("rules"), f"group {group.get('name')} has no rules"


def test_the_alert_rules_parse_the_metrics_they_reference() -> None:
    import yaml

    rules = Path(__file__).resolve().parent.parent / "ops" / "prometheus-rules.yml"
    document = yaml.safe_load(rules.read_text())
    for group in document["groups"]:
        for rule in group["rules"]:
            assert rule.get("alert"), f"{group['name']} has a rule with no alert name"
            assert rule.get("expr"), f"{rule['alert']} has no expression"
            assert rule["annotations"].get("summary"), f"{rule['alert']} has no summary"
            assert rule["annotations"].get("description"), (
                f"{rule['alert']} has no description. A summary alone is not enough to "
                "act on at 3am; it says what broke but not what to do."
            )
    # The staleness rules are the ones that matter most, so they must exist and
    # must key off the timestamp gauge rather than a rate.
    names = {r["alert"] for g in document["groups"] for r in g["rules"]}
    assert {"CouponbaseAlertsNotRunning", "CouponbaseSchedulerNeverStarted"} <= names
