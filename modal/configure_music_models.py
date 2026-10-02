"""Configure and optionally deploy both TIMBOR music services to Modal.

Run from the repository root with ``python modal/configure_music_models.py``. The
script stores API keys in the ignored project .env, deploys both Modal apps,
and records their invocation URLs without enabling remote generation by default.
"""
from __future__ import annotations

import argparse
import os
import re
import secrets
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"
ENV_EXAMPLE = ROOT / ".env.example"
SERVICES = (
    ("YuE2", "YUE2", "timbor-yue2", "modal/yue_engine.py"),
    ("ACE-Step", "ACESTEP", "timbor-acestep", "modal/ace_step_engine.py"),
)
URL_PATTERN = re.compile(r"https://[A-Za-z0-9.-]+\.modal\.run(?:/[A-Za-z0-9_./?=&%-]*)?")


def _initial_env() -> list[str]:
    """Read existing settings or seed a new .env from the committed template."""
    if ENV_PATH.is_file():
        return ENV_PATH.read_text(encoding="utf-8").splitlines()
    if not ENV_EXAMPLE.is_file():
        raise RuntimeError(".env.example is missing, cannot initialize project configuration")
    return [line for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines()
            if line.strip() != "[TEMPLATE]"]


def _get_value(lines: list[str], name: str) -> str:
    for line in lines:
        key, separator, value = line.partition("=")
        if separator and key.strip() == name:
            return value.strip().strip("\"'")
    return ""


def _set_value(lines: list[str], name: str, value: str) -> None:
    """Update matching dotenv assignments, or append the setting if absent."""
    found = False
    for index, line in enumerate(lines):
        key, separator, _ = line.partition("=")
        if separator and key.strip() == name:
            if not found:
                lines[index] = f"{name}={value}"
                found = True
            else:
                lines[index] = f"# duplicate {name} removed by Modal setup"
    if not found:
        lines.append(f"{name}={value}")


def _save_env(lines: list[str]) -> None:
    """Write project settings without printing credentials or changing other keys."""
    content = "\n".join(lines).rstrip() + "\n"
    if ENV_PATH.exists():
        ENV_PATH.write_text(content, encoding="utf-8", newline="\n")
        return
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(ENV_PATH, flags, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
        output.write(content)
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        security_descriptor = wintypes.LPVOID()
        convert = ctypes.windll.advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW
        convert.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,
                            ctypes.POINTER(wintypes.LPVOID), ctypes.POINTER(wintypes.DWORD)]
        convert.restype = wintypes.BOOL
        set_security = ctypes.windll.advapi32.SetFileSecurityW
        set_security.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.LPVOID]
        set_security.restype = wintypes.BOOL
        set_attributes = ctypes.windll.kernel32.SetFileAttributesW
        set_attributes.argtypes = [wintypes.LPCWSTR, wintypes.DWORD]
        set_attributes.restype = wintypes.BOOL
        try:
            if not convert("D:P(A;;FA;;;OW)", 1, ctypes.byref(security_descriptor), None):
                raise OSError(ctypes.get_last_error(), "could not secure .env ACL")
            if not set_security(str(ENV_PATH), 0x00000004, security_descriptor):
                raise OSError(ctypes.get_last_error(), "could not secure .env ACL")
            if not set_attributes(str(ENV_PATH), 0x00000002):
                raise OSError(ctypes.get_last_error(), "could not secure .env ACL")
        except OSError:
            ENV_PATH.unlink(missing_ok=True)
            raise
        finally:
            if security_descriptor:
                ctypes.windll.kernel32.LocalFree(security_descriptor)


def _ensure_api_keys(lines: list[str]) -> tuple[str, ...]:
    generated: list[str] = []
    for _, prefix, _, _ in SERVICES:
        name = f"{prefix}_API_KEY"
        if not _get_value(lines, name):
            _set_value(lines, name, secrets.token_urlsafe(32))
            generated.append(name)
    return tuple(generated)


def _extract_url(output: str, app_name: str) -> str | None:
    """Find the deployed invocation URL for an app in Modal CLI output."""
    for candidate in URL_PATTERN.findall(output):
        host = candidate.split("/", 3)[2].lower()
        if app_name in host and "--" in host:
            return candidate.rstrip(".,;)")
    return None


def _redact(text: str, lines: list[str]) -> str:
    for _, prefix, _, _ in SERVICES:
        key = _get_value(lines, f"{prefix}_API_KEY")
        if key:
            text = text.replace(key, "[REDACTED]")
    return text


def _deploy(service: tuple[str, str, str, str], lines: list[str]) -> str:
    label, _, app_name, relative_path = service
    command = shutil.which("modal")
    if not command:
        raise RuntimeError("Modal CLI is not installed; install it and run `modal setup` first")
    result = subprocess.run(
        [command, "deploy", relative_path], cwd=ROOT,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, check=False)
    output = _redact(result.stdout or "", lines)
    if output:
        print(output.rstrip())
    if result.returncode:
        raise RuntimeError(f"Modal deployment failed for {label} (exit {result.returncode})")

    url = _extract_url(result.stdout or "", app_name)
    while not url:
        try:
            supplied = input(f"Paste the {label} https://*.modal.run invocation URL: ").strip()
        except EOFError as exc:
            raise RuntimeError(
                f"{label} deployed, but its invocation URL was not detected. "
                f"Set {service[1]}_MODAL_URL in .env to the https://*.modal.run URL."
            ) from exc
        supplied = supplied.rstrip("/")
        if supplied.startswith("https://") and ".modal.run" in supplied and "modal.com/apps/" not in supplied:
            url = supplied
        else:
            print("Use the deployed invocation URL ending in .modal.run, not a Modal dashboard link.")
    return url.rstrip("/")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--configure-only", action="store_true",
        help="create missing API keys in .env but do not deploy either service")
    parser.add_argument(
        "--enable", action="store_true",
        help="set both *_ENABLED=true after both deployments succeed")
    args = parser.parse_args(argv)
    if args.configure_only and args.enable:
        parser.error("--enable requires deployment; it cannot be used with --configure-only")

    try:
        lines = _initial_env()
        generated = _ensure_api_keys(lines)
        _save_env(lines)
        if generated:
            print("Created missing API keys in the ignored .env file: " + ", ".join(generated))
        else:
            print("Reusing API keys already configured in the ignored .env file.")

        if args.configure_only:
            print("No Modal deployment was run. Deploy later with this script without --configure-only.")
            return 0

        for service in SERVICES:
            label, prefix, _, _ = service
            print(f"\nDeploying {label}...")
            url = _deploy(service, lines)
            _set_value(lines, f"{prefix}_MODAL_URL", url)
            _save_env(lines)
            print(f"Saved {prefix}_MODAL_URL in .env.")

        if args.enable:
            for _, prefix, _, _ in SERVICES:
                _set_value(lines, f"{prefix}_ENABLED", "true")
            _save_env(lines)
            print("Both remote engines are enabled in .env.")
        else:
            print("Both services are deployed, but remain opt-in; *_ENABLED settings were not changed.")
        print("Review Modal resource usage and model licenses before generating audio.")
        return 0
    except (OSError, RuntimeError) as exc:
        print(f"Modal setup failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
