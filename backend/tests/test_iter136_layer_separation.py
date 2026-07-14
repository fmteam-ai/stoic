"""iter-136 · Research / simulation / production layer boundary (roadmap #1).

Fails if experimental or simulation code can reach a live account, or if
production imports simulation modules. Policy: /app/docs/ARCHITECTURE_LAYERS.md
"""
import ast
import os

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PRODUCTION = ["bot_runner.py", "execution.py",
              os.path.join("agents", "orchestrator.py"),
              os.path.join("routes", "trade_routes.py"),
              os.path.join("routes", "bot_routes.py")]
SIMULATION_PKGS = ("backtester", "research", "strategy_backtest")
LIVE_EXECUTION = ("execution", "bot_runner", "mt5_bridge")


def imports_of(path: str) -> set:
    with open(path) as f:
        tree = ast.parse(f.read())
    mods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module.split(".")[0])
    return mods


class TestLayerSeparation:
    def test_production_never_imports_simulation(self):
        for rel in PRODUCTION:
            p = os.path.join(BACKEND, rel)
            if not os.path.exists(p):
                continue
            bad = imports_of(p) & set(SIMULATION_PKGS)
            assert not bad, f"{rel} imports simulation layer: {bad}"

    def test_simulation_never_imports_live_execution(self):
        for pkg in ("backtester",):
            pkg_dir = os.path.join(BACKEND, pkg)
            if not os.path.isdir(pkg_dir):
                continue
            for fn in os.listdir(pkg_dir):
                if not fn.endswith(".py"):
                    continue
                bad = imports_of(os.path.join(pkg_dir, fn)) & set(LIVE_EXECUTION)
                assert not bad, f"{pkg}/{fn} imports live execution: {bad}"
        # ablation is simulation-layer too
        bad = imports_of(os.path.join(BACKEND, "ablation.py")) & set(LIVE_EXECUTION)
        assert not bad, f"ablation.py imports live execution: {bad}"

    def test_nothing_imports_research(self):
        for root, dirs, files in os.walk(BACKEND):
            dirs[:] = [d for d in dirs if d not in
                       ("__pycache__", "research", "tests", ".git", "node_modules")]
            for fn in files:
                if not fn.endswith(".py"):
                    continue
                p = os.path.join(root, fn)
                try:
                    mods = imports_of(p)
                except SyntaxError:
                    continue
                assert "research" not in mods, f"{p} imports research layer"

    def test_docs_exist(self):
        app_root = os.path.dirname(BACKEND)
        assert os.path.exists(os.path.join(app_root, "docs", "ARCHITECTURE_LAYERS.md"))
        assert os.path.exists(os.path.join(app_root, "docs", "PROMOTION_CRITERIA.md"))
