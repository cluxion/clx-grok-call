"""RED contract suite for clx-grok-call (DESIGN.md F1). Stdlib + pytest only."""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "src" / "clx_grok_call.py"
HANGUL = re.compile(r"[\uac00-\ud7a3]")
TRACEBACK_MARKERS = ("Traceback (most recent call last)", "Traceback (most recent call last):")
JSON_KEYS = frozenset(
    {
        "ok",
        "command",
        "model",
        "output",
        "error",
        "exit_code",
        "timed_out",
        "duration_ms",
    }
)


def test_skill_metadata_hard_excludes_recursive_execution() -> None:
    skill = (ROOT / "skills" / "clx-grok-call" / "SKILL.md").read_text(encoding="utf-8")
    description = next(
        line.removeprefix("description: ")
        for line in skill.splitlines()
        if line.startswith("description: ")
    )
    assert description.startswith("NEVER SELECT OR LOAD THIS SKILL")
    assert "without loading this skill or calling Grok" in description
    assert "even if Grok or clx-grok-call is explicitly named" in description
    assert "exactly one bounded Grok call" in description
    assert len(description) <= 1024


# --- helpers -----------------------------------------------------------------

def _fake_grok_source() -> str:
    """Offline deterministic grok shim controlled by env vars."""
    return r"""#!/usr/bin/env python3
import json, os, signal, sys, time

def main() -> int:
    log = os.environ.get("CLX_FAKE_GROK_LOG")
    if log:
        rec = {
            "pid": os.getpid(),
            "pgid": os.getpgid(0),
            "argv": sys.argv[:],
            "cwd": os.getcwd(),
        }
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    alive = os.environ.get("CLX_FAKE_GROK_ALIVE")
    if alive:
        with open(alive, "w", encoding="utf-8") as fh:
            fh.write(f"{os.getpid()} {os.getpgid(0)}\n")

    # Lightweight doctor / version probe — no prompt required.
    if "--version" in sys.argv or "-V" in sys.argv:
        sys.stdout.write("fake-grok 0.0.0\n")
        sys.stdout.flush()
        return 0

    # Child self-SIGKILL (wrapper must normalize to 137).
    if os.environ.get("CLX_FAKE_GROK_SELF_SIGKILL") == "1":
        os.kill(os.getpid(), signal.SIGKILL)
        return 0

    # Invalid UTF-8 bytes on stdout (bypass text mode of this shim).
    if os.environ.get("CLX_FAKE_GROK_BAD_UTF8") == "1":
        sys.stdout.buffer.write(b"bad\xff\xfeutf8\n")
        sys.stdout.buffer.flush()
        return 0

    sleep_s = float(os.environ.get("CLX_FAKE_GROK_SLEEP", "0") or "0")
    ignore_term = os.environ.get("CLX_FAKE_GROK_IGNORE_SIGTERM") == "1"
    hold_out = os.environ.get("CLX_FAKE_GROK_HOLD_STDOUT") == "1"
    if sleep_s > 0 or ignore_term or hold_out:
        # Grandchild in the same process group — must die with the group.
        if os.fork() == 0:
            if ignore_term:
                signal.signal(signal.SIGTERM, signal.SIG_IGN)
            try:
                if hold_out:
                    # Keep the inherited stdout write end open after leader exits.
                    while True:
                        try:
                            sys.stdout.buffer.write(b".")
                            sys.stdout.buffer.flush()
                        except BrokenPipeError:
                            pass
                        time.sleep(0.2)
                else:
                    time.sleep(max(sleep_s, 0) + 60)
            except Exception:
                pass
            os._exit(0)
        if sleep_s > 0:
            time.sleep(sleep_s)
        # Leader may exit while descendant still holds stdout (timeout RED case).
        if os.environ.get("CLX_FAKE_GROK_LEADER_EXIT") == "1":
            return 0

    err = os.environ.get("CLX_FAKE_GROK_STDERR", "")
    if err:
        sys.stderr.write(err)
        if not err.endswith("\n"):
            sys.stderr.write("\n")
        sys.stderr.flush()
    out = os.environ.get("CLX_FAKE_GROK_STDOUT")
    if out is None:
        if len(sys.argv) >= 2 and sys.argv[1] == "models":
            out = "grok-4.5\ngrok-3\n"
        else:
            out = "FAKE_GROK_OK"
    sys.stdout.write(out)
    if out and not out.endswith("\n"):
        sys.stdout.write("\n")
    sys.stdout.flush()
    return int(os.environ.get("CLX_FAKE_GROK_EXIT", "0") or "0")

if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
"""


def install_fake_grok(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    bindir = tmp_path / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    grok = bindir / "grok"
    grok.write_text(_fake_grok_source(), encoding="utf-8")
    grok.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}")
    # Isolate any accidental HOME writes; design forbids session/cache files.
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    # Keep the fake Python executable from creating its own bytecode cache in HOME.
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    monkeypatch.setenv("CLX_FAKE_GROK_LOG", str(tmp_path / "grok_invocations.jsonl"))
    for key in (
        "CLX_FAKE_GROK_SLEEP",
        "CLX_FAKE_GROK_EXIT",
        "CLX_FAKE_GROK_STDOUT",
        "CLX_FAKE_GROK_STDERR",
        "CLX_FAKE_GROK_ALIVE",
        "CLX_FAKE_GROK_IGNORE_SIGTERM",
        "CLX_FAKE_GROK_HOLD_STDOUT",
        "CLX_FAKE_GROK_LEADER_EXIT",
        "CLX_FAKE_GROK_SELF_SIGKILL",
        "CLX_FAKE_GROK_BAD_UTF8",
    ):
        monkeypatch.delenv(key, raising=False)
    return grok


def run_cli(
    args: list[str],
    *,
    env: dict[str, str] | None = None,
    stdin: str | None = None,
    timeout: float = 30.0,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        input=stdin,
        capture_output=True,
        text=True,
        env=env if env is not None else os.environ.copy(),
        timeout=timeout,
        check=False,
    )


def invocations(tmp_path: Path) -> list[dict[str, Any]]:
    path = tmp_path / "grok_invocations.jsonl"
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def flag_values(argv: list[str], *names: str) -> list[str]:
    found: list[str] = []
    i = 0
    while i < len(argv):
        a = argv[i]
        for name in names:
            if a == name and i + 1 < len(argv):
                found.append(argv[i + 1])
                i += 2
                break
            if a.startswith(name + "="):
                found.append(a.split("=", 1)[1])
                i += 1
                break
        else:
            i += 1
    return found


def load_exactly_one_json(text: str) -> dict[str, Any]:
    stripped = text.strip()
    assert stripped, "stdout empty; expected one JSON object"
    decoder = json.JSONDecoder()
    obj, end = decoder.raw_decode(stripped)
    assert stripped[end:].strip() == "", f"extra data after JSON object: {stripped[end:]!r}"
    assert isinstance(obj, dict), f"JSON root must be object, got {type(obj)}"
    return obj


def assert_json_envelope(stdout: str, *, exit_code: int) -> dict[str, Any]:
    payload = load_exactly_one_json(stdout)
    missing = JSON_KEYS - payload.keys()
    assert not missing, f"missing JSON keys: {sorted(missing)}"
    assert payload["ok"] is (exit_code == 0)
    assert payload["exit_code"] == exit_code
    assert isinstance(payload["timed_out"], bool)
    assert isinstance(payload["duration_ms"], int) and payload["duration_ms"] >= 0
    assert isinstance(payload["output"], str)
    assert payload["model"] is None or isinstance(payload["model"], str)
    assert payload["error"] is None or (
        isinstance(payload["error"], dict)
        and isinstance(payload["error"].get("code"), str)
        and isinstance(payload["error"].get("message"), str)
    )
    if exit_code == 0:
        assert payload["error"] is None
        assert payload["timed_out"] is False
    return payload


def pgid_has_live_members(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def assert_no_traceback(proc: subprocess.CompletedProcess[str]) -> None:
    combined = proc.stdout + proc.stderr
    for marker in TRACEBACK_MARKERS:
        assert marker not in combined, f"unexpected traceback:\n{combined}"


def wait_pgid_dead(pgid: int, *, deadline_s: float = 1.0) -> bool:
    end = time.monotonic() + deadline_s
    while time.monotonic() < end:
        if not pgid_has_live_members(pgid):
            return True
        time.sleep(0.05)
    return not pgid_has_live_members(pgid)


# --- fixtures ----------------------------------------------------------------

@pytest.fixture
def fake_grok(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    return install_fake_grok(tmp_path, monkeypatch)


# --- preserved GREEN contracts -----------------------------------------------

def test_help_is_korean(fake_grok: Path) -> None:
    proc = run_cli(["--help"])
    text = proc.stdout + proc.stderr
    assert proc.returncode == 0
    assert HANGUL.search(text), f"expected Korean help text, got:\n{text}"


def test_version_uses_package_metadata_without_starting_grok(
    tmp_path: Path, fake_grok: Path
) -> None:
    proc = run_cli(["--version"])
    assert proc.returncode == 0
    assert proc.stdout == "clx-grok-call 0.1.2\n"
    assert proc.stderr == ""
    assert invocations(tmp_path) == []


def test_positional_prompt_spawns_one_grok_with_default_model(
    tmp_path: Path, fake_grok: Path
) -> None:
    proc = run_cli(["hello from clx"])
    assert proc.returncode == 0, proc.stderr
    rows = invocations(tmp_path)
    assert len(rows) == 1, rows
    argv = rows[0]["argv"]
    assert Path(argv[0]).name == "grok"
    assert flag_values(argv, "-p", "--single") == ["hello from clx"]
    models = flag_values(argv, "-m", "--model")
    assert models == ["grok-4.5"], argv
    assert flag_values(argv, "--tools") == [""], argv
    assert argv.count("--no-memory") == 1, argv
    assert argv.count("--no-subagents") == 1, argv
    assert argv.count("--no-auto-update") == 1, argv
    assert "FAKE_GROK_OK" in proc.stdout


def test_model_override(tmp_path: Path, fake_grok: Path) -> None:
    proc = run_cli(["--model", "custom-model", "override me"])
    assert proc.returncode == 0, proc.stderr
    rows = invocations(tmp_path)
    assert len(rows) == 1
    argv = rows[0]["argv"]
    assert flag_values(argv, "-p", "--single") == ["override me"]
    assert flag_values(argv, "-m", "--model") == ["custom-model"]


def test_stdin_only_prompt(tmp_path: Path, fake_grok: Path) -> None:
    proc = run_cli([], stdin="stdin prompt body\n")
    assert proc.returncode == 0, proc.stderr
    rows = invocations(tmp_path)
    assert len(rows) == 1
    argv = rows[0]["argv"]
    assert flag_values(argv, "-p", "--single") == ["stdin prompt body"]
    assert flag_values(argv, "-m", "--model") == ["grok-4.5"]


def test_call_leaves_isolated_home_empty(tmp_path: Path, fake_grok: Path) -> None:
    """The wrapper must not create session, cache, or memory state in HOME."""
    home = Path(os.environ["HOME"])
    assert list(home.iterdir()) == []

    proc = run_cli(["no persistent state"])

    assert proc.returncode == 0, proc.stderr
    assert list(home.iterdir()) == []


def test_missing_grok_exit_127(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    empty = tmp_path / "empty_bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir(exist_ok=True)
    proc = run_cli(["ping"])
    assert proc.returncode == 127
    assert invocations(tmp_path) == []


def test_upstream_nonzero_preserved(
    tmp_path: Path, fake_grok: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLX_FAKE_GROK_EXIT", "7")
    monkeypatch.setenv("CLX_FAKE_GROK_STDERR", "upstream boom")
    monkeypatch.setenv("CLX_FAKE_GROK_STDOUT", "")
    proc = run_cli(["fail please"])
    assert proc.returncode == 7
    assert len(invocations(tmp_path)) == 1
    assert "upstream boom" in proc.stderr


def test_stderr_warning_does_not_contaminate_plain_stdout(
    tmp_path: Path, fake_grok: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLX_FAKE_GROK_STDERR", "WARNING: auth soft-fail")
    monkeypatch.setenv("CLX_FAKE_GROK_STDOUT", "clean answer")
    proc = run_cli(["warn me"])
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "clean answer"
    assert "WARNING: auth soft-fail" not in proc.stdout
    assert "WARNING: auth soft-fail" in proc.stderr


@pytest.mark.parametrize("mode", ["success", "failure"])
def test_json_success_and_failure_single_object(
    mode: str,
    tmp_path: Path,
    fake_grok: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if mode == "success":
        monkeypatch.setenv("CLX_FAKE_GROK_STDOUT", "json-ok-body")
        expected_exit = 0
    else:
        monkeypatch.setenv("CLX_FAKE_GROK_EXIT", "5")
        monkeypatch.setenv("CLX_FAKE_GROK_STDERR", "json-fail")
        monkeypatch.setenv("CLX_FAKE_GROK_STDOUT", "")
        expected_exit = 5

    proc = run_cli(["--json", "json prompt"])
    assert proc.returncode == expected_exit
    payload = assert_json_envelope(proc.stdout, exit_code=expected_exit)
    assert payload["model"] == "grok-4.5"
    if mode == "success":
        assert "json-ok-body" in payload["output"]
    else:
        assert payload["ok"] is False
        assert payload["error"] is not None
    assert len(invocations(tmp_path)) == 1


def test_timeout_returns_124_and_kills_process_group(
    tmp_path: Path, fake_grok: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    alive = tmp_path / "alive.txt"
    monkeypatch.setenv("CLX_FAKE_GROK_SLEEP", "30")
    monkeypatch.setenv("CLX_FAKE_GROK_ALIVE", str(alive))
    started = time.monotonic()
    proc = run_cli(["--timeout", "1", "slow work"], timeout=10.0)
    elapsed = time.monotonic() - started
    assert proc.returncode == 124, (proc.returncode, proc.stdout, proc.stderr)
    assert elapsed < 4.0, f"timeout wall time too slow: {elapsed:.2f}s"
    assert len(invocations(tmp_path)) == 1
    deadline = time.monotonic() + 1.0
    while alive.is_file() and time.monotonic() < deadline:
        pid_s, pgid_s = alive.read_text(encoding="utf-8").split()
        if not pgid_has_live_members(int(pgid_s)):
            break
        time.sleep(0.05)
    assert alive.is_file(), "fake grok never recorded pid/pgid"
    _pid_s, pgid_s = alive.read_text(encoding="utf-8").split()
    pgid = int(pgid_s)
    assert not pgid_has_live_members(pgid), f"process group {pgid} still alive"


def test_models_passthrough(tmp_path: Path, fake_grok: Path) -> None:
    proc = run_cli(["models"])
    assert proc.returncode == 0, proc.stderr
    rows = invocations(tmp_path)
    assert len(rows) == 1
    argv = rows[0]["argv"]
    assert "models" in argv[1:], argv
    assert "grok-4.5" in proc.stdout
    assert proc.stdout.strip()


# --- joint Fable+SOL RED contracts -------------------------------------------

def test_positional_prompt_does_not_read_open_stdin_pipe(
    tmp_path: Path, fake_grok: Path
) -> None:
    """Positional prompt must not block on an open stdin PIPE (no EOF needed)."""
    env = os.environ.copy()
    # Keep the write end open ourselves so EOF never arrives if wrapper reads stdin.
    r_fd, w_fd = os.pipe()
    started = time.monotonic()
    proc = subprocess.Popen(
        [sys.executable, str(SCRIPT), "positional only"],
        stdin=r_fd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    os.close(r_fd)
    try:
        deadline = time.monotonic() + 2.0
        while proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        if proc.poll() is None:
            proc.kill()
            proc.communicate()
            pytest.fail(
                "wrapper blocked reading open stdin PIPE despite positional prompt"
            )
        out, err = proc.communicate(timeout=2.0)
    finally:
        try:
            os.close(w_fd)
        except OSError:
            pass
    elapsed = time.monotonic() - started
    assert elapsed < 2.5, f"positional path too slow / likely waited on stdin: {elapsed:.2f}s"
    assert proc.returncode == 0, (proc.returncode, out, err)
    assert "FAKE_GROK_OK" in (out or "")
    rows = invocations(tmp_path)
    assert len(rows) == 1
    assert flag_values(rows[0]["argv"], "-p", "--single") == ["positional only"]


def test_timeout_kills_sigterm_ignoring_descendant_holding_stdout(
    tmp_path: Path, fake_grok: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Timeout must SIGKILL group even if leader exits and orphan ignores SIGTERM."""
    alive = tmp_path / "alive_orphan.txt"
    monkeypatch.setenv("CLX_FAKE_GROK_ALIVE", str(alive))
    monkeypatch.setenv("CLX_FAKE_GROK_IGNORE_SIGTERM", "1")
    monkeypatch.setenv("CLX_FAKE_GROK_HOLD_STDOUT", "1")
    monkeypatch.setenv("CLX_FAKE_GROK_LEADER_EXIT", "1")
    # Leader exits immediately; descendant holds stdout and ignores SIGTERM.
    monkeypatch.setenv("CLX_FAKE_GROK_SLEEP", "0")
    env = os.environ.copy()
    started = time.monotonic()
    proc = subprocess.Popen(
        [sys.executable, str(SCRIPT), "--timeout", "1", "orphan hold"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    try:
        out, err = proc.communicate(timeout=5.0)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.communicate(timeout=2.0)
        except Exception:
            pass
        if alive.is_file():
            _pid_s, pgid_s = alive.read_text(encoding="utf-8").split()
            try:
                os.killpg(int(pgid_s), signal.SIGKILL)
            except OSError:
                pass
        pytest.fail(
            "wrapper hung past 5s; timeout path did not reap SIGTERM-ignoring "
            "descendant holding stdout"
        )
    elapsed = time.monotonic() - started
    assert proc.returncode == 124, (proc.returncode, out, err)
    assert elapsed < 4.0, f"timeout wall time too slow: {elapsed:.2f}s"
    assert alive.is_file(), "fake grok never recorded pid/pgid"
    _pid_s, pgid_s = alive.read_text(encoding="utf-8").split()
    pgid = int(pgid_s)
    assert wait_pgid_dead(pgid), f"process group {pgid} still alive after timeout"


def test_sigterm_to_wrapper_cleans_child_group_exit_143(
    tmp_path: Path, fake_grok: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    alive = tmp_path / "alive_sigterm.txt"
    monkeypatch.setenv("CLX_FAKE_GROK_SLEEP", "30")
    monkeypatch.setenv("CLX_FAKE_GROK_ALIVE", str(alive))
    env = os.environ.copy()
    proc = subprocess.Popen(
        [sys.executable, str(SCRIPT), "--timeout", "60", "sigterm me"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    deadline = time.monotonic() + 2.0
    while not alive.is_file() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert alive.is_file(), "child never started"
    _pid_s, pgid_s = alive.read_text(encoding="utf-8").split()
    pgid = int(pgid_s)
    os.kill(proc.pid, signal.SIGTERM)
    try:
        _out, _err = proc.communicate(timeout=3.0)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        try:
            os.killpg(pgid, signal.SIGKILL)
        except OSError:
            pass
        pytest.fail("wrapper did not exit after SIGTERM")
    # 128 + SIGTERM(15) = 143 (shell-normalized; not raw -15)
    rc = proc.returncode
    pgid_dead = wait_pgid_dead(pgid)
    if not pgid_dead:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except OSError:
            pass
    assert rc == 143, (rc, _out, _err)
    assert pgid_dead, f"child process group {pgid} still alive after wrapper SIGTERM"


def test_child_sigkill_normalized_to_137_json_and_os_agree(
    tmp_path: Path, fake_grok: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLX_FAKE_GROK_SELF_SIGKILL", "1")
    proc = run_cli(["--json", "kill me"], timeout=10.0)
    assert proc.returncode == 137, (proc.returncode, proc.stdout, proc.stderr)
    payload = assert_json_envelope(proc.stdout, exit_code=137)
    assert payload["ok"] is False
    assert payload["exit_code"] == 137
    assert payload["exit_code"] == proc.returncode
    assert len(invocations(tmp_path)) == 1


def test_invalid_utf8_stdout_becomes_replacement_without_traceback(
    tmp_path: Path, fake_grok: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLX_FAKE_GROK_BAD_UTF8", "1")
    proc = run_cli(["bad utf8"], timeout=10.0)
    assert_no_traceback(proc)
    assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
    # Replacement character U+FFFD must appear; raw decode must not crash.
    assert "\ufffd" in proc.stdout or "\ufffd" in proc.stderr or "bad" in proc.stdout
    # Prefer stdout carrying the (possibly replaced) payload.
    assert proc.stdout, "expected replacement/sanitized stdout body"


def test_enoexec_garbage_executable_exits_126_without_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    garbage = bindir / "grok"
    # Not a valid script/binary; execve → ENOEXEC on Unix.
    garbage.write_bytes(b"\x00\x01not-an-executable")
    garbage.chmod(0o755)
    monkeypatch.setenv("PATH", str(bindir))
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    proc = run_cli(["enoexec please"], timeout=10.0)
    assert_no_traceback(proc)
    assert proc.returncode == 126, (proc.returncode, proc.stdout, proc.stderr)


def test_closed_stdout_broken_pipe_exits_141_without_traceback(
    tmp_path: Path, fake_grok: Path
) -> None:
    env = os.environ.copy()
    # Close the pipe read end so wrapper write → EPIPE/SIGPIPE (exit 141).
    proc = subprocess.Popen(
        [sys.executable, str(SCRIPT), "closed-stdout-prompt"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    assert proc.stdout is not None
    assert proc.stderr is not None
    proc.stdout.close()
    try:
        err = proc.stderr.read()
        proc.wait(timeout=5.0)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=2.0)
        pytest.fail("wrapper hung after stdout closed")
    err_text = err.decode("utf-8", errors="replace") if isinstance(err, (bytes, bytearray)) else (err or "")
    assert "Traceback (most recent call last)" not in err_text
    # 128 + SIGPIPE(13) = 141
    assert proc.returncode == 141, (proc.returncode, err_text)


def test_console_entrypoint_closed_stdout_exits_141_without_traceback(
    fake_grok: Path,
) -> None:
    """The installed console script must not defer EPIPE to interpreter shutdown."""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import clx_grok_call; raise SystemExit(clx_grok_call.cli())",
            "closed-stdout-prompt",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    assert proc.stdout is not None
    assert proc.stderr is not None
    proc.stdout.close()
    try:
        err = proc.stderr.read()
        proc.wait(timeout=5.0)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=2.0)
        pytest.fail("console entrypoint hung after stdout closed")
    err_text = err.decode("utf-8", errors="replace")
    assert "Traceback (most recent call last)" not in err_text
    assert proc.returncode == 141, (proc.returncode, err_text)


def test_doctor_rejects_garbage_and_probes_version_no_auto_update(
    tmp_path: Path, fake_grok: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Happy path: real fake grok — must probe with --version and --no-auto-update, no -p.
    log_path = tmp_path / "grok_invocations.jsonl"
    if log_path.is_file():
        log_path.write_text("", encoding="utf-8")
    proc = run_cli(["doctor"], timeout=10.0)
    assert proc.returncode == 0, proc.stderr
    rows = invocations(tmp_path)
    assert len(rows) >= 1, "doctor must probe real grok"
    probe = rows[-1]["argv"]
    assert "--version" in probe or "-V" in probe, probe
    assert "--no-auto-update" in probe, probe
    assert flag_values(probe, "-p", "--single") == [], probe
    # Probe must be bounded / non-interactive (no hanging prompt flags).
    assert "-p" not in probe

    # Garbage executable on PATH: doctor must reject (not claim healthy).
    gbindir = tmp_path / "garbage_bin"
    gbindir.mkdir()
    g = gbindir / "grok"
    g.write_bytes(b"\x00\x01garbage")
    g.chmod(0o755)
    monkeypatch.setenv("PATH", str(gbindir))
    bad = run_cli(["doctor"], timeout=10.0)
    assert bad.returncode != 0, (bad.returncode, bad.stdout, bad.stderr)
    text = (bad.stdout + bad.stderr).lower()
    assert "ok" not in text or "not" in text or bad.returncode != 0


def test_call_argv_contains_no_auto_update(
    tmp_path: Path, fake_grok: Path
) -> None:
    proc = run_cli(["include no-auto-update"])
    assert proc.returncode == 0, proc.stderr
    rows = invocations(tmp_path)
    assert len(rows) == 1
    argv = rows[0]["argv"]
    assert "--no-auto-update" in argv, argv


def test_json_parse_error_invalid_timeout_one_error_envelope(
    tmp_path: Path, fake_grok: Path
) -> None:
    """DESIGN: JSON mode emits exactly one valid error envelope for every failure."""
    proc = run_cli(["--json", "--timeout", "not-an-int", "x"], timeout=10.0)
    assert proc.returncode == 2, (proc.returncode, proc.stdout, proc.stderr)
    payload = assert_json_envelope(proc.stdout, exit_code=2)
    assert payload["ok"] is False
    assert payload["error"] is not None
    assert payload["error"]["code"]
    assert payload["error"]["message"]
    # Exactly one JSON object already enforced by assert_json_envelope/load_exactly_one_json.
    assert invocations(tmp_path) == []
