"""Analytics worker — DB-only daily aggregation (decision/trade stats)."""
from workers.base import main

if __name__ == "__main__":
    from background_loops import _analytics_loop
    main("analytics", [_analytics_loop])
