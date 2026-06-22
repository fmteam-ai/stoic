from datetime import datetime, timezone
from typing import Optional, List, Literal
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


class UserOut(BaseModel):
    id: str
    email: str
    name: Optional[str] = None
    role: str = "user"
    created_at: Optional[datetime] = None


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


# ---------- Bot config ----------
class BotConfigUpdate(BaseModel):
    risk_level: RiskLevel = "medium"
    symbols: List[str] = ["XAUUSD", "BTCUSD"]
    active: bool = False
    max_concurrent_trades: int = 3
    auto_execute: bool = True


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
