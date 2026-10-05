#!/usr/bin/env python3
"""N-R2 / A9e — every `secrets.<name>.file: ./secrets/<file>` in docker-compose*.yml must be
created by deploy/lib.sh::ensure_release_secrets (the update.sh path) or deploy/install.sh,
otherwise an EXISTING server cannot start the new containers and update.sh rolls back.
Also validates `docker compose config` when the docker CLI is available.
Exit 1 on the first missing secret."""
import glob
import os
import re
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# opt-in profiles whose secrets are minted by their own command (docs/HOST_MIGRATION.md: `make migrator-on`)
OPT_IN_PROFILES = ("docker-compose.migrator.yml",)


def compose_file_secrets() -> dict[str, str]:
    out = {}
    for path in sorted(glob.glob(os.path.join(ROOT, "docker-compose*.yml"))):
        if os.path.basename(path) in OPT_IN_PROFILES:
            continue
        text = open(path, encoding="utf-8").read()
        m = re.search(r"^secrets:\s*\n((?:[ \t]+.*\n?)+)", text, re.M)
        if not m:
            continue
        for name, f in re.findall(r"^\s{2}([A-Za-z0-9_]+):\s*\n\s+file:\s*\./secrets/([A-Za-z0-9_.-]+)", m.group(1), re.M):
            out[name] = f
    return out


def created_by_deploy_scripts() -> set[str]:
    files = set()
    for p in ("deploy/lib.sh", "deploy/install.sh"):
        text = open(os.path.join(ROOT, p), encoding="utf-8").read()
        files.update(re.findall(r"secrets/([A-Za-z0-9_.-]+)", text))
    return files


def _stub_from_example(example: str) -> str:
    lines = []
    for ln in open(example, encoding="utf-8").read().splitlines():
        if re.match(r"^[A-Z0-9_]+=\s*$", ln):
            ln = ln.rstrip() + "ci-stub"
        lines.append(ln)
    return "\n".join(lines) + "\n"


def _compose_config() -> "subprocess.CompletedProcess":
    """`docker compose config` against stubs for every env file a CI checkout lacks:
    ./.env (--env-file, `${VAR:?}` contract) and backend/.env (compose `env_file:` — the
    path must exist). Stubs are built from the .env.example files only and removed after."""
    import tempfile
    env = {**os.environ, "COMPOSE_FILE": "docker-compose.yml"}
    cmd = ["docker", "compose"]
    cleanup = []
    try:
        if not os.path.exists(os.path.join(ROOT, ".env")) and os.path.exists(os.path.join(ROOT, ".env.example")):
            stub = tempfile.NamedTemporaryFile("w", suffix=".env", delete=False)
            stub.write(_stub_from_example(os.path.join(ROOT, ".env.example")))
            stub.close()
            cleanup.append(stub.name)
            cmd += ["--env-file", stub.name]
        backend_env = os.path.join(ROOT, "backend", ".env")
        if not os.path.exists(backend_env):
            example = os.path.join(ROOT, "backend", ".env.example")
            with open(backend_env, "w", encoding="utf-8") as fh:
                fh.write(_stub_from_example(example) if os.path.exists(example) else "APP_ENV=ci-stub\n")
            cleanup.append(backend_env)
        return subprocess.run(cmd + ["config", "--quiet"], cwd=ROOT, env=env, capture_output=True, text=True)
    finally:
        for p in cleanup:
            try:
                os.unlink(p)
            except OSError:
                pass


def main() -> int:
    want = compose_file_secrets()
    have = created_by_deploy_scripts()
    lib = open(os.path.join(ROOT, "deploy/lib.sh"), encoding="utf-8").read()
    ensure = lib[lib.index("ensure_release_secrets()"):]
    ensure = ensure[:ensure.index("\n}\n")]
    missing_update = [f for f in want.values() if f not in have]
    # the update.sh path (ensure_release_secrets) must cover every secret install.sh does NOT
    # treat as operator-provided (certs / keys that install.sh generates are fine on old hosts
    # because they existed before; NEW empty-placeholder secrets must be created here).
    placeholders = [f for f in want.values() if f"secrets/{f}" in lib or f"secrets/{f}" in ensure]
    print(f"compose secrets: {len(want)} · created by deploy scripts: {len(have & set(want.values()))} · ensure_release_secrets covers: {len(placeholders)}")
    rc = 0
    if missing_update:
        print("FAIL: compose mounts secrets no deploy script creates:", ", ".join(sorted(missing_update)))
        rc = 1
    if "security_telegram_token" not in ensure:
        print("FAIL: deploy/lib.sh::ensure_release_secrets does not create secrets/security_telegram_token (N-R2)")
        rc = 1
    if shutil.which("docker"):
        r = _compose_config()
        if r.returncode != 0:
            print("FAIL: docker compose config:", (r.stderr or r.stdout).strip()[:800])
            rc = 1
        else:
            print("OK: docker compose config")
    print("OK: every compose secret is provisioned by the update path" if rc == 0 else "see failures above")
    return rc


if __name__ == "__main__":
    sys.exit(main())
