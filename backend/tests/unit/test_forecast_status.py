"""Unit — Forecast Health telemetry (iter-197)."""
import forecast_agent as fa


def test_runtime_status_shape_and_defaults(monkeypatch):
    monkeypatch.delenv("STOIC_PROCESS_ROLE", raising=False)
    monkeypatch.delenv("FORECAST_AGENT_ENABLED", raising=False)
    st = fa.runtime_status()
    for k in ("model", "role", "holder", "model_loaded", "model_failed",
              "load_ms", "last_forecast_at", "last_latency_ms",
              "forecasts_total", "cache_entries", "cache_ttl_s",
              "agent_enabled", "ml_runtime_enabled", "torch_available",
              "updated_at"):
        assert k in st, k
    assert st["model"] == fa.MODEL_NAME
    assert st["role"] == "api"
    assert st["agent_enabled"] is True
    assert st["cache_ttl_s"] == fa.CACHE_TTL


def test_runtime_status_reads_process_role_and_disable_flag(monkeypatch):
    monkeypatch.setenv("STOIC_PROCESS_ROLE", "worker-trading")
    monkeypatch.setenv("FORECAST_AGENT_ENABLED", "false")
    st = fa.runtime_status()
    assert st["role"] == "worker-trading"
    assert st["agent_enabled"] is False


def test_forecast_sync_updates_load_stats_when_model_unavailable(monkeypatch):
    # force the gate off → model skipped → stats record the reason
    monkeypatch.setattr(fa, "_model", None)
    monkeypatch.setattr(fa, "_model_failed", False)
    import ml_runtime
    monkeypatch.setattr(ml_runtime, "ml_runtime_enabled", lambda: False)
    assert fa._load_model() is None
    assert fa._stats["last_error"] == "heavy ML disabled (memory gate)"
