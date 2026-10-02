"""Protection worker — unprotected-position repair + portfolio stop."""
from workers.base import main

if __name__ == "__main__":
    from background_loops import _protection_guard_loop
    from crypto_lifecycle import crypto_lifecycle_loop
    from risk_layers import _portfolio_stop_loop
    main("protection", [_protection_guard_loop, _portfolio_stop_loop,
                        crypto_lifecycle_loop])
