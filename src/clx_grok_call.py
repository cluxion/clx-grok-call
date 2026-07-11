#!/usr/bin/env python3
"""clx-grok-call: one bounded, stateless Grok CLI call (stdlib only)."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from importlib.metadata import PackageNotFoundError, version
from typing import Any, Mapping

DEFAULT_MODEL = "grok-4.5"
DEFAULT_TIMEOUT = 120
MIN_TIMEOUT, MAX_TIMEOUT = 1, 600
EXIT_USAGE, EXIT_SPAWN, EXIT_MISSING = 2, 126, 127
EXIT_TIMEOUT, EXIT_INTERRUPT, EXIT_PIPE, EXIT_SIGTERM = 124, 130, 141, 143
GRACE = 0.5
COMMANDS = frozenset({"models", "doctor"})
_active_pgid: int | None = None


def package_version() -> str:
    try:
        return version("clx-grok-call")
    except PackageNotFoundError:
        return "0+unknown"


def _timeout_type(value: str) -> int:
    try:
        n = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"타임아웃은 정수(초)여야 합니다: {value!r}"
        ) from exc
    if not MIN_TIMEOUT <= n <= MAX_TIMEOUT:
        raise argparse.ArgumentTypeError(
            f"타임아웃은 {MIN_TIMEOUT}..{MAX_TIMEOUT}초 범위여야 합니다"
        )
    return n


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="clx-grok-call",
        description="설치된 Grok CLI를 단일 상태 없는 호출로 실행합니다.",
        epilog="프롬프트는 위치 인자 또는 표준입력. 위치 인자가 있으면 표준입력을 읽지 않습니다.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "-V", "--version", action="version",
        version=f"%(prog)s {package_version()}",
    )
    p.add_argument("-m", "--model", default=DEFAULT_MODEL, help=f"모델 (기본: {DEFAULT_MODEL})")
    p.add_argument(
        "-t", "--timeout", type=_timeout_type, default=DEFAULT_TIMEOUT,
        help=f"초 단위 타임아웃 ({MIN_TIMEOUT}..{MAX_TIMEOUT}, 기본: {DEFAULT_TIMEOUT})",
    )
    p.add_argument("--json", action="store_true", help="결과를 단일 JSON 객체로 출력")
    p.add_argument("tokens", nargs="*", help="하위 명령(models|doctor) 또는 호출 프롬프트")
    return p


def normalize_exit(code: int | None) -> int:
    if code is None:
        return 1
    return 128 + (-code) if code < 0 else code


def call_result(
    *,
    ok: bool,
    command: str,
    model: str | None,
    output: str,
    error: dict[str, str] | None,
    exit_code: int,
    timed_out: bool,
    duration_ms: int,
) -> dict[str, Any]:
    return {
        "ok": ok,
        "command": command,
        "model": model,
        "output": output,
        "error": error,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "duration_ms": duration_ms,
    }


def emit_result(
    result: dict[str, Any],
    *,
    as_json: bool,
    plain_stdout: str = "",
    plain_stderr: str = "",
) -> int:
    code = int(result["exit_code"])
    if as_json:
        sys.stdout.write(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
        sys.stdout.write("\n")
        sys.stdout.flush()
        return code
    if plain_stdout:
        sys.stdout.write(plain_stdout if plain_stdout.endswith("\n") else plain_stdout + "\n")
        sys.stdout.flush()
    if plain_stderr:
        sys.stderr.write(plain_stderr if plain_stderr.endswith("\n") else plain_stderr + "\n")
        sys.stderr.flush()
    return code


def usage_result(message: str, *, model: str | None = None) -> dict[str, Any]:
    return call_result(
        ok=False, command="call", model=model, output="",
        error={"code": "usage", "message": message},
        exit_code=EXIT_USAGE, timed_out=False, duration_ms=0,
    )


def find_grok() -> str | None:
    path = shutil.which("grok")
    if path is None or not os.path.isfile(path) or not os.access(path, os.X_OK):
        return None
    return path


def _signal_group(pgid: int, sig: int) -> None:
    """Signal whole group; never gated on leader poll()."""
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, OSError):
        pass


def _close_pipes(proc: subprocess.Popen[str]) -> None:
    for stream in (proc.stdout, proc.stderr):
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass


def _bounded_communicate(proc: subprocess.Popen[str], timeout: float) -> tuple[str, str]:
    try:
        out, err = proc.communicate(timeout=timeout)
        return out or "", err or ""
    except subprocess.TimeoutExpired:
        return "", ""
    except ValueError:
        return "", ""


def _reap_after_term(proc: subprocess.Popen[str], pgid: int, grace: float) -> tuple[str, str]:
    """TERM already sent. Bounded grace → SIGKILL group → bounded drain; never hang."""
    out, err = _bounded_communicate(proc, grace)
    _signal_group(pgid, signal.SIGKILL)
    more_out, more_err = _bounded_communicate(proc, grace)
    if proc.poll() is None:
        _close_pipes(proc)
        _bounded_communicate(proc, 0.1)
    return (out or more_out), (err or more_err)


def _on_sigterm(_signum: int, _frame: Any) -> None:
    pgid = _active_pgid
    if pgid is not None:
        _signal_group(pgid, signal.SIGTERM)
        _signal_group(pgid, signal.SIGKILL)
    raise SystemExit(EXIT_SIGTERM)


def run_grok(
    argv: list[str],
    *,
    timeout: float,
    env: Mapping[str, str] | None = None,
) -> tuple[int, str, str, bool, int]:
    global _active_pgid
    started = time.monotonic()
    try:
        proc = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=dict(env) if env is not None else None,
            start_new_session=True,
        )
    except FileNotFoundError:
        return EXIT_MISSING, "", "grok 실행 파일을 찾을 수 없습니다", False, int((time.monotonic() - started) * 1000)
    except OSError as exc:
        return EXIT_SPAWN, "", f"grok 실행 실패: {exc.strerror or exc}", False, int((time.monotonic() - started) * 1000)

    pgid = proc.pid  # session leader
    _active_pgid = pgid
    prev = signal.signal(signal.SIGTERM, _on_sigterm)
    timed_out, out, err = False, "", ""
    try:
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            _signal_group(pgid, signal.SIGTERM)
            out, err = _reap_after_term(proc, pgid, GRACE)
        except KeyboardInterrupt:
            _signal_group(pgid, signal.SIGTERM)
            _reap_after_term(proc, pgid, GRACE)
            raise SystemExit(EXIT_INTERRUPT) from None
    finally:
        _active_pgid = None
        signal.signal(signal.SIGTERM, prev)

    ms = int((time.monotonic() - started) * 1000)
    if timed_out:
        return EXIT_TIMEOUT, out, err, True, ms
    return normalize_exit(proc.returncode), out, err, False, ms


def _finish(
    *,
    command: str,
    model: str | None,
    code: int,
    out: str,
    err: str,
    timed_out: bool,
    duration_ms: int,
    as_json: bool,
    timeout_msg: str,
) -> int:
    if timed_out:
        error: dict[str, str] | None = {"code": "timeout", "message": timeout_msg}
    elif code != 0:
        error = {"code": "upstream_error", "message": err.strip() or f"exit {code}"}
    else:
        error = None
    result = call_result(
        ok=code == 0 and not timed_out,
        command=command,
        model=model,
        output=out,
        error=error,
        exit_code=code,
        timed_out=timed_out,
        duration_ms=duration_ms,
    )
    if as_json:
        return emit_result(result, as_json=True)
    return emit_result(result, as_json=False, plain_stdout=out, plain_stderr=err)


def _not_found(command: str, model: str | None, as_json: bool) -> int:
    msg = "grok 실행 파일을 찾을 수 없습니다"
    result = call_result(
        ok=False, command=command, model=model, output="",
        error={"code": "not_found", "message": msg},
        exit_code=EXIT_MISSING, timed_out=False, duration_ms=0,
    )
    if as_json:
        return emit_result(result, as_json=True)
    sys.stderr.write(msg + "\n")
    return EXIT_MISSING


def cmd_doctor(*, as_json: bool, timeout: int) -> int:
    """Bounded real probe: grok --version --no-auto-update (no prompt)."""
    path = find_grok()
    if path is None:
        msg = "doctor: grok 실행 파일이 없습니다"
        result = call_result(
            ok=False, command="doctor", model=None, output="",
            error={"code": "not_found", "message": msg},
            exit_code=EXIT_MISSING, timed_out=False, duration_ms=0,
        )
        if as_json:
            return emit_result(result, as_json=True)
        sys.stderr.write(msg + "\n")
        return EXIT_MISSING

    code, out, err, timed_out, ms = run_grok(
        [path, "--version", "--no-auto-update"],
        timeout=min(float(timeout), 30.0),
    )
    if timed_out or code != 0:
        msg = err.strip() or out.strip() or f"doctor: grok probe failed (exit {code})"
        exit_code = code if code != 0 else EXIT_TIMEOUT
        result = call_result(
            ok=False, command="doctor", model=None, output=out,
            error={"code": "doctor_failed", "message": msg},
            exit_code=exit_code, timed_out=timed_out, duration_ms=ms,
        )
        if as_json:
            return emit_result(result, as_json=True)
        sys.stderr.write(msg + "\n")
        return exit_code

    text = out.strip() or f"doctor: grok ok ({path})"
    result = call_result(
        ok=True, command="doctor", model=None, output=text,
        error=None, exit_code=0, timed_out=False, duration_ms=ms,
    )
    if as_json:
        return emit_result(result, as_json=True)
    sys.stdout.write(text if text.endswith("\n") else text + "\n")
    return 0


def cmd_models(*, as_json: bool, timeout: int, env: Mapping[str, str] | None = None) -> int:
    path = find_grok()
    if path is None:
        return _not_found("models", None, as_json)
    code, out, err, timed_out, ms = run_grok([path, "models"], timeout=float(timeout), env=env)
    return _finish(
        command="models", model=None, code=code, out=out, err=err,
        timed_out=timed_out, duration_ms=ms, as_json=as_json,
        timeout_msg="models 호출이 시간 초과되었습니다",
    )


def cmd_call(
    prompt: str,
    *,
    model: str,
    timeout: int,
    as_json: bool,
    env: Mapping[str, str] | None = None,
) -> int:
    path = find_grok()
    if path is None:
        return _not_found("call", model, as_json)
    argv = [
        path, "-p", prompt, "-m", model,
        "--output-format", "plain", "--tools", "",
        "--no-memory", "--no-subagents", "--no-auto-update",
    ]
    code, out, err, timed_out, ms = run_grok(argv, timeout=float(timeout), env=env)
    return _finish(
        command="call", model=model, code=code, out=out, err=err,
        timed_out=timed_out, duration_ms=ms, as_json=as_json,
        timeout_msg="호출이 시간 초과되었습니다",
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    raw = argv if argv is not None else sys.argv[1:]
    want_json = "--json" in raw
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        code = 0 if exc.code is None else (exc.code if isinstance(exc.code, int) else EXIT_USAGE)
        if want_json and code == EXIT_USAGE:
            return emit_result(usage_result("잘못된 인자입니다", model=DEFAULT_MODEL), as_json=True)
        return code

    tokens, as_json, model, timeout = list(args.tokens), bool(args.json), args.model, args.timeout
    try:
        if tokens and tokens[0] in COMMANDS and len(tokens) == 1:
            if tokens[0] == "doctor":
                return cmd_doctor(as_json=as_json, timeout=timeout)
            return cmd_models(as_json=as_json, timeout=timeout)

        # Positional argv wins: never read stdin. Stdin-only when no positional prompt.
        prompt = " ".join(tokens).strip() if tokens else ""
        if not prompt:
            stdin_text = "" if sys.stdin.isatty() else sys.stdin.read()
            prompt = stdin_text.strip()
            if not prompt:
                msg = "프롬프트가 비어 있습니다. 위치 인자 또는 표준입력으로 프롬프트를 제공하세요"
                if as_json:
                    return emit_result(usage_result(msg, model=model), as_json=True)
                sys.stderr.write(msg + "\n")
                return EXIT_USAGE

        return cmd_call(prompt, model=model, timeout=timeout, as_json=as_json)
    except BrokenPipeError:
        return EXIT_PIPE
    except KeyboardInterrupt:
        return EXIT_INTERRUPT


def _silence_stdout() -> None:
    """Prevent interpreter-shutdown flush from replacing exit 141 with exit 120."""
    try:
        fd = os.open(os.devnull, os.O_WRONLY)
        try:
            os.dup2(fd, sys.stdout.fileno())
        finally:
            os.close(fd)
    except (BrokenPipeError, OSError, ValueError):
        pass


def cli() -> int:
    """Console entrypoint with deterministic SIGPIPE-compatible flushing."""
    code = main()
    try:
        sys.stdout.flush()
    except BrokenPipeError:
        code = EXIT_PIPE
    if code == EXIT_PIPE:
        _silence_stdout()
    return code


if __name__ == "__main__":
    raise SystemExit(cli())
