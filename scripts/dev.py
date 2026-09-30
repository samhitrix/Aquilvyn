#!/usr/bin/env python3
"""Run every FolioSense service + worker locally WITHOUT Docker — works on Windows, macOS, Linux.

    python scripts/dev.py            # migrate, start 6 services + 3 workers, Ctrl+C stops all
    python scripts/dev.py --no-workers
    python scripts/dev.py --only advisor,market

Needs: PostgreSQL (15+) and Redis running locally, and `pip install -r requirements.txt`.
Reads .env from the repo root. The DB URL is built from POSTGRES_USER/PASSWORD/DB (+ LOCAL_DB_HOST/
LOCAL_DB_PORT, default localhost:5432); LOCAL_REDIS_URL overrides Redis (default localhost:6379).
Logs: .dev/logs/<service>.log (single-line JSON).
"""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

# Windows consoles may use a legacy code page: never crash on ✔/→ characters
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(errors="replace")

ROOT = Path(__file__).resolve().parents[1]
SERVICES = {"identity": 8001, "portfolio": 8002, "market": 8003, "analytics": 8004, "advisor": 8005, "dashboard": 8006, "readiness": 8007}
WORKERS = ("market", "portfolio", "advisor", "readiness")


def load_env() -> dict[str, str]:
    env = dict(os.environ)
    dotenv = ROOT / ".env"
    if dotenv.exists():
        for line in dotenv.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            v = v.split(" #", 1)[0].strip().strip('"').strip("'")
            env.setdefault(k.strip(), v)
    # Same POSTGRES_* values as Docker; only the host differs. No URL to keep in sync.
    for k in ("DATABASE_URL", "MIGRATIONS_DATABASE_URL", "MIGRATIONS_DB_HOST", "MIGRATIONS_DB_PORT"):
        env.pop(k, None)
    if env.get("LOCAL_DATABASE_URL"):  # advanced override
        env["DATABASE_URL"] = env["MIGRATIONS_DATABASE_URL"] = env["LOCAL_DATABASE_URL"]
    env["DB_HOST"] = env.get("LOCAL_DB_HOST", "localhost")
    env["DB_PORT"] = env.get("LOCAL_DB_PORT", "5432")
    env["REDIS_URL"] = env.get("LOCAL_REDIS_URL", "redis://localhost:6379/0")
    env["DB_BEHIND_PGBOUNCER"] = "false"
    env["OTEL_ENABLED"] = env.get("LOCAL_OTEL_ENABLED", "false")
    for name, port in SERVICES.items():
        env[f"{name.upper()}_URL"] = f"http://localhost:{port}"
    env["PYTHONUNBUFFERED"] = "1"
    return env


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-workers", action="store_true")
    ap.add_argument("--no-migrate", action="store_true")
    ap.add_argument("--only", help="comma-separated subset of services")
    args = ap.parse_args()
    env = load_env()
    logs = ROOT / ".dev" / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    py = sys.executable

    if not args.no_migrate:
        print("→ running migrations …")
        r = subprocess.run([py, "-m", "alembic", "upgrade", "head"], cwd=ROOT / "migrations", env=env)
        if r.returncode:
            print("✘ migrations failed — is PostgreSQL running? Run: python scripts/fm.py db-init  (then: python scripts/fm.py doctor)")
            return r.returncode

    only = set(args.only.split(",")) if args.only else set(SERVICES)
    procs: list[tuple[str, subprocess.Popen]] = []
    for name, port in SERVICES.items():
        if name not in only:
            continue
        cmd = [py, "-m", "uvicorn", f"{name}_svc.main:app", "--host", "127.0.0.1", "--port", str(port), "--loop", "auto", "--http", "auto"]
        procs.append((name, subprocess.Popen(cmd, cwd=ROOT / "services" / name, env=env, stdout=open(logs / f"{name}.log", "ab"), stderr=subprocess.STDOUT)))
    if not args.no_workers:
        for name in WORKERS:
            if name not in only:
                continue
            cmd = [py, "-m", "arq", f"{name}_svc.worker.WorkerSettings"]
            procs.append((f"{name}-worker", subprocess.Popen(cmd, cwd=ROOT / "services" / name, env=env, stdout=open(logs / f"{name}-worker.log", "ab"), stderr=subprocess.STDOUT)))

    print("✔ started:", ", ".join(f"{n}" + (f" :{SERVICES[n]}" if n in SERVICES else "") for n, _ in procs))
    print(f"  logs → {logs}\n  web  → cd web && npm run dev   (http://localhost:3000)\n  Ctrl+C to stop")

    def stop(*_: object) -> None:
        print("\n→ stopping …")
        for _, p in procs:
            p.terminate()
        for _, p in procs:
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()
        sys.exit(0)

    signal.signal(signal.SIGINT, stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, stop)
    while True:
        for name, p in procs:
            if p.poll() is not None:
                print(f"✘ {name} exited with code {p.returncode} — see {logs / (name + '.log')}")
                procs.remove((name, p))
                break
        time.sleep(2)


if __name__ == "__main__":
    raise SystemExit(main())
