"""iter-173 · OOM guard — the GBM zoo must never load/train inside a
memory-constrained container (prod 1Gi pod was OOM-crash-looping → 520s)."""
import os
import sys

_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BACKEND_DIR)
from dotenv import load_dotenv

load_dotenv(os.path.join(_BACKEND_DIR, ".env"))


def _run(coro):
    from conftest import run_async
    return run_async(coro)


def _db():
    from database import get_db
    return get_db()


def test_explicit_env_wins(monkeypatch):
    import ml_ensemble as m
    monkeypatch.setenv("ML_ENSEMBLE_ENABLED", "false")
    assert m.ml_runtime_enabled() is False
    monkeypatch.setenv("ML_ENSEMBLE_ENABLED", "true")
    assert m.ml_runtime_enabled() is True


def test_auto_disable_on_small_container(monkeypatch):
    import ml_runtime as mr
    monkeypatch.delenv("ML_ENSEMBLE_ENABLED", raising=False)
    monkeypatch.setattr(mr, "_memory_budget_gb", lambda: 1.0)
    assert mr.ml_runtime_enabled() is False   # 1Gi prod pod
    monkeypatch.setattr(mr, "_memory_budget_gb", lambda: 8.0)
    assert mr.ml_runtime_enabled() is True    # preview pod


def test_train_ensemble_skips_when_disabled(monkeypatch):
    import ml_ensemble as m
    monkeypatch.setenv("ML_ENSEMBLE_ENABLED", "false")
    db = _db()
    uid = f"iter173ml-{os.urandom(4).hex()}"
    try:
        meta = _run(m.train_ensemble(db, uid))
        assert meta["status"] == "disabled_low_memory"
        doc = _run(db.ml_ensembles.find_one({"user_id": uid}))
        assert doc["status"] == "disabled_low_memory"
    finally:
        _run(db.ml_ensembles.delete_many({"user_id": uid}))


def test_get_meta_never_trains_when_disabled(monkeypatch):
    import ml_ensemble as m

    async def _boom(*a, **k):
        raise AssertionError("train_ensemble must NOT run when disabled")
    monkeypatch.setenv("ML_ENSEMBLE_ENABLED", "false")
    monkeypatch.setattr(m, "train_ensemble", _boom)
    meta = _run(m.get_meta(_db(), f"iter173ml-{os.urandom(4).hex()}"))
    assert meta["status"] == "disabled_low_memory"


def test_ml_predict_keeps_light_members_when_disabled(monkeypatch):
    import ml_ensemble as m
    monkeypatch.setenv("ML_ENSEMBLE_ENABLED", "false")
    signal = {"rl_policy": {"n": 20, "mean": 30.0},
              "bayes": {"n": 20, "p_success": 0.62}}
    pred = _run(m.ml_predict(_db(), f"iter173ml-{os.urandom(4).hex()}",
                             signal, "EURUSD"))
    names = {mm["name"] for mm in pred["members"]}
    assert {"rl_agent", "bayesian"} <= names   # light members still vote
    assert pred["gbm_status"] == "disabled_low_memory"
    assert pred["p_win"] is not None


def test_staged_retrain_gated(monkeypatch):
    from learning_pipeline import staged_ml_retrain
    monkeypatch.setenv("ML_ENSEMBLE_ENABLED", "false")
    out = _run(staged_ml_retrain(_db(), f"iter173ml-{os.urandom(4).hex()}"))
    assert out["status"] == "disabled_low_memory"


def test_learned_meta_xgb_lazy_and_gated(monkeypatch):
    import learned_meta
    monkeypatch.setenv("ML_ENSEMBLE_ENABLED", "false")
    assert learned_meta._xgb() is None
    monkeypatch.setenv("ML_ENSEMBLE_ENABLED", "true")
    assert learned_meta._xgb() is not None


def test_forecast_model_gated(monkeypatch):
    import forecast_agent
    monkeypatch.setenv("ML_ENSEMBLE_ENABLED", "false")
    monkeypatch.setattr(forecast_agent, "_model", None)
    monkeypatch.setattr(forecast_agent, "_model_failed", False)
    assert forecast_agent._load_model() is None
    assert forecast_agent._model_failed is True  # cached skip, no torch load


def test_no_heavy_ml_imported_at_server_boot():
    """The API must boot WITHOUT torch/sklearn/xgboost/lightgbm/catboost —
    module-level imports of these OOM-kill small production containers."""
    import subprocess
    import sys
    code = (
        "import sys; import server; "
        "bad = [m for m in ('torch','sklearn','xgboost','lightgbm',"
        "'catboost','chronos') if m in sys.modules]; "
        "print('HEAVY:' + ','.join(bad))")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True,
                       text=True, cwd=_BACKEND_DIR, timeout=180)
    assert "HEAVY:" in r.stdout, r.stderr[-500:]
    heavy = r.stdout.split("HEAVY:")[-1].strip()
    assert heavy == "", f"heavy ML imported at boot: {heavy}"
