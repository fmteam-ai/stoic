from datetime import datetime
from typing import Optional, List, Literal, Dict
from pydantic import BaseModel, EmailStr, Field

RiskLevel = Literal["low", "medium", "high", "extreme"]
SignalAction = Literal["BUY", "SELL", "HOLD"]
TradeStatus = Literal["pending", "open", "closed", "cancelled", "failed"]


# ---------- Auth ----------
class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=6)
    name: Optional[str] = None


class LoginRequest(BaseModel):
    email: EmailStr
    password: str
    totp_code: Optional[str] = None  # required if user has 2FA enabled


class ProfileUpdateRequest(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=120)


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str = Field(min_length=6)


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
    risk_level: RiskLevel = "medium"
    symbols: List[str] = ["XAUUSD", "BTCUSD"]
    active: bool = False
    max_concurrent_trades: int = 3
    auto_execute: bool = True
    # Profit Protection Suite
    breakeven_enabled: bool = True            # move SL to entry after +1R
    breakeven_trigger_r: float = 1.0          # R-multiple at which SL flips to break-even
    partial_close_enabled: bool = True        # take 50% off at TP1
    partial_close_trigger_r: float = 1.0      # R-multiple at which 50% closes (defaults to 1R)
    partial_close_fraction: float = 0.5       # fraction of lot to close at TP1
    trailing_enabled: bool = True             # trail SL after partial close
    trailing_start_r: float = 1.5             # activate trailing after this R-multiple
    trailing_distance_r: float = 0.7          # distance SL trails behind price (in R)
    daily_drawdown_pct: float = 3.0           # auto-stop bot if today's P&L drops below -3%
    daily_drawdown_enabled: bool = True
    # Spread Filter — block auto-execution when current MT5 spread > threshold
    spread_filter_enabled: bool = False
    max_spread_pips: Dict[str, float] = Field(
        default_factory=lambda: {"XAUUSD": 50.0, "BTCUSD": 100.0}
    )
    # Auto-Tune — let the bot raise the min-confidence threshold based on
    # historical win-rates per (symbol, confidence-bucket).
    auto_tune_enabled: bool = True
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
class BridgeHeartbeat(BaseModel):
    bridge_token: str
    balance: float
    equity: float
    open_positions: int = 0
    spreads: Optional[Dict[str, float]] = None  # symbol -> spread in pips (EA v1.21+)


class BridgeTradeReport(BaseModel):
    bridge_token: str
    trade_id: str
    mt5_ticket: Optional[int] = None
    status: TradeStatus
    entry_price: Optional[float] = None
    exit_price: Optional[float] = None
    pnl: Optional[float] = None
    error: Optional[str] = None


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
