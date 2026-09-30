"""Host-port selection used by `python scripts/fm.py up`."""
import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import fm  # noqa: E402


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_free_preferred_port_is_used():
    p = _free_port()
    assert fm.pick_port(p) == p


def test_busy_port_moves_to_next_free_one():
    busy = socket.socket()
    busy.bind(("127.0.0.1", 0))
    busy.listen()
    port = busy.getsockname()[1]
    try:
        assert not fm.can_bind(port)
        chosen = fm.pick_port(port)
        assert chosen is not None and chosen > port and fm.can_bind(chosen)
    finally:
        busy.close()


def test_port_owned_by_our_container_is_kept():
    busy = socket.socket()
    busy.bind(("127.0.0.1", 0))
    busy.listen()
    port = busy.getsockname()[1]
    try:
        assert fm.pick_port(port, owned={port}) == port  # re-running `up` never jumps ports
    finally:
        busy.close()


def test_choice_is_saved_to_env(tmp_path):
    env = tmp_path / ".env"
    env.write_text("POSTGRES_USER=fm\nFM_HTTP_PORT=8080          # the app\n", encoding="utf-8")
    fm.save_env_value("FM_HTTP_PORT", "8081", env)
    fm.save_env_value("FM_DB_PORT", "55433", env)
    text = env.read_text(encoding="utf-8")
    assert "FM_HTTP_PORT=8081\n" in text and "FM_DB_PORT=55433" in text and "POSTGRES_USER=fm" in text
    assert fm._get(text, "FM_HTTP_PORT") == "8081"
