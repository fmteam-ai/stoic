"""Common MT5 broker presets — populated from public broker registration pages.

NOT exhaustive — users can always free-type any broker/server. This list
just makes the most common ones one-click in the UI.
"""

BROKER_PRESETS = [
    {
        "broker": "RoboForex",
        "servers": ["RoboForex-ECN", "RoboForex-Pro", "RoboForex-Demo"],
        "account_types": ["microcent", "cent", "standard", "demo"],
    },
    {
        "broker": "IC Markets",
        "servers": ["ICMarkets-Live01", "ICMarkets-Live02", "ICMarkets-Demo"],
        "account_types": ["standard", "demo"],
    },
    {
        "broker": "Exness",
        "servers": ["Exness-MT5Real", "Exness-MT5Trial", "Exness-MT5Real4"],
        "account_types": ["microcent", "cent", "standard", "demo"],
    },
    {
        "broker": "FXTM",
        "servers": ["ForexTimeFXTM-Live", "ForexTimeFXTM-Demo"],
        "account_types": ["microcent", "cent", "standard", "demo"],
    },
    {
        "broker": "Pepperstone",
        "servers": ["Pepperstone-Live01", "Pepperstone-Live02", "Pepperstone-Demo"],
        "account_types": ["standard", "demo"],
    },
    {
        "broker": "OctaFX",
        "servers": ["OctaFX-Real", "OctaFX-Demo"],
        "account_types": ["microcent", "cent", "standard", "demo"],
    },
    {
        "broker": "XM",
        "servers": ["XMGlobal-MT5", "XMGlobal-MT5 2", "XMGlobal-Demo MT5"],
        "account_types": ["microcent", "cent", "standard", "demo"],
    },
    {
        "broker": "FBS",
        "servers": ["FBS-Real-1", "FBS-Real-2", "FBS-Demo"],
        "account_types": ["microcent", "cent", "standard", "demo"],
    },
    {
        "broker": "HotForex (HF Markets)",
        "servers": ["HFMarketsSV-Live Server", "HFMarketsSV-Demo Server"],
        "account_types": ["microcent", "cent", "standard", "demo"],
    },
    {
        "broker": "Tickmill",
        "servers": ["Tickmill-Live01", "Tickmill-Live02", "Tickmill-Demo"],
        "account_types": ["standard", "demo"],
    },
]
