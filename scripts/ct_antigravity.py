"""Antigravity CLI preflight, per-session profiles, and headless result decoding."""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager, ExitStack
import ctypes
from ctypes import wintypes
from dataclasses import dataclass, field
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess


MAX_PROMPT_BYTES = 64 * 1024
MAX_STDOUT_BYTES = 1024 * 1024
MAX_STDERR_BYTES = 256 * 1024
_MAX_CONFIG_BYTES = 64 * 1024
_MAX_CONFIG_ENTRIES = 256
_MAX_NATIVE_ROOTS = 16
_GOOGLE_SIGNER = "Google LLC"
_VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][a-zA-Z0-9.-]+)?")
_PROFILE_FILE = "agy-session-settings.json"
_LOG_FILE = "agy-session.log"
_WINDOWS_ENV = frozenset({
    "APPDATA", "COMMONPROGRAMFILES", "HOME", "HOMEDRIVE", "HOMEPATH", "LOCALAPPDATA",
    "PROGRAMDATA", "SYSTEMDRIVE", "SYSTEMROOT", "TEMP", "TMP", "USERPROFILE", "WINDIR",
})

# The November 2026 signed Windows installation had no PE version resource.
# Its embedded version agrees with the recorded 1.5.9 installation inspection.
# This table labels metadata only. It grants no bootstrap or invocation capability.
_OBSERVED_VERSIONS = (
    ("8803c289cac04d6e53f2211592d9981c2d5760bc0d9b4a7f222ef6ecc7abb38a", "1.5.9"),
)

# Records pin (signed binary SHA-256, version, baseline profile + source digest).
# Add a record only after review of exhaustive startup sources on the target
# runtime, --settings precedence, and the version-specific headless flags.
# CHECK separately proves runtime stdin, output, and each permission canary.
_REVIEWED_BOOTSTRAPS: tuple[tuple[str, str, str], ...] = ()

_MODES = {"READ": "plan", "EDIT": "accept-edits"}
_COMMON_DENY = ("command(*)", "unsandboxed(*)", "mcp(*)", "read_url(*)", "execute_url(*)")
_CUSTOM_DIRS = (".agents", ".agent", "_agents", "_agent")
_INSTRUCTIONS = ("AGENTS.md", "GEMINI.md")
_COLLECTIONS = ("agents", "plugins", "rules", "skills")
_CONFIG_FILES = (
    "settings.json", "hooks.json", "mcp_config.json", "agents.json", "plugins.json", "rules.json", "skills.json",
)

_SIGNATURE_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$path = $env:CT_AGY_FILE_FOR_SIGNATURE
$signature = Microsoft.PowerShell.Security\Get-AuthenticodeSignature -LiteralPath $path
if ($signature.Status -ne 'Valid' -or $null -eq $signature.SignerCertificate) { exit 2 }
$certificate = $signature.SignerCertificate
$subject = $certificate.Subject -split ',\s*'
if ($certificate.GetNameInfo([System.Security.Cryptography.X509Certificates.X509NameType]::SimpleName, $false) -cne 'Google LLC' -or
    $subject -cnotcontains 'CN=Google LLC' -or $subject -cnotcontains 'O=Google LLC' -or
    $subject -cnotcontains 'C=US' -or $subject -cnotcontains 'SERIALNUMBER=3582691' -or
    $subject -cnotcontains 'OID.1.3.6.1.4.1.311.60.2.1.2=Delaware') { exit 2 }
$version = [System.Diagnostics.FileVersionInfo]::GetVersionInfo($path)
[pscustomobject]@{signer='Google LLC'; version=$version.ProductVersion; file_version=$version.FileVersion} |
    Microsoft.PowerShell.Utility\ConvertTo-Json -Compress
"""


class CliPreflightError(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class LaunchSpec:
    executable: Path
    args: tuple[str, ...]
    cwd: Path
    env: dict[str, str] = field(repr=False)
    stdin_bytes: bytes = field(repr=False)


@dataclass(frozen=True)
class BinaryMetadata:
    binary_path: Path | None
    binary_sha256: str | None
    version: str | None
    reason: str
    signer: str | None = None
    _identity: tuple[int, ...] = field(default=(), repr=False, compare=False)


@dataclass(frozen=True)
class Preflight(BinaryMetadata):
    config_fingerprint: str | None = None
    _runtime_env: tuple[tuple[str, str], ...] = field(default=(), repr=False, compare=False)
    _config_roots: tuple[Path, ...] = field(default=(), repr=False, compare=False)


@dataclass(frozen=True)
class CliResult:
    ok: bool
    reason: str
    response: str | None = field(repr=False)
    metadata: dict[str, object] = field(default_factory=dict)


def _strict_json(raw: bytes) -> object:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    def invalid_constant(_):
        raise ValueError("non-finite JSON constant")

    return json.loads(raw.decode("utf-8"), object_pairs_hook=unique, parse_constant=invalid_constant)


def _windows_directory() -> Path:
    buffer = ctypes.create_unicode_buffer(32768)
    count = ctypes.windll.kernel32.GetSystemWindowsDirectoryW(buffer, len(buffer))
    if not 0 < count < len(buffer):
        raise OSError("Windows directory unavailable")
    return Path(buffer.value)


def _read_windows_signature(path: Path) -> tuple[str, str | None]:
    if os.name != "nt":
        raise OSError("Authenticode requires Windows")
    windows = _windows_directory()
    powershell = windows / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    verified = subprocess.run(
        [str(powershell), "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", _SIGNATURE_SCRIPT],
        capture_output=True,
        stdin=subprocess.DEVNULL,
        cwd=windows,
        env={
            "SystemRoot": str(windows),
            "PSModulePath": str(powershell.parent / "Modules"),
            "CT_AGY_FILE_FOR_SIGNATURE": str(path),
        },
        timeout=12,
        check=True,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    result = _strict_json(verified.stdout)
    if not isinstance(result, dict) or result.get("signer") != _GOOGLE_SIGNER:
        raise ValueError("unexpected Authenticode result")
    for key in ("version", "file_version"):
        value = result.get(key)
        if isinstance(value, str) and _VERSION.fullmatch(value):
            return _GOOGLE_SIGNER, value
    return _GOOGLE_SIGNER, None


def _local_path(path: Path) -> bool:
    # Avoid UNC paths, path aliases, and NTFS alternate streams.
    return (path.is_absolute() and not path.drive.startswith(("\\", "//"))
            and all(part != ".." and ":" not in part for part in path.parts[1:]))


def _plain_stat(path: Path) -> os.stat_result | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise OSError("reparse point")
    return info


def _plain_path(path: Path) -> os.stat_result | None:
    if not _local_path(path):
        raise OSError("path must be absolute and local")
    for parent in reversed(path.parents):
        info = _plain_stat(parent)
        if info is None:
            return None
        if not stat.S_ISDIR(info.st_mode):
            raise OSError("path component is not a directory")
    return _plain_stat(path)


def _identity(info: os.stat_result) -> tuple[int, ...]:
    # On Windows 3.12+, pathname and handle stats agree on birthtime. The
    # deprecated ctime field can report different kinds of timestamps.
    timestamp = getattr(info, "st_birthtime_ns", info.st_ctime_ns) if os.name == "nt" else info.st_ctime_ns
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, timestamp)


def _binary_identity(path: Path) -> tuple[int, ...]:
    info = _plain_path(path)
    if info is None:
        raise FileNotFoundError("binary unavailable")
    if not stat.S_ISREG(info.st_mode):
        raise OSError("binary is not a regular file")
    return _identity(info)


def _runtime_environment(env: Mapping[str, str]) -> dict[str, str]:
    result = {}
    for key, value in env.items():
        canonical = key.upper()
        if canonical in _WINDOWS_ENV:
            if "\0" in value or (canonical in result and result[canonical] != value):
                raise CliPreflightError("CUSTOMIZATIONS_BLOCKED")
            if value:
                result[canonical] = value
    return result


def _binary_metadata(local_app_data: str | None) -> BinaryMetadata:
    if not local_app_data:
        return BinaryMetadata(None, None, None, "CLI_MISSING")
    path = Path(local_app_data) / "agy" / "bin" / "agy.exe"
    try:
        identity = _binary_identity(path)
        with path.open("rb") as stream:
            if identity != _identity(os.fstat(stream.fileno())):
                raise OSError("binary replaced before read")
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
            signer, version = _read_windows_signature(path)
            if identity != _identity(os.fstat(stream.fileno())) or identity != _binary_identity(path):
                raise OSError("binary changed during verification")
    except FileNotFoundError:
        return BinaryMetadata(path, None, None, "CLI_MISSING")
    except (OSError, ValueError, subprocess.SubprocessError):
        return BinaryMetadata(path if _local_path(path) else None, None, None, "CLI_UNVERIFIED")
    if signer != _GOOGLE_SIGNER:
        return BinaryMetadata(path, None, None, "CLI_UNVERIFIED")
    if version is None:
        version = dict(_OBSERVED_VERSIONS).get(digest)
    if version is None or not _VERSION.fullmatch(version):
        return BinaryMetadata(path, digest, None, "CLI_UNVERIFIED", signer, identity)
    return BinaryMetadata(path, digest, version, "OK", signer, identity)


def inspect_binary_metadata(env: Mapping[str, str]) -> BinaryMetadata:
    """Read static binary metadata without launching agy or inspecting native settings."""
    paths = {value for key, value in env.items() if key.upper() == "LOCALAPPDATA"}
    if len(paths) > 1 or any("\0" in value for value in paths):
        return BinaryMetadata(None, None, None, "CLI_UNVERIFIED")
    return _binary_metadata(next(iter(paths), None))


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _profile_bytes(role: str) -> bytes:
    deny = list(_COMMON_DENY)
    if role == "READ":
        deny.append("write_file(*)")
    return _json_bytes({
        "agentMode": _MODES[role],
        "allowNonWorkspaceAccess": False,
        "disableSlashCommands": True,
        "modelProvider": "",
        "permissions": {"allow": [], "ask": [], "deny": deny},
        "toolPermission": "request-review",
        "useG1Credits": False,
    }) + b"\n"


def _arguments(role: str, settings: str, log: str, timeout: str) -> tuple[str, ...]:
    return (
        "--mode", _MODES[role], "--disable-slash-commands", "--settings", settings,
        "--input-format", "text", "--output-format", "json", "--print-timeout", timeout,
        "--log-file", log,
    )


def _source_bytes(path: Path) -> bytes | None:
    info = _plain_path(path)
    if info is None:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_CONFIG_BYTES:
        raise OSError("configuration is not a bounded regular file")
    with path.open("rb") as stream:
        identity = _identity(info)
        if _identity(os.fstat(stream.fileno())) != identity:
            raise OSError("configuration changed while opening")
        data = stream.read(_MAX_CONFIG_BYTES + 1)
        if len(data) > _MAX_CONFIG_BYTES or _identity(os.fstat(stream.fileno())) != identity:
            raise OSError("configuration changed during read")
    updated = _plain_path(path)
    if updated is None or _identity(updated) != identity:
        raise OSError("configuration changed during read")
    return data


def _dir_exists(path: Path) -> bool:
    info = _plain_path(path)
    if info is None:
        return False
    if not stat.S_ISDIR(info.st_mode):
        raise OSError("expected a configuration directory")
    return True


def _only_known_entries(path: Path, names: tuple[str, ...]) -> None:
    if _dir_exists(path) and any(entry.name not in names for entry in path.iterdir()):
        raise ValueError("unknown customization entry")


def _safe_config(name: str, value: object) -> bool:
    if not isinstance(value, dict):
        return False
    if name == "hooks.json":
        return value in ({}, {"hooks": {}})
    if name == "mcp_config.json":
        servers = value.get("mcpServers")
        return value == {} or (
            value.keys() == {"mcpServers"} and isinstance(servers, dict)
            and all(isinstance(server, dict) and server.get("disabled") is True for server in servers.values())
        )
    if name == "settings.json":
        safe_values = {
            "allowNonWorkspaceAccess": (False,), "disableSlashCommands": (True,),
            "modelProvider": ("",), "toolPermission": ("request-review",), "useG1Credits": (False,),
            "permissions": ({}, {"allow": [], "ask": [], "deny": []}),
            "$schema": ("https://antigravity.google/schemas/v1/settings.json",),
        }
        return all(
            any(type(setting) is type(safe) and setting == safe for safe in safe_values.get(key, ()))
            for key, setting in value.items()
        )
    # Discovery manifests can pull files from paths beyond the inspected roots.
    return value == {}


def _read_sources(root: Path, files: tuple[str, ...], entries: dict[str, str], prefix: str) -> None:
    for name in files:
        data = _source_bytes(root / name)
        if data is None:
            continue
        if name not in _INSTRUCTIONS and not _safe_config(name, _strict_json(data)):
            raise ValueError("active or unknown customization")
        if len(entries) >= _MAX_CONFIG_ENTRIES:
            raise ValueError("too many customization files")
        entries[f"{prefix}/{name}"] = hashlib.sha256(data).hexdigest()


def _read_custom_dir(path: Path, entries: dict[str, str], prefix: str) -> None:
    _only_known_entries(path, _CONFIG_FILES + _INSTRUCTIONS + _COLLECTIONS)
    for name in _COLLECTIONS:
        _only_known_entries(path / name, ())
    _read_sources(path, _CONFIG_FILES + _INSTRUCTIONS, entries, prefix)


def _workspace_sources(workspace: Path, entries: dict[str, str]) -> None:
    if not _dir_exists(workspace):
        # Admission fingerprints a future projection before it is materialized.
        return
    # Standalone workspaces are scanned only at their own root. In a repository,
    # discovery covers that root and the intervening directories.
    ancestry = (workspace, *workspace.parents)
    levels = ancestry[:1]
    for depth, level in enumerate(ancestry):
        if _plain_stat(level / ".git") is not None:
            levels = ancestry[:depth + 1]
            break
    for index, level in enumerate(levels):
        prefix = f"workspace/{index}"
        _read_sources(level, _INSTRUCTIONS, entries, prefix)
        for name in _CUSTOM_DIRS:
            _read_custom_dir(level / name, entries, f"{prefix}/{name}")


def _fingerprint(env: dict[str, str], roots: tuple[Path, ...], workspace: Path) -> str:
    if not env.get("USERPROFILE") or len(roots) > _MAX_NATIVE_ROOTS:
        raise ValueError("native customization roots unavailable")
    entries: dict[str, str] = {}
    homes = sorted({Path(env[key]) for key in ("USERPROFILE", "HOME") if key in env})
    for home in homes:
        if not _dir_exists(home):
            raise OSError("native home unavailable")
        gemini = home / ".gemini"
        cli = gemini / "antigravity-cli"
        _read_sources(gemini, _INSTRUCTIONS + _CONFIG_FILES, entries, str(gemini))
        _read_sources(cli, _CONFIG_FILES, entries, str(cli))
        for base in (gemini, cli):
            for name in _COLLECTIONS:
                _only_known_entries(base / name, ())
        _read_custom_dir(gemini / "config", entries, str(gemini / "config"))
    for index, root in enumerate(roots):
        _read_custom_dir(root, entries, f"managed/{index}")
    _workspace_sources(workspace, entries)
    templates = {
        role: {
            "settings": hashlib.sha256(_profile_bytes(role)).hexdigest(),
            "args": _arguments(role, "<settings>", "<log>", "<timeout>"),
        }
        for role in _MODES
    }
    document = {"templates": templates, "env": env, "roots": [str(root) for root in roots], "sources": entries}
    return hashlib.sha256(_json_bytes(document)).hexdigest()


def inspect_cli(env: Mapping[str, str], native_config_roots: tuple[Path, ...], workspace: Path) -> Preflight:
    binary = inspect_binary_metadata(env)
    reason = binary.reason
    config_fingerprint = None
    roots = ()
    runtime_env = {}
    if reason == "OK":
        reason = "CUSTOMIZATIONS_BLOCKED"
        try:
            runtime_env = _runtime_environment(env)
            roots = tuple(sorted(set(native_config_roots)))
            config_fingerprint = _fingerprint(runtime_env, roots, workspace)
            if (binary.binary_sha256, binary.version, config_fingerprint) in _REVIEWED_BOOTSTRAPS:
                reason = "OK"
        except (CliPreflightError, OSError, ValueError, RecursionError):
            pass
    return Preflight(
        binary.binary_path, binary.binary_sha256, binary.version, reason, binary.signer,
        binary._identity, config_fingerprint, tuple(sorted(runtime_env.items())), roots,
    )


def validate_prompt(prompt: str) -> bytes:
    if len(prompt) > MAX_PROMPT_BYTES:
        raise CliPreflightError("REQUEST_TOO_LARGE")
    try:
        data = prompt.encode("utf-8")
    except UnicodeEncodeError:
        raise CliPreflightError("INVALID_REQUEST") from None
    if len(data) > MAX_PROMPT_BYTES:
        raise CliPreflightError("REQUEST_TOO_LARGE")
    if not prompt.strip() or "\0" in prompt:
        raise CliPreflightError("INVALID_REQUEST")
    return data


def _check_contract(preflight: Preflight, workspace: Path) -> dict[str, str]:
    if preflight.reason != "OK":
        raise CliPreflightError(preflight.reason)
    pinned = (preflight.binary_sha256, preflight.version, preflight.config_fingerprint)
    if preflight.binary_path is None or pinned not in _REVIEWED_BOOTSTRAPS:
        raise CliPreflightError("CUSTOMIZATIONS_BLOCKED")
    env = dict(preflight._runtime_env)
    try:
        if not preflight._identity or _binary_identity(preflight.binary_path) != preflight._identity:
            raise CliPreflightError("CHECK_REQUIRED")
    except OSError:
        raise CliPreflightError("CHECK_REQUIRED") from None
    try:
        config = _fingerprint(env, preflight._config_roots, workspace)
    except (OSError, ValueError, RecursionError):
        raise CliPreflightError("CUSTOMIZATIONS_BLOCKED") from None
    if config != preflight.config_fingerprint:
        raise CliPreflightError("CHECK_REQUIRED")
    try:
        if not _dir_exists(workspace):
            raise OSError("projection missing")
    except OSError:
        raise CliPreflightError("CUSTOMIZATIONS_BLOCKED") from None
    return env


def _outside_projection(private_root: Path, workspace: Path) -> bool:
    return private_root != workspace and not private_root.is_relative_to(workspace)


def build_launch(
    preflight: Preflight, role: str, prompt: str, workspace: Path, private_root: Path, timeout_s: int,
) -> LaunchSpec:
    data = validate_prompt(prompt)
    if role not in _MODES or type(timeout_s) is not int or not 1 <= timeout_s <= 1800:
        raise CliPreflightError("INVALID_REQUEST")
    env = _check_contract(preflight, workspace)
    binary = inspect_binary_metadata(env)
    if (binary.reason != "OK" or binary.binary_path != preflight.binary_path
            or binary.binary_sha256 != preflight.binary_sha256 or binary.version != preflight.version
            or binary._identity != preflight._identity):
        raise CliPreflightError("CHECK_REQUIRED")
    try:
        if not _outside_projection(private_root, workspace) or not _dir_exists(private_root):
            raise OSError("invalid private directory")
        settings = private_root / _PROFILE_FILE
        log = private_root / _LOG_FILE
        if _plain_path(log) is not None:
            raise OSError("log already exists")
        with settings.open("xb") as stream:
            stream.write(_profile_bytes(role))
    except OSError:
        raise CliPreflightError("CUSTOMIZATIONS_BLOCKED") from None
    launch = LaunchSpec(binary.binary_path, _arguments(role, str(settings), str(log), f"{timeout_s}s"), workspace, env, data)
    revalidate_launch(preflight, launch)
    return launch


def revalidate_launch(preflight: Preflight, launch: LaunchSpec) -> None:
    """Check the prepared contract before creating/resuming the job and while it runs."""
    env = _check_contract(preflight, launch.cwd)
    if launch.executable != preflight.binary_path or launch.env != env:
        raise CliPreflightError("CHECK_REQUIRED")
    roles = {mode: role for role, mode in _MODES.items()}
    args = launch.args
    try:
        role = roles[args[1]]
        settings = Path(args[4])
        timeout = args[10]
        log = settings.parent / _LOG_FILE
        if (not re.fullmatch(r"[1-9][0-9]{0,3}s", timeout) or int(timeout[:-1]) > 1800
                or args != _arguments(role, str(settings), str(log), timeout)
                or settings.name != _PROFILE_FILE or not _outside_projection(settings.parent, launch.cwd)
                or _source_bytes(settings) != _profile_bytes(role)):
            raise ValueError("profile changed")
        log_info = _plain_path(log)
        if log_info is not None and not stat.S_ISREG(log_info.st_mode):
            raise OSError("invalid log")
    except (IndexError, KeyError, ValueError, OSError):
        raise CliPreflightError("CHECK_REQUIRED") from None


@contextmanager
def _windows_read_guard(path: Path):
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    close_handle = kernel.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL
    generic_read, share_read, open_existing, open_reparse_point = 0x80000000, 1, 3, 0x00200000
    handle = create_file(str(path), generic_read, share_read, None, open_existing, open_reparse_point, None)
    if handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        yield
    finally:
        close_handle(handle)


@contextmanager
def guard_launch(preflight: Preflight, launch: LaunchSpec):
    """Pin the verified executable and session profile across Windows job creation and resume."""
    revalidate_launch(preflight, launch)
    with ExitStack() as locks:
        try:
            if os.name == "nt":
                locks.enter_context(_windows_read_guard(launch.executable))
                locks.enter_context(_windows_read_guard(Path(launch.args[4])))
            with launch.executable.open("rb") as stream:
                identity = _identity(os.fstat(stream.fileno()))
                if identity != preflight._identity or hashlib.file_digest(stream, "sha256").hexdigest() != preflight.binary_sha256:
                    raise OSError("binary changed after launch preparation")
        except OSError:
            raise CliPreflightError("CHECK_REQUIRED") from None
        revalidate_launch(preflight, launch)
        yield


_TOOL_NAMES = {
    "command": "shell", "run_command": "shell", "unsandboxed": "shell",
    "read_file": "read", "view_file": "read", "write_file": "write", "write_to_file": "write",
    "replace_file_content": "write", "multi_replace_file_content": "write",
    "mcp": "mcp", "mcp_tool": "mcp", "read_url": "web", "read_url_content": "web",
    "execute_url": "web", "browser_subagent": "web",
}
_TOOL_DENIAL = re.compile(
    r"(?im)^[ \t]*(?:tool[ \t]+(?P<tool>[a-z_]+)[ \t]+"
    r"(?:denied:[ \t]+requires approval in headless mode|(?:denied|blocked) by (?:the )?permission policy)"
    r"|permission denied for (?:tool )?(?P<resource>[a-z_]+)(?=[ \t(:.]|$))\b"
)
_DENIED = re.compile(r"\b(?:permission denied|permission request denied|tool .{0,120}(?:denied|blocked)"
                     r"|requires approval in headless mode|soft[- ]denied)\b", re.IGNORECASE)
_AUTH = re.compile(r"\b(?:authentication required|unauthenticated|login required|not authenticated|"
                   r"sign[- ]in required|invalid_grant|expired credentials)\b", re.IGNORECASE)
_QUOTA = re.compile(r"\b(?:quota (?:is )?(?:exceeded|exhausted)|(?:exceeded|exhausted).{0,80}\bquota|"
                    r"resource_exhausted|rate limit(?:ed| exceeded)|too many requests)\b", re.IGNORECASE)
_ANSI_SGR = re.compile(r"\x1b\[[\d;]*m")


def decode_result(exit_code: int, stdout: bytes, stderr: bytes, overflow: bool) -> CliResult:
    """Return response text only for a clean completion. Export bounded diagnostic metadata."""
    metadata: dict[str, object] = {"exit_code": exit_code}
    if overflow or len(stdout) > MAX_STDOUT_BYTES or len(stderr) > MAX_STDERR_BYTES:
        return CliResult(False, "OUTPUT_OVERFLOW", None, metadata)
    envelope: dict = {}
    diagnostic = ""
    try:
        result = _strict_json(stdout)
        if isinstance(result, dict):
            envelope = result
        diagnostic = _ANSI_SGR.sub("", stderr.decode("utf-8"))
    except (UnicodeError, ValueError, RecursionError):
        # A failure can have only stderr. Its diagnostic still carries a reason.
        try:
            diagnostic = _ANSI_SGR.sub("", stderr.decode("utf-8"))
        except UnicodeError:
            return CliResult(False, "MALFORMED_OUTPUT", None, metadata)
    denied_tools = sorted({
        _TOOL_NAMES[(match["tool"] or match["resource"]).lower()]
        for match in _TOOL_DENIAL.finditer(diagnostic)
        if (match["tool"] or match["resource"]).lower() in _TOOL_NAMES
    })
    if denied_tools:
        metadata.update(deny_observed=True, denied_tools=tuple(denied_tools))
    error = envelope.get("error", "")
    errors = diagnostic
    if envelope.get("status") == "ERROR" and isinstance(error, str):
        errors += "\n" + error
    if denied_tools or _DENIED.search(errors):
        return CliResult(False, "PERMISSION_DENIED", None, metadata)
    if _AUTH.search(errors):
        return CliResult(False, "AUTH_REQUIRED", None, metadata)
    if _QUOTA.search(errors):
        return CliResult(False, "QUOTA_EXCEEDED", None, metadata)
    if exit_code != 0:
        return CliResult(False, "CLI_EXIT_NONZERO", None, metadata)
    response = envelope.get("response")
    if (envelope.get("status") != "SUCCESS" or error or diagnostic.strip()
            or not isinstance(response, str) or not response.strip()):
        return CliResult(False, "MALFORMED_OUTPUT", None, metadata)
    try:
        response.encode("utf-8")
    except UnicodeEncodeError:
        return CliResult(False, "MALFORMED_OUTPUT", None, metadata)
    turns = envelope.get("num_turns")
    if type(turns) is int and 0 <= turns < 2**31:
        metadata["num_turns"] = turns
    duration = envelope.get("duration_seconds")
    if type(duration) in (int, float) and 0 <= duration <= 86400 and math.isfinite(duration):
        metadata["duration_seconds"] = duration
    usage = envelope.get("usage")
    if isinstance(usage, dict):
        metadata["usage"] = {
            key: usage[key] for key in (
                "input_tokens", "output_tokens", "thinking_tokens", "cache_read_tokens", "total_tokens",
            )
            if type(usage.get(key)) is int and 0 <= usage[key] < 2**31
        }
    return CliResult(True, "OK", response, metadata)
