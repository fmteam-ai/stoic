"""Trading worker — signal generation, trade management, market warming."""
from workers.base import main

if __name__ == "__main__":
    import bot_runner
    import trade_manager
    import warmer
    main("trading", [bot_runner.loop, warmer.loop, trade_manager.run_loop])
