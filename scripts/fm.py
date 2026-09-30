#!/usr/bin/env python3
"""FolioSense task runner — works the same on Windows (PowerShell/CMD), macOS and Linux.
Standard library only, so it runs before any `pip install`.

    python scripts/fm.py help
    python scripts/fm.py setup        # create .env with generated secrets
    python scripts/fm.py db-init      # no Docker: create the DB user + database from .env
    python scripts/fm.py doctor       # pre-flight checks
    python scripts/fm.py up-lite      # Docker: core services (less RAM); picks free host ports automatically
    python scripts/fm.py up           # Docker: everything incl. Grafana/Loki/Tempo
    python scripts/fm.py logs advisor # follow one service's logs
    python scripts/fm.py dev          # no Docker: run services locally (see docs/RUNNING.md, Option B)
"""
from __future__ import annotations

import base64
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
from pathlib import Path

# Windows consoles may use a legacy code page: never crash on ✔/→ characters
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(errors="replace")

ROOT = Path(__file__).resolve().parents[1]
ENV = ROOT / ".env"
EXAMPLE = ROOT / ".env.example"
PY = sys.executable

PLACEHOLDERS = {
    "JWT_SECRET": ("change-me", lambda: secrets.token_urlsafe(48)),
    "FM_ENCRYPTION_KEY": ("", lambda: base64.urlsafe_b64encode(os.urandom(32)).decode()),
    "POSTGRES_PASSWORD": ("change-me", lambda: secrets.token_urlsafe(18).replace("-", "x").replace("_", "y")),
    "GRAFANA_ADMIN_PASSWORD": ("change-me", lambda: secrets.token_urlsafe(12)),
}


def say(msg: str, ok: bool | None = None) -> None:
    mark = "" if ok is None else ("[ok]  " if ok else "[!!]  ")
    print(f"{mark}{msg}", flush=True)


def run(cmd: list[str], env_extra: dict[str, str] | None = None, check: bool = True, secret: str | None = None) -> int:
    env = {**os.environ, **(env_extra or {})}
    shown = " ".join(cmd)
    if secret:
        shown = shown.replace(secret, "****")
    if env_extra:
        shown = " ".join(f"{k}={v}" for k, v in env_extra.items()) + " " + shown
    say(f"> {shown}")
    rc = subprocess.call(cmd, cwd=ROOT, env=env)
    if check and rc != 0:
        sys.exit(rc)
    return rc


def _engine_up() -> bool:
    return subprocess.call(["docker", "info"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) == 0


def ensure_docker_engine(timeout: int = 180) -> None:
    """Docker CLI present but engine stopped (e.g. 'npipe:////./pipe/docker_engine' error on Windows):
    start Docker Desktop ourselves and wait until the engine answers."""
    if not shutil.which("docker"):
        return  # compose() prints install help
    if _engine_up():
        return
    launched = False
    if os.name == "nt":
        for exe in (r"C:\Program Files\Docker\Docker\Docker Desktop.exe", os.path.expandvars(r"%LOCALAPPDATA%\Docker\Docker Desktop.exe")):
            if Path(exe).exists():
                say("Docker engine not running — starting Docker Desktop …")
                subprocess.Popen([exe], creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
                launched = True
                break
    elif sys.platform == "darwin":
        launched = subprocess.call(["open", "-a", "Docker"]) == 0
        if launched:
            say("Docker engine not running — starting Docker Desktop …")
    if not launched:
        say("Docker engine is not running. Start Docker Desktop (Windows/macOS) or `sudo systemctl start docker` (Linux), then retry.", False)
        sys.exit(1)
    import time

    start = time.time()
    while time.time() - start < timeout:
        if _engine_up():
            say(f"Docker engine is up ({int(time.time() - start)} s)", True)
            return
        print(".", end="", flush=True)
        time.sleep(3)
    print()
    say("Docker Desktop did not become ready in time. Open it, wait for 'Engine running', then retry.", False)
    sys.exit(1)


OLD_PROJECT = "foliomatrix"  # the app's earlier name — its Docker volumes hold existing installs' data


def keep_existing_data() -> None:
    """The Docker project was renamed foliomatrix → foliosense. Volume names follow the project name, so an
    existing install would suddenly see an empty database. If the old volumes exist and .env doesn't pin a
    project yet, pin the old name so the same data keeps being used."""
    if os.environ.get("COMPOSE_PROJECT_NAME") or _env_value("COMPOSE_PROJECT_NAME"):
        return
    try:
        vols = subprocess.run(["docker", "volume", "ls", "-q"], capture_output=True, text=True, timeout=20).stdout.split()
    except Exception:  # noqa: BLE001 — no Docker, nothing to protect
        return
    if f"{OLD_PROJECT}_pgdata" in vols:
        with ENV.open("a", encoding="utf-8") as fh:
            fh.write(f"\n# Keeps using the data created before the rename to FolioSense (Docker volumes {OLD_PROJECT}_*)\n"
                     f"COMPOSE_PROJECT_NAME={OLD_PROJECT}\n")
        say(f"found your existing data (Docker volumes {OLD_PROJECT}_*) — kept using it: COMPOSE_PROJECT_NAME={OLD_PROJECT} added to .env", True)


def compose() -> list[str]:
    """`docker compose` (v2 plugin) or the legacy `docker-compose` binary (engine must be running)."""
    ensure_docker_engine()
    if ENV.exists():
        keep_existing_data()
    if shutil.which("docker"):
        if subprocess.call(["docker", "compose", "version"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) == 0:
            return ["docker", "compose"]
    if shutil.which("docker-compose"):
        return ["docker-compose"]
    say("Docker Compose not found. Install Docker Desktop (Windows/macOS) or Docker Engine + compose plugin (Linux).", False)
    say("Or run without Docker: see docs/RUNNING.md → Option B (python scripts/fm.py dev).")
    sys.exit(1)


# --------------------------------------------------------------------------- setup
def _get(text: str, key: str) -> str | None:
    m = re.search(rf"^{key}=([^\r\n]*)", text, flags=re.M)
    return m.group(1).split(" #", 1)[0].strip().strip('"').strip("'") if m else None


def _set(text: str, key: str, value: str) -> str:
    return re.sub(rf"^{key}=[^\r\n]*", f"{key}={value}", text, count=1, flags=re.M)


LEGACY_KEYS = ("DATABASE_URL", "MIGRATIONS_DATABASE_URL", "DB_BEHIND_PGBOUNCER")


def _add_new_settings(text: str) -> str:
    """Settings added to .env.example after your .env was created are appended (with their comments and the
    example's default, usually blank). Nothing you already set is changed."""
    have = set(re.findall(r"^([A-Z][A-Z0-9_]*)=", text, flags=re.M))
    block: list[str] = []
    comments: list[str] = []
    for line in EXAMPLE.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^([A-Z][A-Z0-9_]*)=", line)
        if m:
            if m.group(1) not in have:
                block += [*comments, line]
            comments = []
        elif line.startswith("#"):
            comments.append(line)
        else:
            comments = []
    if not block:
        return text
    added = [re.match(r"^([A-Z][A-Z0-9_]*)=", x).group(1) for x in block if re.match(r"^[A-Z]", x)]  # type: ignore[union-attr]
    shutil.copyfile(ENV, ENV.with_name(".env.bak"))
    text = text.rstrip("\n") + "\n\n# ---------- added by `fm.py setup` (new settings — fill in the ones you use) ----------\n" + "\n".join(block) + "\n"
    ENV.write_text(text, encoding="utf-8")
    say(f"added new settings to .env: {', '.join(added)} (backup: .env.bak)", True)
    return text


def setup() -> None:
    created = False
    if not ENV.exists():
        shutil.copyfile(EXAMPLE, ENV)
        created = True
        say(".env created from .env.example", True)
    text = ENV.read_text(encoding="utf-8")
    # Upgrade older .env files: URLs are now built from POSTGRES_* — stale URL lines would override them.
    legacy = [k for k in LEGACY_KEYS if re.search(rf"^{k}=", text, flags=re.M)]
    if legacy:
        shutil.copyfile(ENV, ENV.with_name(".env.bak"))
        for k in legacy:
            text = re.sub(rf"^{k}=.*(\r?\n)?", "", text, flags=re.M)
        ENV.write_text(text, encoding="utf-8")
        say(f"removed old {', '.join(legacy)} from .env (now built from POSTGRES_*); backup: .env.bak", True)
    text = _add_new_settings(text)
    changed = []
    for key, (marker, gen) in PLACEHOLDERS.items():
        cur = _get(text, key)
        if cur is None:
            continue
        if cur == "" or (marker and marker in cur):
            new = gen()
            text = _set(text, key, new)
            changed.append(key)
    if changed:
        ENV.write_text(text, encoding="utf-8")
        say(f"generated secure values for: {', '.join(changed)}", True)
    elif not created:
        say(".env already configured — nothing changed", True)
    say("Optional: edit .env to set MARKET_DATA_PROVIDER=yahoo (live prices) and AI keys (ANTHROPIC_API_KEY …).")



# --------------------------------------------------------------------------- host ports
# (env key, default, compose service, container port, label, needed by lite mode)
HOST_PORTS = (
    ("FM_HTTP_PORT", 8080, "gateway", 8080, "App", True),
    ("FM_DB_PORT", 55432, "postgres", 5432, "Docker PostgreSQL (pgAdmin/psql)", True),
    ("FM_GRAFANA_PORT", 3001, "grafana", 3000, "Grafana", False),
)


def can_bind(port: int) -> bool:
    """True if we can actually *bind* the port — unlike a connect test this also catches ports
    Windows/Hyper-V/WSL has reserved (WinError 10013 'forbidden by its access permissions')."""
    for host in ("127.0.0.1", "0.0.0.0"):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind((host, port))
        except OSError:
            return False
    return True


def pick_port(preferred: int, owned: set[int] | frozenset[int] = frozenset(), span: int = 100) -> int | None:
    """First usable port from `preferred` upward. Ports already published by our own running
    containers (`owned`) count as usable, so re-running `up` never jumps to a new port."""
    for port in range(preferred, preferred + span + 1):
        if port in owned or can_bind(port):
            return port
    return None


def save_env_value(key: str, value: str, path: Path | None = None) -> None:
    path = path or ENV
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    if re.search(rf"^{key}=", text, flags=re.M):
        text = re.sub(rf"^{key}=[^\r\n]*", f"{key}={value}", text, count=1, flags=re.M)
    else:
        text = text.rstrip("\n") + f"\n{key}={value}\n"
    path.write_text(text, encoding="utf-8")


def _owned_port(base: list[str], service: str, container_port: int) -> int | None:
    """Host port our running container already publishes (None if not running)."""
    try:
        out = subprocess.run([*base, "port", service, str(container_port)], cwd=ROOT, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = re.search(r":(\d+)\s*$", out.stdout.strip().splitlines()[-1]) if out.returncode == 0 and out.stdout.strip() else None
    return int(m.group(1)) if m else None


def resolve_ports(base: list[str], full: bool) -> dict[str, str]:
    chosen: dict[str, str] = {}
    for key, default, service, cport, label, in_lite in HOST_PORTS:
        if not (full or in_lite):
            continue
        preferred = int(_env_value(key) or default)
        owned = _owned_port(base, service, cport)
        port = pick_port(preferred, {owned} if owned else set())
        if port is None:
            say(f"no free port found for {label} in {preferred}-{preferred + 100}; set {key} in .env", False)
            sys.exit(1)
        if port != preferred:
            say(f"port {preferred} ({label}) is busy or reserved → using {port} (saved as {key} in .env)")
        elif owned == port:
            say(f"port {port} ({label}) already used by FolioSense — keeping it", True)
        else:
            say(f"port {port} ({label}) is free", True)
        if _env_value(key) != str(port):
            save_env_value(key, str(port))
        chosen[key] = str(port)
    return chosen


# --------------------------------------------------------------------------- doctor
def _port_free(port: int) -> bool:
    """Free only if nothing answers on IPv4 *or* IPv6 localhost (Postgres/Redis may bind either)."""
    for family, host in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")):
        try:
            with socket.socket(family, socket.SOCK_STREAM) as s:
                s.settimeout(0.3)
                if s.connect_ex((host, port)) == 0:
                    return False
        except OSError:
            continue
    return True


EOL_FILES = ("infra/healthping/ping.sh", "infra/nginx/nginx.conf")


def _db_parts() -> dict[str, str]:
    return {
        "user": _env_value("POSTGRES_USER") or "foliosense",
        "password": _env_value("POSTGRES_PASSWORD") or "foliosense",
        "db": _env_value("POSTGRES_DB") or "foliosense",
        "host": _env_value("LOCAL_DB_HOST") or "localhost",
        "port": _env_value("LOCAL_DB_PORT") or "5432",
    }


def _local_db_url() -> str:
    from urllib.parse import quote

    if url := _env_value("LOCAL_DATABASE_URL"):
        return url
    p = _db_parts()
    return f"postgresql://{quote(p['user'], safe='')}:{quote(p['password'], safe='')}@{p['host']}:{p['port']}/{quote(p['db'], safe='')}"


def _redis_ping(url: str) -> tuple[bool, str]:
    """Stdlib-only PING (works for Redis, Memurai, Valkey)."""
    m = re.match(r"redis://(?::(?P<pw>[^@]*)@)?(?P<host>[^:/]+)(?::(?P<port>\d+))?", url)
    if not m:
        return False, f"cannot parse {url}"
    try:
        with socket.create_connection((m["host"], int(m["port"] or 6379)), timeout=1) as s:
            if m["pw"]:
                s.sendall(f"AUTH {m['pw']}\r\n".encode())
                s.recv(64)
            s.sendall(b"PING\r\n")
            reply = s.recv(64)
            return reply.startswith(b"+PONG"), reply.decode(errors="replace").strip()
    except OSError as exc:
        return False, str(exc)


def _pg_check(url: str) -> tuple[bool | None, str]:
    """Connect with asyncpg if installed (after `pip install -r requirements.txt`)."""
    try:
        import asyncio

        import asyncpg  # type: ignore[import-not-found]
    except ImportError:
        return None, "asyncpg not installed yet — run: pip install -r requirements.txt"

    async def go() -> str:
        conn = await asyncpg.connect(url.replace("postgresql+asyncpg://", "postgresql://"), timeout=3)
        try:
            return await conn.fetchval("SHOW server_version")
        finally:
            await conn.close()

    try:
        return True, f"PostgreSQL {asyncio.run(go())}"
    except Exception as exc:  # noqa: BLE001 — report any connection problem verbatim
        return False, f"{type(exc).__name__}: {exc}"


def _env_value(key: str) -> str | None:
    if os.environ.get(key):
        return os.environ[key]
    return _get(ENV.read_text(encoding="utf-8"), key) if ENV.exists() else None


def doctor() -> None:
    problems = 0
    say(f"Python {sys.version.split()[0]} at {PY}", sys.version_info >= (3, 11))
    say(".env present" if ENV.exists() else ".env missing — run: python scripts/fm.py setup", ENV.exists())
    problems += not ENV.exists()
    if ENV.exists():
        text = ENV.read_text(encoding="utf-8")
        weak = [k for k, (m, _) in PLACEHOLDERS.items() if (v := _get(text, k)) is not None and (v == "" or (m and m in v))]
        say("secrets set" if not weak else f"placeholder secrets: {', '.join(weak)} — run: python scripts/fm.py setup", not weak)
        problems += bool(weak)

    has_docker = shutil.which("docker") is not None
    docker_ok = False
    if has_docker:
        docker_ok = subprocess.call(["docker", "info"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) == 0
        say("Docker engine is running" if docker_ok else "Docker is installed but NOT running — start Docker Desktop", docker_ok)

    if docker_ok:
        print("\n-- Option A (Docker) --")
        say("a local PostgreSQL on 5432 / Redis on 6379 is fine — Docker's own DB/cache stay inside Docker")
        base = compose()
        for key, default, service, cport, label, _ in HOST_PORTS:
            preferred = int(_env_value(key) or default)
            owned = _owned_port(base, service, cport)
            if owned == preferred:
                say(f"{key}={preferred} ({label}) — in use by FolioSense", True)
            elif can_bind(preferred):
                say(f"{key}={preferred} ({label}) — free", True)
            else:
                alt = pick_port(preferred)
                say(f"{key}={preferred} ({label}) — busy/reserved; `up` will switch to {alt}", True)
        for rel in EOL_FILES:
            crlf = b"\r\n" in (ROOT / rel).read_bytes()
            say(f"{rel} line endings {'CRLF — run: python scripts/fm.py fixeol' if crlf else 'LF'}", not crlf)
            problems += crlf
    else:
        print("\n-- Option B (no Docker): using your local PostgreSQL + Redis --")
        if not has_docker:
            say("Docker not installed — that's fine, use: python scripts/fm.py dev")
            if os.name == "nt":
                say("   (to install Docker on Windows, in an *Administrator* PowerShell: "
                    "wsl --install  → restart →  winget install -e --id Docker.DockerDesktop)")
        db = _local_db_url()
        say(f"database (from POSTGRES_* in .env): {re.sub(r':[^:@/]+@', ':****@', db)}")
        ok, info = _pg_check(db)
        if ok is None:
            say(info, False)
        else:
            say(f"PostgreSQL reachable: {info}" if ok else f"PostgreSQL NOT reachable: {info}", ok)
            if not ok:
                say("   → run: python scripts/fm.py db-init   (creates/updates the user + database from .env)")
        problems += not ok
        rurl = _env_value("LOCAL_REDIS_URL") or "redis://localhost:6379/0"
        rok, rinfo = _redis_ping(rurl)
        say(f"Redis reachable at {rurl} ({rinfo})" if rok else f"Redis NOT reachable at {rurl}: {rinfo} — start Redis/Memurai", rok)
        problems += not rok
        for port, what in ((8001, "identity"), (8005, "advisor"), (3000, "web")):
            free = _port_free(port)
            say(f"port {port} ({what}) {'free' if free else 'IN USE (fine if FolioSense is already running)'}", free)
    print()
    say("all good" if not problems else f"{problems} issue(s) found", not problems)


def _find_psql() -> str | None:
    if found := shutil.which("psql"):
        return found
    if os.name == "nt":  # Windows installers don't add PostgreSQL to PATH
        import glob

        hits = sorted(glob.glob(r"C:\Program Files\PostgreSQL\*\bin\psql.exe"), key=lambda p: int((re.findall(r"\\(\d+)\\bin", p) or ["0"])[0]))
        return hits[-1] if hits else None
    return None


def db_init() -> None:
    """Create (or update the password of) the FolioSense role + database on your local
    PostgreSQL, using exactly the POSTGRES_* values from .env — nothing to type twice."""
    if not ENV.exists():
        setup()
    p = _db_parts()
    lit = lambda v: "'" + v.replace("'", "''") + "'"  # noqa: E731 — SQL string literal
    ident = lambda v: '"' + v.replace('"', '""') + '"'  # noqa: E731 — SQL identifier
    role_sql = (f"DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = {lit(p['user'])}) THEN "
                f"CREATE ROLE {ident(p['user'])} LOGIN PASSWORD {lit(p['password'])}; ELSE "
                f"ALTER ROLE {ident(p['user'])} WITH LOGIN PASSWORD {lit(p['password'])}; END IF; END $$;")
    db_sql = f"CREATE DATABASE {ident(p['db'])} OWNER {ident(p['user'])}"
    psql = _find_psql()
    admin = os.environ.get("PGADMIN_USER", "postgres")
    if not psql:
        say("psql not found. Run these as the postgres superuser (e.g. in pgAdmin → Query Tool):", False)
        print(f"   {role_sql}\n   {db_sql};")
        return
    base = [psql, "-h", p["host"], "-p", p["port"], "-U", admin, "-d", "postgres", "-v", "ON_ERROR_STOP=1"]
    say(f"Using {psql} as superuser '{admin}' on {p['host']}:{p['port']} (you may be asked for the postgres password)")
    run([*base, "-c", role_sql], secret=p["password"])
    exists = subprocess.run([*base, "-tAc", f"SELECT 1 FROM pg_database WHERE datname = {lit(p['db'])}"], capture_output=True, text=True)
    if exists.stdout.strip() != "1":
        run([*base, "-c", db_sql])
    else:
        say(f"database '{p['db']}' already exists", True)
    say(f"role '{p['user']}' and database '{p['db']}' ready — next: python scripts/fm.py doctor", True)


def fixeol() -> None:
    """Re-checkout container-mounted files with LF endings (as .gitattributes demands). Only these
    files are touched; nothing else in your working tree changes."""
    for rel in EOL_FILES:
        path = ROOT / rel
        if b"\r\n" in path.read_bytes():
            path.unlink()
            run(["git", "checkout", "--", rel])
    for rel in EOL_FILES:
        lf = b"\r\n" not in (ROOT / rel).read_bytes()
        say(f"{rel}: {'LF' if lf else 'still CRLF — check core.autocrlf'}", lf)


# --------------------------------------------------------------------------- diagnostics
DIAG_SQL = r"""
\echo '--- schema version (expect 0004)'
SELECT version_num FROM audit.alembic_version;
\echo '--- per household: members, profiles (live/deleted), transactions (live/deleted), imports'
SELECT left(h.id::text, 8) AS household,
  (SELECT count(*) FROM auth.users u WHERE u.household_id = h.id) AS members,
  (SELECT count(*) FROM core.profiles p WHERE p.household_id = h.id AND p.deleted_at IS NULL) AS profiles,
  (SELECT count(*) FROM core.profiles p WHERE p.household_id = h.id AND p.deleted_at IS NOT NULL) AS profiles_deleted,
  (SELECT count(*) FROM core.transactions t WHERE t.household_id = h.id AND t.deleted_at IS NULL) AS txns,
  (SELECT count(*) FROM core.transactions t WHERE t.household_id = h.id AND t.deleted_at IS NOT NULL) AS txns_deleted,
  (SELECT count(*) FROM core.import_jobs j WHERE j.household_id = h.id) AS imports,
  (SELECT max(last_login_at)::timestamp(0) FROM auth.users u WHERE u.household_id = h.id) AS last_login
FROM auth.households h ORDER BY last_login DESC NULLS LAST;
\echo '--- profiles (linked = belongs to a login; hidden/merged ones are counted above, not listed)'
SELECT left(household_id::text, 8) AS household, display_name, relationship, linked_user_id IS NOT NULL AS linked,
       deleted_at IS NOT NULL AS deleted, created_at::timestamp(0)
FROM core.profiles WHERE deleted_at IS NULL ORDER BY household_id, created_at;
"""


def diag() -> None:
    """Everything needed to debug a broken stack, in one paste (no passwords, PANs or amounts)."""
    base = compose()
    run([*base, "ps", "-a"], check=False)
    say("--- last run of the database migration")
    run([*base, "logs", "--tail=40", "migrate"], check=False)
    say("--- database contents (counts only)")
    psql = subprocess.run([*base, "exec", "-T", "postgres", "sh", "-c", 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -P pager=off'],
                          cwd=ROOT, input=DIAG_SQL, capture_output=True, text=True, encoding="utf-8", errors="replace")
    print(psql.stdout + psql.stderr)
    say("--- Login with Gmail settings")
    text = ENV.read_text(encoding="utf-8") if ENV.exists() else ""
    cid, secret, redirect = _get(text, "OIDC_CLIENT_ID") or "", _get(text, "OIDC_CLIENT_SECRET") or "", _get(text, "OIDC_REDIRECT_URI") or ""
    if cid or secret:
        port = _get(text, "GATEWAY_PORT") or "8080"
        say("OIDC_CLIENT_ID set" if cid else "OIDC_CLIENT_ID is empty — copy it from Google Cloud → Credentials", bool(cid))
        say("OIDC_CLIENT_SECRET set" if secret else "OIDC_CLIENT_SECRET is empty", bool(secret))
        ok_uri = redirect.endswith("/api/v1/auth/oidc/callback") and f":{port}/" in redirect
        say(f"OIDC_REDIRECT_URI = {redirect}" if ok_uri else
            f"OIDC_REDIRECT_URI ({redirect or 'empty'}) should be http://localhost:{port}/api/v1/auth/oidc/callback — and exactly that in Google Cloud", ok_uri)
    else:
        say("Login with Gmail is off (OIDC_CLIENT_ID / OIDC_CLIENT_SECRET are empty)")
    for svc in ("identity", "gateway"):
        st = subprocess.run([*base, "ps", "--format", "{{.Service}} {{.State}} {{.Status}}", svc], cwd=ROOT, capture_output=True, text=True,
                            encoding="utf-8", errors="replace").stdout.strip()
        say(f"{svc}: {st or 'not running'}", "running" in st)
    for svc in ("portfolio", "identity", "gateway"):
        say(f"--- recent errors: {svc}")
        out = subprocess.run([*base, "logs", "--tail=400", svc], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace")
        lines = [ln for ln in (out.stdout + out.stderr).splitlines() if any(k in ln for k in ("Error", "error", "Traceback", "exception", " 500", "\"status\":5"))]
        print("\n".join(lines[-25:]) or "(none)")


def prune(deep: bool) -> None:
    """Reclaim Docker disk space without touching your data (volumes are never removed here)."""
    compose()  # makes sure the engine is running
    say("Docker disk usage before:")
    run(["docker", "system", "df"], check=False)
    run(["docker", "image", "prune", "-f"], check=False)              # old, untagged image versions
    run(["docker", "container", "prune", "-f"], check=False)          # stopped one-off containers
    run(["docker", "builder", "prune", "-af" if deep else "-f"], check=False)  # build cache
    say("Docker disk usage after:")
    run(["docker", "system", "df"], check=False)
    if os.name == "nt":
        say("Windows keeps the freed space inside Docker's virtual disk until it is compacted. To give it back to C: —")
        print("   1) Quit Docker Desktop, then in PowerShell:  wsl --shutdown")
        print("   2) Docker Desktop → Settings → Resources → Advanced → Disk image location shows the folder of 'docker_data.vhdx'")
        print("   3) PowerShell as Administrator:")
        print('        diskpart')
        print('        select vdisk file="C:\\Users\\<you>\\AppData\\Local\\Docker\\wsl\\disk\\docker_data.vhdx"')
        print('        attach vdisk readonly')
        print('        compact vdisk')
        print('        detach vdisk')
        print('        exit')
        print("   (Or Docker Desktop → Troubleshoot → 'Clean / Purge data' — that ALSO deletes your database volume.)")


# --------------------------------------------------------------------------- targets
def up(lite: bool) -> None:
    setup()
    base = compose()  # also starts Docker Desktop if needed
    say("checking host ports …")
    ports = resolve_ports(base, full=not lite)
    env = {**ports, **({"OTEL_ENABLED": "false"} if lite else {})}
    cmd = [*base, "up", "-d", "--build"] if lite else [*base, "--profile", "observability", "up", "-d", "--build"]
    run(cmd, env)
    # nginx resolves service addresses once at start; a rebuilt service gets a new container IP,
    # so without this the gateway keeps talking to the old one (login works, portfolio data "vanishes").
    run([*base, "restart", "gateway"], env, check=False)
    # each rebuild leaves the previous images behind untagged ("dangling") — drop them
    subprocess.call(["docker", "image", "prune", "-f"], cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print()
    say(f"App      → http://localhost:{ports['FM_HTTP_PORT']}", True)
    if "FM_GRAFANA_PORT" in ports:
        say(f"Grafana  → http://localhost:{ports['FM_GRAFANA_PORT']}", True)
    say(f"Postgres → localhost:{ports['FM_DB_PORT']} (pgAdmin; user/password from .env)", True)
    say("First build takes a few minutes. Check status with: python scripts/fm.py ps")
    env_text = ENV.read_text(encoding="utf-8") if ENV.exists() else ""
    if any(line.split("#")[0].strip().replace(" ", "") == "MARKET_DATA_PROVIDER=simulated" for line in env_text.splitlines()):
        say("Stock prices are SIMULATED (MARKET_DATA_PROVIDER=simulated in .env) — fine for a demo, wrong for your real "
            "portfolio. For live NSE/BSE prices set MARKET_DATA_PROVIDER=yahoo in .env and run this again. "
            "(Mutual-fund NAVs are always real, from AMFI.)", False)


def main(argv: list[str]) -> None:
    target = argv[0] if argv else "help"
    rest = argv[1:]
    if target == "setup":
        setup()
    elif target == "doctor":
        doctor()
    elif target == "fixeol":
        fixeol()
    elif target == "db-init":
        db_init()
    elif target == "up":
        up(lite=False)
    elif target == "up-lite":
        up(lite=True)
    elif target == "down":
        run([*compose(), "--profile", "observability", "down"])
    elif target == "clean":
        if input("This DELETES all FolioSense data volumes. Type 'yes' to continue: ").strip().lower() == "yes":
            run([*compose(), "--profile", "observability", "down", "-v"])
    elif target == "ps":
        run([*compose(), "ps"])
    elif target == "diag":
        diag()
    elif target == "prune":
        prune(deep="--all" in rest)
    elif target == "logs":
        run([*compose(), "logs", "-f", "--tail=100", *rest], check=False)
    elif target == "migrate":
        run([*compose(), "run", "--rm", "migrate"])
    elif target == "dev":
        run([PY, "scripts/dev.py", *rest], check=False)
    elif target == "test":
        run([PY, "-m", "pytest", "-q", *rest])
    elif target == "lint":
        run([PY, "-m", "ruff", "check", "libs", "services", "tests", "migrations", "scripts"])
    elif target == "smoke":
        env = {} if "BASE" in os.environ else {"BASE": "http://localhost:8080"}
        if "--direct" in rest:
            env = {}
        run([PY, "tests/e2e/smoke.py"], env)
    else:
        print(__doc__)
        print("targets: setup · db-init · doctor · fixeol · up · up-lite · down · ps · logs [service] · diag · prune [--all] · migrate · clean · dev · test · lint · smoke [--direct]")


if __name__ == "__main__":
    main(sys.argv[1:])
