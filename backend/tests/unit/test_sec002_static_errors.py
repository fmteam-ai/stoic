"""SEC-002 — no raw exception text in HTTP error details; http_errors contract."""
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

pytestmark = pytest.mark.unit
BACKEND = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
RAW = re.compile(r"detail=str\((e|exc|err)\)|\"detail\":\s*str\((e|exc|err)\)|detail=f\"[^\"]*\{(e|exc|err)\}")


def test_no_raw_exception_text_in_route_error_details():
    offenders = []
    for root in ("routes", "modules"):
        for dp, _, fns in os.walk(os.path.join(BACKEND, root)):
            for fn in fns:
                if fn.endswith(".py"):
                    path = os.path.join(dp, fn)
                    for i, line in enumerate(open(path, encoding="utf-8"), 1):
                        if RAW.search(line):
                            offenders.append(f"{os.path.relpath(path, BACKEND)}:{i}")
    assert offenders == [], offenders


def test_static_error_hides_message_and_logs_ref(caplog):
    from http_errors import static_error
    with caplog.at_level("WARNING", logger="stoic.http_errors"):
        exc = static_error(401, "agent_auth_failed", ValueError("secret internal detail xyz"))
    d = exc.detail
    assert exc.status_code == 401 and d["code"] == "agent_auth_failed" and d["message"] == "authentication failed"
    assert "secret" not in str(d) and len(d["ref"]) == 10
    assert any(d["ref"] in r.message and "secret internal detail xyz" in r.message for r in caplog.records)
    custom = static_error(409, "x", "boom", message="custom text")
    assert custom.detail["message"] == "custom text"
