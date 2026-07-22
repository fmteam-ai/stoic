"""Protection worker — dedicated unprotected-position repair cadence."""
from workers.base import main

if __name__ == "__main__":
    from background_loops import _protection_guard_loop
    main("protection", [_protection_guard_loop])
