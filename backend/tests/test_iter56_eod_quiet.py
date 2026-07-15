"""iter-56 · EOD quiet window — EA v1.41 client guard + backend signal gate.

Spreads widen drastically across liquidity providers in the final minutes
before the daily close. No open/modify/close from 23:40 to 00:05 broker time.
"""
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from eod_quiet import is_eod_quiet, eod_quiet_block  # noqa: E402

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EA_PATH = os.path.join(BACKEND, "static", "EmergentTradingBridge.mq5")


def utc(h, m):
    return datetime(2026, 7, 6, h, m, tzinfo=timezone.utc)


class TestIsEodQuiet:
    def test_inside_window_no_offset(self):
        assert is_eod_quiet(0, now=utc(23, 40)) is True
        assert is_eod_quiet(0, now=utc(23, 55)) is True
        assert is_eod_quiet(0, now=utc(0, 0)) is True
        assert is_eod_quiet(0, now=utc(0, 4)) is True

    def test_outside_window_no_offset(self):
        assert is_eod_quiet(0, now=utc(23, 39)) is False
        assert is_eod_quiet(0, now=utc(0, 5)) is False
        assert is_eod_quiet(0, now=utc(12, 0)) is False

    def test_broker_offset_plus3(self):
        # Broker UTC+3: 20:40 UTC == 23:40 broker time → quiet
        off = 3 * 3600
        assert is_eod_quiet(off, now=utc(20, 40)) is True
        assert is_eod_quiet(off, now=utc(21, 4)) is True   # 00:04 broker
        assert is_eod_quiet(off, now=utc(21, 5)) is False  # 00:05 broker
        assert is_eod_quiet(off, now=utc(20, 39)) is False
        # 23:40 UTC == 02:40 broker → NOT quiet for a UTC+3 broker
        assert is_eod_quiet(off, now=utc(23, 40)) is False

    def test_negative_offset(self):
        # Broker UTC-5: 04:40 UTC == 23:40 broker time → quiet
        off = -5 * 3600
        assert is_eod_quiet(off, now=utc(4, 40)) is True
        assert is_eod_quiet(off, now=utc(4, 39)) is False


class TestEodQuietBlock:
    def test_blocks_with_learned_offset(self):
        acc = {"broker_utc_offset_sec": 3 * 3600}
        reason = eod_quiet_block_at(acc, utc(20, 45))
        assert reason is not None and "EOD quiet window" in reason

    def test_allows_outside(self):
        acc = {"broker_utc_offset_sec": 3 * 3600}
        assert eod_quiet_block_at(acc, utc(12, 0)) is None

    def test_none_account_falls_back_to_utc(self):
        assert eod_quiet_block_at(None, utc(23, 45)) is not None
        assert eod_quiet_block_at(None, utc(12, 0)) is None


def eod_quiet_block_at(account, now):
    from unittest.mock import patch
    with patch("eod_quiet.datetime") as md:
        md.now.return_value = now
        return eod_quiet_block(account)


class TestEaWiring:
    def setup_method(self):
        self.src = open(EA_PATH).read()

    def test_version_141(self):
        from ea_version import current_ea_version
        v = current_ea_version()
        assert f'#property version   "{v}"' in self.src
        assert f'#define EA_CLIENT_VERSION "{v}"' in self.src

    def test_inputs_present(self):
        assert "input bool   EodQuietEnabled" in self.src
        assert 'input string EodQuietStart          = "23:40"' in self.src
        assert 'input string EodQuietEnd            = "00:05"' in self.src

    def test_helper_uses_broker_time(self):
        assert "bool IsEodQuietWindow()" in self.src
        # TimeCurrent() (broker server time), never TimeLocal/TimeGMT
        helper = self.src[self.src.index("bool IsEodQuietWindow()"):]
        helper = helper[:helper.index("}\n\nvoid OnTimer") if "}\n\nvoid OnTimer" in helper else 1500]
        assert "TimeToStruct(TimeCurrent()" in helper

    def test_poll_skipped_in_window(self):
        poll = self.src[self.src.index("void PollPendingTrades()"):]
        poll = poll[:poll.index("ParseTradesBlock")]
        assert "IsEodQuietWindow()" in poll

    def test_all_order_functions_guarded(self):
        for fn in ("void ExecuteTrade(", "void ClosePosition(",
                   "void ApplyModifySL(", "void ApplyPartialClose(",
                   "void ApplyFullClose("):
            body_start = self.src.index(fn)
            head = self.src[body_start:body_start + 250]
            assert "IsEodQuietWindow()" in head, f"{fn} missing EOD quiet guard"


class TestBackendWiring:
    def test_bot_runner_gate(self):
        src = open(os.path.join(BACKEND, "bot_runner.py")).read()
        assert "eod_quiet_block" in src and "eod_quiet_block" in src
        assert '"eod_quiet_block"' in src  # intel counter

    def test_versions_bumped_everywhere(self):
        from ea_version import current_ea_version
        v = current_ea_version()
        assert f'LATEST_EA = "{v}"' in open(os.path.join(BACKEND, "routes", "bot_routes.py")).read()
        assert f'LATEST_EA = "{v}"' in open(os.path.join(BACKEND, "routes", "diagnostic_routes.py")).read()
        assert f'"ea_latest_version": "{v}"' in open(os.path.join(BACKEND, "routes", "setup_routes.py")).read()
        assert f'LATEST_EA_VERSION = "{v}"' in open("/app/frontend/src/pages/Accounts.jsx").read()
        assert f'LATEST_EA_VERSION = "{v}"' in open("/app/frontend/src/components/EaVersionStrip.jsx").read()
