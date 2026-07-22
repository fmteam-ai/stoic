from datetime import datetime
from typing import Optional, List, Literal, Dict
from pydantic import BaseModel, EmailStr, Field, model_validator

RiskLevel = Literal["low", "medium", "high", "extreme"]
SignalAction = Literal["BUY", "SELL", "HOLD"]
TradeStatus = Literal["pending", "open", "closed", "cancelled", "failed"]


# ---------- Auth ----------
class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8)
    name: Optional[str] = None
    terms_agreed: bool = False
    terms_version: Optional[str] = None


class VerifyEmailRequest(BaseModel):
    token: str = Field(min_length=10, max_length=120)


class ResendActivationRequest(BaseModel):
    email: EmailStr


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    token: str = Field(min_length=10, max_length=120)
    new_password: str = Field(min_length=8)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str
    totp_code: Optional[str] = None  # required if user has 2FA enabled


class ProfileUpdateRequest(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=120)


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8)


class TOTPVerifyRequest(BaseModel):
    code: str = Field(min_length=6, max_length=16)


class TOTPDisableRequest(BaseModel):
    current_password: str
    code: str = Field(min_length=6, max_length=16)


class UserOut(BaseModel):
    id: str
    email: str
    name: Optional[str] = None
    role: str = "user"
    created_at: Optional[datetime] = None
    two_factor_enabled: bool = False
    email_verified: bool = True


# ---------- Symbols ----------
class SymbolCreate(BaseModel):
    symbol: str  # e.g. XAUUSD, BTCUSD, EURUSD
    display_name: str
    asset_type: Literal["forex", "crypto", "commodity", "stock"] = "forex"


class SymbolOut(BaseModel):
    id: str
    symbol: str
    display_name: str
    asset_type: str
    enabled: bool = True
    created_at: Optional[datetime] = None


# ---------- MT5 Accounts ----------
class AccountCreate(BaseModel):
    label: str
    broker: str
    server: str
    account_number: str
    account_type: Literal["microcent", "cent", "standard", "demo"] = "microcent"
    base_currency: str = "USD"
    mode: Literal["live", "paper"] = "live"
    initial_balance: float = 10000.0  # only used for paper accounts
    investor_password: Optional[str] = None  # encrypted at rest — read-only MT5 password
    master_password: Optional[str] = None    # encrypted at rest — full-trade MT5 password


class AccountCredsUpdate(BaseModel):
    investor_password: Optional[str] = None
    master_password: Optional[str] = None


class AccountOut(BaseModel):
    id: str
    label: str
    broker: str
    server: str
    account_number: str
    account_type: str
    base_currency: str
    bridge_token: str
    status: Literal["disconnected", "connected"] = "disconnected"
    balance: float = 0.0
    equity: float = 0.0
    last_heartbeat: Optional[datetime] = None
    created_at: Optional[datetime] = None
    has_investor_password: bool = False
    has_master_password: bool = False


# ---------- Bot config ----------
class BotConfigUpdate(BaseModel):
    # Conservative first-live defaults (audit C2/C3): low risk, one symbol,
    # one concurrent trade, spread filter ON. Users opt UP explicitly.
    risk_level: RiskLevel = "low"
    symbols: List[str] = ["XAUUSD"]
    active: bool = False
    max_concurrent_trades: int = Field(default=1, ge=1, le=10)
    auto_execute: bool = True
    # Profit Protection Suite
    breakeven_enabled: bool = True            # move SL to entry after +1R
    breakeven_trigger_r: float = Field(default=1.0, ge=0.1, le=10.0)
    partial_close_enabled: bool = True        # take 50% off at TP1
    partial_close_trigger_r: float = Field(default=1.0, ge=0.1, le=10.0)
    partial_close_fraction: float = Field(default=0.5, gt=0.0, lt=1.0)
    trailing_enabled: bool = True             # trail SL after partial close
    trailing_start_r: float = Field(default=1.5, ge=0.1, le=20.0)
    trailing_distance_r: float = Field(default=0.7, gt=0.0, le=10.0)
    daily_drawdown_pct: float = Field(default=3.0, gt=0.0, le=20.0)
    daily_drawdown_enabled: bool = True
    weekly_drawdown_pct: float = Field(default=7.0, gt=0.0, le=40.0)
    weekly_drawdown_enabled: bool = True
    # Daily profit target (iter-65) — upside mirror of the drawdown breaker.
    # When today's realised P&L reaches `daily_profit_target_r * R_$`, the bot
    # either locks the profit (`lock` mode — subsequent sizing uses
    # equity minus locked amount, so the locked $ can't be lost), or pauses
    # the bot until 00:00 UTC next day (`stop` mode). Resets daily at 00:00 UTC.
    # `0` / `null` = disabled.
    daily_profit_target_r: float = Field(default=0.0, ge=0.0, le=20.0)
    daily_profit_target_action: Literal["lock", "stop"] = "lock"
    # Spread Filter — ON by default (audit C3): spread is a first-order
    # execution cost; thresholds are per-symbol and broker-tunable.
    spread_filter_enabled: bool = True
    max_spread_pips: Dict[str, float] = Field(
        default_factory=lambda: {"XAUUSD": 50.0, "BTCUSD": 100.0}
    )
    # Friday Flat guard (iter-52) — weekend gap protection. Before the
    # Friday 21:00 UTC weekly close: close all open non-crypto positions
    # ("close", EA v1.40+) or tighten their SL ("tighten"), and block new
    # entries for the window.
    friday_flat_enabled: bool = True
    friday_flat_mode: str = "close"          # "close" | "tighten"
    friday_flat_minutes_before: int = 60
    # Auto-Tune — let the bot raise the min-confidence threshold based on
    # historical win-rates per (symbol, confidence-bucket).
    auto_tune_enabled: bool = True
    # Offline RL policy (iter-61): off | advisory | enforce
    rl_policy_mode: str = "advisory"
    # Chronos forecast gate (iter-62): off | advisory | enforce
    forecast_gate_mode: str = "advisory"
    # Master Agent consensus (iter-63): off | advisory | enforce
    consensus_gate_mode: str = "enforce"
    consensus_threshold: int = Field(default=55, ge=0, le=100)
    # Probabilistic forecast layer (iter-64): off | advisory | enforce
    prob_forecast_mode: str = "advisory"
    # Bayesian decision gate (iter-65): off | advisory | enforce
    bayes_gate_mode: str = "advisory"
    # Stacked ML ensemble (iter-108): off | advisory | enforce
    ml_ensemble_mode: str = "advisory"
    # Liquidity Mapping gate (iter-105): advisory | enforce
    liquidity_gate_mode: str = "enforce"
    # AI News Understanding gate (iter-106): advisory | enforce
    news_gate_mode: str = "enforce"
    # Calendar Intelligence gate (iter-107): advisory | enforce
    calendar_intel_mode: str = "enforce"
    # Meta-Learning strategy switcher (iter-109) — bandit picks the preset
    # that is actually winning right now; auto-switches when it stops.
    meta_strategy_enabled: bool = False
    # Uncertainty estimation (iter-110): skip low-confidence trades.
    uncertainty_gate_mode: str = "enforce"   # off | advisory | enforce
    min_calibrated_confidence: int = Field(default=60, ge=1, le=99)
    # Adaptive Position Sizing (iter-111) — risk% per trade scales with
    # confidence, volatility, recent accuracy, liquidity and drawdown.
    adaptive_sizing_enabled: bool = True
    adaptive_risk_floor_pct: float = Field(default=0.1, gt=0.0, le=5.0)
    adaptive_risk_cap_pct: float = Field(default=2.0, gt=0.0, le=5.0)
    # Monte Carlo trade simulation (iter-112): enter only on positive EV.
    monte_carlo_mode: str = "enforce"        # off | advisory | enforce
    monte_carlo_paths: int = Field(default=10000, ge=100, le=100000)
    # Advanced Risk Engine (iter-114): dynamic leverage, event-exposure caps,
    # daily/weekly/monthly drawdown ladder, abnormal-market halt, CVaR budget.
    risk_engine_enabled: bool = True
    monthly_drawdown_pct: float = 12.0
    max_leverage: float = 20.0
    cvar_budget_pct: float = 8.0
    event_exposure_cap_pct: float = 100.0
    # Slippage veto — force-close fills whose actual entry deviated more than
    # this many pips from the signal's intended entry. Hard guard against
    # ECN bad-fills during news.
    slippage_veto_enabled: bool = True
    max_slippage_pips: Dict[str, float] = Field(
        default_factory=lambda: {"XAUUSD": 20.0, "BTCUSD": 80.0}
    )
    # Capital-preservation guards
    anti_tilt_enabled: bool = True
    anti_tilt_consecutive_losses: int = 3
    anti_tilt_freeze_hours: int = 4
    trade_of_day_cap: int = 1   # max NEW trades per symbol per UTC day; 0 = unlimited
    asia_session_skip_xau: bool = True
    # Per-symbol cooldown after a stop-loss hit — prevents revenge-regime re-entry
    sl_cooldown_enabled: bool = True
    sl_cooldown_minutes: int = 45
    # Loss cooldown (iter-58/122b) — after a BOT loss, pause same
    # symbol+direction re-entries on THIS account for N minutes.
    loss_cooldown_enabled: bool = True
    loss_cooldown_minutes: int = 30
    # Final R:R guard (iter-119) — skip trades whose post-overlay weighted
    # geometry falls below this floor. 0 = disabled.
    min_final_rr: float = 0.75
    # Payoff guard — clamp SL to at most this multiple of the TP1 distance.
    payoff_guard_enabled: bool = True
    payoff_guard_max_sl_tp1: float = 1.2
    # Pre-news existing-position protector — flatten OPEN trades into imminent HIGH-impact events
    pre_news_protect_enabled: bool = True
    pre_news_protect_minutes: int = 5
    # iter-67: stricter MTF gate
    # Custom minimum-confidence override (1-95). 0 = use the risk_level default.
    min_confidence_override: int = 0
    # Per-account lot-size cap. 0 = uncapped (use signal's computed lot size).
    # Hard ceiling — even if AI computes a larger lot, this clamps it.
    max_lot_size: float = Field(default=0.0, ge=0.0, le=100.0)
    # iter-39 — Paper Shadow Mode: when active=False but paper_shadow_mode=True,
    # the bot_runner still generates signals (origin="shadow") but never executes.
    # Lets users A/B-test their config for weeks without risking capital.
    paper_shadow_mode: bool = False
    # iter-39 — Per-account crypto risk cap override (% of equity per trade).
    # Overrides the global env CRYPTO_MAX_RISK_PCT_PER_TRADE. None → fall back to env.
    crypto_risk_pct_per_trade: Optional[float] = None
    # ───── iter-74 · Win-Rate Adaptive Mode (Phases 1-3) ────────────────
    # Phase 1 — Profit-taking shape: how the bot harvests winners.
    #   "expected_value" (default) → ATR-driven TPs, existing partial/trail.
    #   "win_rate"                 → tight partials (0.5R/70%), tight trail,
    #                                hard TP cap (100 pips default) → maximises
    #                                count of green trades.
    #   "trend_follow"             → wide partials (1.5R/30%), wide trail —
    #                                lets winners stretch.
    profit_taking_mode: str = "expected_value"
    # Per-symbol hard ceiling on TP distance (pips). 0 / missing = uncapped.
    # Applies regardless of profit_taking_mode, but win_rate mode also
    # supplies a 100-pip default when this dict is empty.
    max_tp_pips_per_symbol: Dict[str, float] = Field(default_factory=dict)
    # Phase 2 — Rolling adaptive risk. When enabled, the bot scales the
    # risk_pct by a multiplier derived from the last-N closed trades'
    # win rate. Multiplier ∈ [0.5, 1.3]; defaults to 1.0 below 5 samples.
    adaptive_risk_enabled: bool = False
    adaptive_risk_window: int = 20
    # Phase 3 — Regime-aware auto-preset. When enabled, each tick the bot
    # overlays the preset matching the live execution_mode (trend_rider,
    # fast_scalp, scalper, mean_reversion). User's explicit `active_preset`
    # is bypassed only while auto is enabled.
    auto_preset_enabled: bool = False
    # ───── iter-41 · Payoff-ratio repair (high win rate / oversized losers) ─
    # Soft-Stop: cut a losing trade early at a fraction of its SL distance
    # instead of riding to the full stop. Directly shrinks the average loss.
    soft_stop_enabled: bool = False
    soft_stop_loss_fraction: float = 0.6   # close at 60% of the SL distance
    soft_stop_min_minutes: int = 10        # min trade age before it may fire
    # Let Winners Run: at TP1 move SL to break-even WITHOUT banking 50% —
    # full size continues toward TP2/TP3. Raises the average win.
    let_winners_run: bool = False

    @model_validator(mode="after")
    def validate_relationships(self):
        if self.adaptive_risk_floor_pct > self.adaptive_risk_cap_pct:
            raise ValueError("adaptive risk floor cannot exceed cap")
        if (self.trailing_enabled and self.breakeven_enabled
                and self.trailing_start_r < self.breakeven_trigger_r):
            raise ValueError("trailing should start at or after breakeven")
        return self


class BotConfigOut(BotConfigUpdate):
    id: str
    user_id: str
    updated_at: Optional[datetime] = None


# ---------- Signals ----------
class SignalOut(BaseModel):
    id: str
    user_id: str
    symbol: str
    action: SignalAction
    confidence: float  # 0-100
    entry_price: float
    stop_loss: float
    take_profit: float
    lot_size: float
    risk_level: RiskLevel
    reasoning: str
    indicators: dict = {}
    consumed: bool = False
    created_at: datetime


# ---------- Trades ----------
class TradeOut(BaseModel):
    id: str
    user_id: str
    account_id: str
    signal_id: Optional[str] = None
    symbol: str
    action: SignalAction
    lot_size: float
    entry_price: float
    stop_loss: float
    take_profit: float
    exit_price: Optional[float] = None
    pnl: float = 0.0
    status: TradeStatus
    mt5_ticket: Optional[int] = None
    opened_at: Optional[datetime] = None
    closed_at: Optional[datetime] = None
    error: Optional[str] = None


# ---------- Bridge (MT5 EA <-> server) ----------
class BridgePosition(BaseModel):
    """A single MT5 open position reported in a heartbeat snapshot.

    The EA fills these from PositionsTotal() on every tick so STOIC always
    has the full live picture — including positions that were opened BEFORE
    the EA was attached (legacy trades the OnTradeTransaction handler never
    saw) or opened manually directly on MT5.
    """
    ticket: int                 # position ticket (long)
    symbol: str
    type: Literal["BUY", "SELL"]
    volume: float = Field(gt=0)
    price_open: float
    sl: float = 0.0
    tp: float = 0.0
    time_open: int = 0          # unix seconds
    magic: int = 0              # 0 = manual broker-side
    profit: float = 0.0         # current floating P&L
    # EA v1.27+: broker-live tick (PositionGetDouble(POSITION_PRICE_CURRENT)).
    # When present, the Trades UI overlays this onto the row INSTEAD of the
    # 5s-polled external-feed quote → display matches MT5 to the tick.
    current_price: Optional[float] = None


class BridgeSymbolSpec(BaseModel):
    """EA v1.48+: per-symbol broker stop-placement constraints so the backend
    can respect precise minimum stop / freeze distances (round 13 item 5).
    EA v1.49+ adds trade_mode (SYMBOL_TRADE_MODE: 0=disabled 1=long-only
    2=short-only 3=close-only 4=full) for order-direction preflight."""
    point: float = 0.0
    digits: int = 0
    stops_level_points: float = 0.0
    freeze_level_points: float = 0.0
    trade_mode: Optional[int] = None


class BridgeHeartbeat(BaseModel):
    bridge_token: str
    balance: float
    equity: float
    open_positions: int = 0
    # EA v1.22+: full ticket list of currently-open MT5 positions on this account.
    # When provided, the server reconciles DB-open trades against this list and
    # auto-closes any orphans (e.g. SL hit but trade-close report was missed).
    open_tickets: Optional[list[int]] = None
    spreads: Optional[Dict[str, float]] = None  # symbol -> spread in pips (EA v1.21+)
    # EA v1.24+: the MT5 account number the EA is currently logged into. We
    # cross-check this against the configured `account_number` on the STOIC
    # account — when they diverge, we surface a "wrong terminal" warning so
    # the user catches the case of "both EAs attached to the same MT5 instance"
    # which makes two STOIC accounts mirror the same balance.
    account_login: Optional[int] = None
    base_currency: Optional[str] = None         # broker's reported account currency
    # EA v1.25+: full snapshot of every open position. Lets STOIC auto-create
    # trade records for positions that were open BEFORE the EA was attached
    # or were opened manually directly on MT5 (not just newly-fired deals via
    # OnTradeTransaction). Backfills the "I see 4 trades on MT5 but only 0
    # on STOIC" gap.
    positions: Optional[list[BridgePosition]] = None
    # EA v1.48+: per-symbol broker constraints (SYMBOL_TRADE_STOPS_LEVEL /
    # SYMBOL_TRADE_FREEZE_LEVEL in points + point size) — round 13 item 5.
    symbol_specs: Optional[Dict[str, BridgeSymbolSpec]] = None
    # EA v1.26+: the EA's own semantic version string. Drives the "EA Version"
    # badge on the Dashboard so the user can tell at a glance which terminals
    # are running stale builds (e.g. missing the autonomous deal-history sweep).
    client_version: Optional[str] = None
    # EA v1.34+: MarketWatch symbol inventory (filtered to instruments we
    # care about — XAU/BTC/forex majors). Drives the iter-76 broker
    # symbol-suffix auto-detector so the bot routes orders with the
    # right name (`XAUUSD.fx`, `XAUUSD.e`, etc.) without manual setup.
    available_symbols: Optional[list[str]] = None


class BridgeTradeReport(BaseModel):
    bridge_token: str
    trade_id: str
    mt5_ticket: Optional[int] = None
    status: TradeStatus
    entry_price: Optional[float] = None
    requested_price: Optional[float] = None  # EA v1.40+ — price at OrderSend
    exit_price: Optional[float] = None
    pnl: Optional[float] = None
    error: Optional[str] = None
    requested_sl: Optional[float] = None          # EA v1.50 — SL as commanded
    applied_sl: Optional[float] = None            # EA v1.50 — SL post broker clamp
    confirmed_position_sl: Optional[float] = None  # EA v1.50 — live POSITION_SL
    replay: Optional[bool] = None                 # EA v1.50 — journal re-report
    order_ticket: Optional[int] = None            # EA v1.52 — broker ORDER ticket
    deal_ticket: Optional[int] = None             # EA v1.52 — executed DEAL ticket
    position_id: Optional[int] = None             # EA v1.52 — POSITION identifier
    filled_volume: Optional[float] = None         # EA v1.52 — actual filled lots
    partial_fill: Optional[bool] = None           # EA v1.52 — DONE_PARTIAL open
    position_volume: Optional[float] = None       # EA v1.53 — netted symbol position after fill


class BridgeExternalDeal(BaseModel):
    """Raw MT5 deal event reported via OnTradeTransaction.

    Covers BOTH bot-initiated and broker-side manual trades. Idempotency is
    keyed by `deal_id` (broker's unique deal identifier) so the same event
    can never be double-applied even if the EA retries.
    """
    bridge_token: str
    mt5_ticket: int                       # position ticket
    deal_id: int                          # unique broker deal id
    deal_entry: Literal["in", "out", "inout"]
    symbol: str
    action: Literal["BUY", "SELL"]
    lots: float = Field(gt=0)
    price: float                          # deal fill price
    profit: float = 0.0
    commission: float = 0.0
    swap: float = 0.0
    deal_time: int = 0                    # unix seconds (broker time)
    backfill: bool = False                # EA v1.40+: history-sweep / deep-sync push
    magic: int = 0                        # 0 = manual broker-side; else our MagicNumber
    position_volume: Optional[float] = None  # EA v1.45+: broker's REMAINING position volume after this deal


# ---------- Market ----------
class QuoteOut(BaseModel):
    symbol: str
    price: float
    change: float
    change_pct: float
    high: Optional[float] = None
    low: Optional[float] = None
    timestamp: datetime
    cached: bool = False
