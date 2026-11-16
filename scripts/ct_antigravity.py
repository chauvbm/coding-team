"""Antigravity CLI preflight, per-session profiles, and headless result decoding."""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager, ExitStack
import ctypes
from ctypes import wintypes
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import sys

from ct_external_workspace import validate_relative_paths


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
_INSPECTION_DIR = "inspection"
_INSPECTIONS = ("models", "usage")
_MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}")
_MAX_MODELS = 256
_MAX_QUOTA_GROUPS = 64
_OUTPUT_PROTOCOL = "stream-json/1"
_TOOL_GUARD_FILE = "tool-guard.py"
_TOOL_GUARD_NAME = "coding-team-tool-guard"
_TOOL_GUARD_REASON = "coding-team worker only runs file tools"
_TOOL_GUARD_PYTHON_FLAGS = ("-I", "-S", "-B")
_TOOL_GUARD_DENIAL = f"tool call denied by pre-tool hook: {_TOOL_GUARD_REASON}"
_TOOL_GUARD_ERROR = f'JSON hook "jsonhook__{_TOOL_GUARD_NAME}_PreToolUse_0_0" failed: '
_FILE_TOOLS = (
    "read_file", "view_file", "write_file", "write_to_file",
    "replace_file_content", "multi_replace_file_content",
)
_WINDOWS_ENV = frozenset({
    "APPDATA", "COMMONPROGRAMFILES", "HOME", "HOMEDRIVE", "HOMEPATH", "LOCALAPPDATA",
    "PROGRAMDATA", "SYSTEMDRIVE", "SYSTEMROOT", "TEMP", "TMP", "USERPROFILE", "WINDIR",
})
_SYSTEM_ENV = frozenset({"COMMONPROGRAMFILES", "PROGRAMDATA", "SYSTEMDRIVE", "SYSTEMROOT", "WINDIR"})
_BOOTSTRAP_FILES = {
    "config/config.json": b'{"plugins":{"swarm":{"enabled":false}}}\n',
    "config/mcp_config.json": b'{"mcpServers":{}}\n',
}
_TOOL_GUARD_SOURCE = (
    "import json\nimport sys\n\n"
    f"file_tools = {_FILE_TOOLS!r}\n"
    """
limit = 16 * 1024 * 1024
data = sys.stdin.buffer.read(limit + 1)
allowed = False
if len(data) <= limit:
    try:
        call = json.loads(data)["toolCall"]["name"]
        allowed = isinstance(call, str) and call in file_tools
    except (KeyError, ValueError, TypeError, RecursionError):
        pass
response = {"decision": "allow"} if allowed else {"decision": "deny", "reason": """
    + repr(_TOOL_GUARD_REASON) + """}
print(json.dumps(response))
""").encode("ascii")

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
_REVIEWED_BOOTSTRAPS: tuple[tuple[str, str, str], ...] = (
    (
        "8803c289cac04d6e53f2211592d9981c2d5760bc0d9b4a7f222ef6ecc7abb38a",
        "1.5.9",
        "c5220089365cc72118bfe7ea0b73b39057b7484955e21c6dfa093cdb286e2899",
    ),
)

_ROLES = ("READ", "EDIT")
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
    role: str
    settings_path: Path
    edit_paths: tuple[str, ...] = ()
    mcp_inventory: bool = False
    model_id: str | None = None
    inspection: str | None = None
    timeout_s: int = 300


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


def _strict_json(raw: bytes, *, parse_float=float) -> object:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    def invalid_constant(_):
        raise ValueError("non-finite JSON constant")

    return json.loads(raw.decode("utf-8"), object_pairs_hook=unique,
                      parse_constant=invalid_constant, parse_float=parse_float)


def _windows_directory() -> Path:
    buffer = ctypes.create_unicode_buffer(32768)
    count = ctypes.windll.kernel32.GetSystemWindowsDirectoryW(buffer, len(buffer))
    if not 0 < count < len(buffer):
        raise OSError("Windows directory unavailable")
    return Path(buffer.value)


def _common_application_data() -> Path | None:
    if os.name != "nt":
        return None
    buffer = ctypes.create_unicode_buffer(260)
    csidl_common_appdata = 0x23
    result = ctypes.windll.shell32.SHGetFolderPathW(None, csidl_common_appdata, None, 0, buffer)
    if result != 0 or not buffer.value:
        raise OSError("Windows common application data unavailable")
    return Path(buffer.value)


def _check_machine_settings(env: Mapping[str, str]) -> None:
    common = _common_application_data()
    roots = {common} if common is not None else set()
    if "PROGRAMDATA" in env:
        roots.add(Path(env["PROGRAMDATA"]))
    for root in roots:
        if _plain_path(root / "Antigravity" / "admin_settings.json") is not None:
            raise OSError("unreviewed machine settings")


def _check_command_processor() -> None:
    if os.name != "nt":
        return
    import winreg

    key = r"Software\Microsoft\Command Processor"
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for view in (winreg.KEY_WOW64_32KEY, winreg.KEY_WOW64_64KEY):
            try:
                with winreg.OpenKey(hive, key, access=winreg.KEY_QUERY_VALUE | view) as handle:
                    value, _ = winreg.QueryValueEx(handle, "AutoRun")
            except FileNotFoundError:
                continue
            if value:
                raise OSError("unreviewed command processor AutoRun")


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


def _profile_bytes(role: str, write_targets: tuple[str, ...] = (), *, disable_slash: bool = True) -> bytes:
    allow = [f"write_file({target})" for target in write_targets]
    deny = list(_COMMON_DENY)
    if role == "READ":
        deny.append("write_file(*)")
    return _json_bytes({
        "allowNonWorkspaceAccess": False,
        "disableSlashCommands": disable_slash,
        "modelProvider": "",
        "permissions": {"allow": allow, "ask": [], "deny": deny},
        "toolPermission": "request-review",
        "useG1Credits": False,
    }) + b"\n"


def _session_profile(role: str, workspace: Path, edit_paths: tuple[str, ...], *, inspection: str | None = None) -> bytes:
    if role != "EDIT" and edit_paths:
        raise ValueError("write paths require an EDIT request")
    targets = tuple((workspace / path).as_posix() for path in validate_relative_paths(edit_paths))
    profile = _profile_bytes(role, targets, disable_slash=inspection != "usage")
    if len(profile) > _MAX_CONFIG_BYTES:
        raise ValueError("session profile exceeds the size limit")
    return profile


def _arguments(settings: Path, log: Path, timeout: str, *, mcp_inventory: bool = False,
               model_id: str | None = None, inspection: str | None = None) -> tuple[str, ...]:
    layout = (
        "--gemini_dir", str(settings.parent / "home" / ".gemini"),
        "--app_data_dir", "antigravity-cli",
    )
    if mcp_inventory:
        return (*layout, "mcp", "list")
    if inspection == "models":
        return (*layout, "models")
    if inspection == "usage":
        return (*layout, "--settings", str(settings), "--print", "/usage",
                "--output-format", "json", "--print-timeout", timeout, "--log-file", str(log))
    ordinary = (
        "--disable-slash-commands", "--settings", str(settings),
        "--input-format", "text", "--output-format", "stream-json", "--print-timeout", timeout,
        "--log-file", str(log), *layout,
    )
    return ordinary if model_id is None else (*ordinary, "--model", model_id)


def _launch_environment(env: dict[str, str], private_root: Path) -> dict[str, str]:
    home = private_root / "home"
    result = {name: value for name, value in env.items() if name in _SYSTEM_ENV}
    result.update(HOME=str(home), USERPROFILE=str(home), HOMEDRIVE=home.drive,
                  HOMEPATH=str(home)[len(home.drive):], APPDATA=str(home / "AppData" / "Roaming"),
                  LOCALAPPDATA=str(home / "AppData" / "Local"), TEMP=str(private_root / "temp"),
                  TMP=str(private_root / "temp"))
    if os.name == "nt":
        result.update(PATH=str(_windows_directory() / "System32"), NoDefaultCurrentDirectoryInExePath="1")
    return result


def _tool_guard_config(command: str) -> bytes:
    hook = {"type": "command", "command": command, "timeout": 5}
    return _json_bytes({_TOOL_GUARD_NAME: {
        "enabled": True, "PreToolUse": [{"matcher": "*", "hooks": [hook]}],
    }}) + b"\n"


def _tool_guard_command(script: Path) -> str:
    interpreter = Path(sys.executable)
    for path in (interpreter, script):
        if not _local_path(path) or any(char in str(path) for char in '"%!?&|^<>()\r\n'):
            raise OSError("invalid command processor path")
    return subprocess.list2cmdline((str(interpreter), *_TOOL_GUARD_PYTHON_FLAGS, str(script)))


def _tool_guard_binaries() -> tuple[Path, ...]:
    if os.name != "nt":
        return ()
    return Path(sys.executable), _windows_directory() / "System32" / "cmd.exe"


def _tool_guard_binary_digests() -> tuple[str, ...]:
    digests = []
    for path in _tool_guard_binaries():
        expected = _binary_identity(path)
        with path.open("rb") as stream:
            if _identity(os.fstat(stream.fileno())) != expected:
                raise OSError("tool guard executable changed")
            digests.append(hashlib.file_digest(stream, "sha256").hexdigest())
            if _identity(os.fstat(stream.fileno())) != expected or _binary_identity(path) != expected:
                raise OSError("tool guard executable changed")
    return tuple(digests)


def _managed_config(private_root: Path) -> dict[Path, bytes]:
    root = private_root / "home" / ".gemini"
    files = {root / path: content for path, content in _BOOTSTRAP_FILES.items()}
    script = private_root / _TOOL_GUARD_FILE
    files[script] = _TOOL_GUARD_SOURCE
    files[root / "config" / "hooks.json"] = _tool_guard_config(_tool_guard_command(script))
    return files


def _verify_managed_config(private_root: Path) -> None:
    home = private_root / "home"
    gemini = home / ".gemini"
    config = _managed_config(private_root)
    for path, content in config.items():
        if _source_bytes(path) != content:
            raise ValueError("managed startup configuration changed")
    for root in (home, gemini, gemini / "config", gemini / "antigravity-cli"):
        for name in _COLLECTIONS:
            _only_known_entries(root / name, ())
        for name in _INSTRUCTIONS + _CONFIG_FILES:
            path = root / name
            if path not in config and _plain_path(path) is not None:
                raise ValueError("unreviewed managed startup source")


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
        if _plain_path(level / ".gemini" / "config.json") is not None:
            raise OSError("unreviewed workspace customization redirect")
        _read_sources(level, _INSTRUCTIONS, entries, prefix)
        for name in _CUSTOM_DIRS:
            _read_custom_dir(level / name, entries, f"{prefix}/{name}")


def _fingerprint(binary: BinaryMetadata, roots: tuple[Path, ...], workspace: Path) -> str:
    if len(roots) > _MAX_NATIVE_ROOTS:
        raise ValueError("native customization roots unavailable")
    entries: dict[str, str] = {}
    for index, root in enumerate(roots):
        _read_custom_dir(root, entries, f"managed/{index}")
    _workspace_sources(workspace, entries)
    settings, log, timeout = Path("<settings>"), Path("<log>"), "<timeout>"
    templates = {
        role: {
            "settings": hashlib.sha256(_profile_bytes(
                role, ("C:/projection/edit-target",) if role == "EDIT" else (),
            )).hexdigest(),
            "args": _arguments(settings, log, timeout),
            "model_args": _arguments(settings, log, timeout, model_id="<model-id>"),
        }
        for role in _ROLES
    }
    inspections = {
        mode: {"args": _arguments(settings, log, timeout, inspection=mode),
               "settings": hashlib.sha256(_profile_bytes("READ", disable_slash=mode != "usage")).hexdigest()}
        for mode in _INSPECTIONS
    }
    document = {"binary": binary.binary_sha256, "version": binary.version,
                "templates": templates, "bootstrap_files": {name: hashlib.sha256(content).hexdigest()
                for name, content in _BOOTSTRAP_FILES.items()}, "system_keys": sorted(_SYSTEM_ENV),
                "roots": [str(root) for root in roots], "sources": entries, "output_protocol": _OUTPUT_PROTOCOL,
                "machine_admin_settings": "absent",
                "tool_guard_sha256": hashlib.sha256(_TOOL_GUARD_SOURCE).hexdigest(),
                "tool_guard_config_sha256": hashlib.sha256(_tool_guard_config(subprocess.list2cmdline(
                    ("<python path>", *_TOOL_GUARD_PYTHON_FLAGS, "<guard path>"),
                ))).hexdigest(),
                "tool_guard_binary_sha256": _tool_guard_binary_digests(),
                "tool_guard_shell": "System32/cmd.exe with AutoRun excluded and no current-directory lookup",
                "mcp_inventory": _arguments(settings, log, "", mcp_inventory=True),
                "inspections": inspections, "inspection_cwd": _INSPECTION_DIR}
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
            _check_machine_settings(runtime_env)
            _check_command_processor()
            roots = tuple(sorted(set(native_config_roots)))
            config_fingerprint = _fingerprint(binary, roots, workspace)
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
        _check_machine_settings(env)
        _check_command_processor()
        config = _fingerprint(preflight, preflight._config_roots, workspace)
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


def _valid_model_id(model_id: object) -> bool:
    return isinstance(model_id, str) and _MODEL_ID.fullmatch(model_id) is not None


def _valid_launch_mode(role, timeout_s, mcp_inventory, model_id, inspection, has_prompt, edit_paths):
    if (role not in _ROLES or type(timeout_s) is not int or not 1 <= timeout_s <= 1800
            or type(mcp_inventory) is not bool
            or (model_id is not None and not _valid_model_id(model_id))
            or (inspection is not None and (inspection not in _INSPECTIONS or mcp_inventory))):
        return False
    if mcp_inventory or inspection is not None:
        return role == "READ" and not has_prompt and not edit_paths and model_id is None
    return True


def _standalone_inspection(workspace: Path) -> None:
    if any(_plain_stat(parent / ".git") is not None for parent in (workspace, *workspace.parents)):
        raise ValueError("inspection cannot use a project directory")
    _only_known_entries(workspace, ())


def build_launch(
    preflight: Preflight, role: str, prompt: str, workspace: Path, private_root: Path, timeout_s: int,
    *, edit_paths: tuple[str, ...] = (), mcp_inventory: bool = False,
    model_id: str | None = None, inspection: str | None = None,
) -> LaunchSpec:
    if not _valid_launch_mode(role, timeout_s, mcp_inventory, model_id, inspection, prompt != "", edit_paths):
        raise CliPreflightError("INVALID_REQUEST")
    data = b"" if mcp_inventory or inspection is not None else validate_prompt(prompt)
    try:
        if not _outside_projection(private_root, workspace) or not _dir_exists(private_root):
            raise OSError("invalid private directory")
        if inspection is not None:
            workspace = private_root / _INSPECTION_DIR
            _standalone_inspection(workspace)
            workspace.mkdir()
    except (OSError, ValueError):
        raise CliPreflightError("CUSTOMIZATIONS_BLOCKED") from None
    env = _check_contract(preflight, workspace)
    try:
        profile = _session_profile(role, workspace, edit_paths, inspection=inspection)
    except ValueError:
        raise CliPreflightError("INVALID_REQUEST") from None
    binary = inspect_binary_metadata(env)
    if (binary.reason != "OK" or binary.binary_path != preflight.binary_path
            or binary.binary_sha256 != preflight.binary_sha256 or binary.version != preflight.version
            or binary._identity != preflight._identity):
        raise CliPreflightError("CHECK_REQUIRED")
    try:
        settings = private_root / _PROFILE_FILE
        log = private_root / _LOG_FILE
        if _plain_path(log) is not None:
            raise OSError("log already exists")
        with settings.open("xb") as stream:
            stream.write(profile)
        child_env = _launch_environment(env, private_root)
        for name in ("APPDATA", "LOCALAPPDATA", "TEMP"):
            Path(child_env[name]).mkdir(parents=True)
        for path, content in _managed_config(private_root).items():
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("xb") as stream:
                stream.write(content)
    except OSError:
        raise CliPreflightError("CUSTOMIZATIONS_BLOCKED") from None
    args = _arguments(settings, log, f"{timeout_s}s", mcp_inventory=mcp_inventory,
                      model_id=model_id, inspection=inspection)
    launch = LaunchSpec(binary.binary_path, args, workspace, child_env, data, role, settings,
                        edit_paths, mcp_inventory, model_id, inspection, timeout_s)
    revalidate_launch(preflight, launch)
    return launch


def revalidate_launch(preflight: Preflight, launch: LaunchSpec) -> None:
    """Check the prepared contract before creating/resuming the job and while it runs."""
    env = _check_contract(preflight, launch.cwd)
    if launch.executable != preflight.binary_path or launch.env != _launch_environment(env, launch.settings_path.parent):
        raise CliPreflightError("CHECK_REQUIRED")
    try:
        settings = launch.settings_path
        log = settings.parent / _LOG_FILE
        if (not _valid_launch_mode(launch.role, launch.timeout_s, launch.mcp_inventory, launch.model_id,
                                  launch.inspection, bool(launch.stdin_bytes), launch.edit_paths)
                or launch.args != _arguments(settings, log, f"{launch.timeout_s}s", mcp_inventory=launch.mcp_inventory,
                                             model_id=launch.model_id, inspection=launch.inspection)
                or settings.name != _PROFILE_FILE or not _outside_projection(settings.parent, launch.cwd)
                or _source_bytes(settings) != _session_profile(launch.role, launch.cwd, launch.edit_paths,
                                                               inspection=launch.inspection)):
            raise ValueError("profile changed")
        if launch.inspection is not None:
            if launch.cwd != settings.parent / _INSPECTION_DIR:
                raise ValueError("inspection work directory changed")
            _standalone_inspection(launch.cwd)
        log_info = _plain_path(log)
        if log_info is not None and not stat.S_ISREG(log_info.st_mode):
            raise OSError("invalid log")
        _verify_managed_config(settings.parent)
    except (ValueError, OSError):
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
    """Hold the executable, profile and managed startup files through the CLI process."""
    revalidate_launch(preflight, launch)
    with ExitStack() as locks:
        try:
            if os.name == "nt":
                locks.enter_context(_windows_read_guard(launch.executable))
                locks.enter_context(_windows_read_guard(launch.settings_path))
                if not launch.mcp_inventory:
                    for path in _tool_guard_binaries():
                        locks.enter_context(_windows_read_guard(path))
                for path in _managed_config(launch.settings_path.parent):
                    locks.enter_context(_windows_read_guard(path))
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
    r"(?:tool[ \t]+(?P<tool>[a-z_]+)[ \t]+"
    r"(?:denied:[ \t]+requires approval in headless mode|(?:denied|blocked) by (?:the )?permission policy)"
    r"|permission denied for (?:tool )?(?P<resource>[a-z_]+))\.?",
    re.IGNORECASE,
)
_DENIED = re.compile(r"\b(?:permission denied|permission request denied|tool .{0,120}(?:denied|blocked)"
                     r"|requires approval in headless mode|soft[- ]denied)\b", re.IGNORECASE)
_AUTH = re.compile(r"\b(?:authentication required|unauthenticated|login required|not authenticated|"
                   r"sign[- ]in required|invalid_grant|expired credentials)\b", re.IGNORECASE)
_QUOTA = re.compile(r"\b(?:quota (?:is )?(?:exceeded|exhausted)|(?:exceeded|exhausted).{0,80}\bquota|"
                    r"resource_exhausted|rate limit(?:ed| exceeded)|too many requests)\b", re.IGNORECASE)
_UNKNOWN_MODEL = re.compile(
    r"^(?:(?:error|agy|jetski):[ \t]*)*(?:invalid model selection \(--model\b|unknown model(?:[ :]|$))",
    re.IGNORECASE | re.MULTILINE,
)
_ANSI_SGR = re.compile(r"\x1b\[[\d;]*m")
_CONVERSATION = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", re.ASCII)
_USAGE_KEYS = ("input_tokens", "output_tokens", "thinking_tokens", "cache_read_tokens", "total_tokens")
_USAGE_WINDOWS = ("5h", "weekly")
_USAGE_GROUP_DESCRIPTION = "Models within this group: "
_RESET_UTC = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
                        r"(?:\.[0-9]{1,9})?(?:Z|\+00:00)")
_EFFORT_SUFFIX = re.compile(r" \((?:none|minimal|low|medium|high|xhigh|max|ultra|thinking)\)$", re.IGNORECASE)
_PERMISSION_ERROR = re.compile(
    r'permission check failed for (?P<action>[a-z_]+) "(?P<target>[^\r\n]+)": (?P<detail>.+)', re.DOTALL,
)
_HEADLESS_DENIAL = (
    'jetski: no output produced \u2014 a tool required the "{action}" permission that headless mode cannot prompt for, '
    'so it was auto-denied. Add an allow-rule under permissions.allow in settings.json (e.g. {action}(<target>)). '
    'Alternatively, re-run with --dangerously-skip-permissions to auto-approve all tools.'
)
_REFUSAL_FOLLOWUP = (
    "Do not attempt to circumvent this denial by rephrasing the command, using alternative tools/scripts "
    "(e.g. python, sh, curl), or accessing the same target resource. Proceed without performing this action."
)
_CHECK_TOOLS = {
    "read_file": ("Path", ("read_file",)),
    "write_file": ("Path", ("write_file",)),
    "run_command": ("CommandLine", ("command", "unsandboxed")),
    "search_web": ("query", ()),
}


def _valid_success_response(envelope, *, allow_empty_response=False):
    response = envelope.get("response")
    if (envelope.get("status") != "SUCCESS" or envelope.get("error", "") != ""
            or not isinstance(response, str) or not (response.strip() or (allow_empty_response and response == ""))):
        return False
    try:
        response.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _runtime_failure(diagnostic: str) -> str | None:
    if _AUTH.search(diagnostic):
        return "AUTH_REQUIRED"
    if _QUOTA.search(diagnostic):
        return "QUOTA_EXCEEDED"
    if _UNKNOWN_MODEL.search(diagnostic):
        return "MODEL_UNAVAILABLE"
    return None


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
    denied_tools = []
    for line in diagnostic.splitlines():
        line = line.strip()
        if not line:
            continue
        match = _TOOL_DENIAL.fullmatch(line)
        name = (match["tool"] or match["resource"]).lower() if match else ""
        denied_tools.append(_TOOL_NAMES.get(name))
    error = envelope.get("error", "")
    errors = diagnostic
    if envelope.get("status") == "ERROR" and isinstance(error, str):
        errors += "\n" + error
    if reason := _runtime_failure(errors):
        return CliResult(False, reason, None, metadata)
    if exit_code != 0:
        return CliResult(False, "CLI_EXIT_NONZERO", None, metadata)
    response = envelope.get("response")
    completed = _valid_success_response(envelope)
    actions = envelope.get("denied_actions", [])
    valid_actions = isinstance(actions, list) and all(
        isinstance(action, dict) and isinstance(action.get("action"), str)
        and bool(re.fullmatch(r"[a-z_]+", action["action"])) for action in actions
    )
    if (valid_actions and actions) or any(denied_tools) or _DENIED.search(errors):
        # Qualifying evidence needs a complete turn and only known runtime denial diagnostics.
        if completed and denied_tools and all(denied_tools):
            metadata.update(deny_observed=True, denied_tools=tuple(sorted(set(denied_tools))))
        return CliResult(False, "PERMISSION_DENIED", None, metadata)
    if not completed or not valid_actions or diagnostic.strip():
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
            key: usage[key] for key in _USAGE_KEYS
            if type(usage.get(key)) is int and 0 <= usage[key] < 2**31
        }
    return CliResult(True, "OK", response, metadata)


def _same_path(value, target: Path, workspace: Path | None = None) -> bool:
    if not isinstance(value, str) or not value or "\0" in value:
        return False
    path = Path(value)
    if not path.is_absolute():
        if workspace is None or path.drive or path.root:
            return False
        path = workspace / path
    return os.path.normcase(os.path.normpath(path)) == os.path.normcase(os.path.normpath(target))


def _stream_call_error(step):
    info = step.get("tool_info")
    error = info.get("error") if isinstance(info, dict) else None
    message = error.get("message") if isinstance(error, dict) and error.get("type") == "TOOL_ERROR" else None
    return message if step.get("state") == "ERROR" and isinstance(message, str) else None


def _read_events(stdout):
    records = [_strict_json(line) for line in stdout.splitlines()]
    if not records or not all(isinstance(record, dict) for record in records):
        raise ValueError("invalid stream")
    result = records[-1]
    if result.get("event") != "result" or not isinstance(result.get("result"), dict):
        raise ValueError("missing terminal event")
    return records, result["result"]


def _validate_init(first, result, workspace):
    init, conversation = first.get("init"), first.get("conversation_id")
    if (first.get("event") != "init" or not isinstance(init, dict)
            or not isinstance(conversation, str) or not _CONVERSATION.fullmatch(conversation)
            or not _same_path(init.get("cwd"), workspace) or init.get("permission_mode") != "request-review"
            or result.get("conversation_id") != conversation):
        raise ValueError("invalid session identity")
    return init


def _validate_events(records, result):
    if type(result.get("num_turns")) is not int or result["num_turns"] != 1:
        raise ValueError("invalid turn count")
    conversation = records[0]["conversation_id"]
    steps = []
    for record in records[1:-1]:
        step = record.get("step_update")
        if (record.get("event") != "step_update" or not isinstance(step, dict)
                or step.get("conversation_id") != conversation or type(step.get("step_index")) is not int
                or step["step_index"] < 0 or step.get("state") not in ("ACTIVE", "DONE", "ERROR")
                or not isinstance(step.get("step_type"), str)):
            raise ValueError("invalid step event")
        steps.append(step)
    return steps


def _stream_result(exit_code, stdout, stderr, overflow, workspace):
    if overflow or len(stdout) > MAX_STDOUT_BYTES or len(stderr) > MAX_STDERR_BYTES:
        return decode_result(exit_code, b"", b"", True), None, None, False
    try:
        records, terminal = _read_events(stdout)
    except (ValueError, RecursionError):
        # An error before session initialization can be a standalone envelope.
        failure = decode_result(exit_code, stdout, stderr, False)
        if failure.reason in ("AUTH_REQUIRED", "QUOTA_EXCEEDED", "MODEL_UNAVAILABLE"):
            return failure, None, None, False
        return decode_result(exit_code, b"", stderr, False), None, None, False
    result = decode_result(exit_code, _json_bytes(terminal), stderr, False)
    try:
        init = _validate_init(records[0], terminal, workspace)
        reported_model = init.get("model")
        if _valid_model_id(reported_model):
            result.metadata["reported_model_id"] = reported_model
        if not result.ok and result.reason != "PERMISSION_DENIED":
            return result, None, None, "model" in init
        steps = _validate_events(records, terminal)
    except ValueError:
        if not result.ok and result.reason != "PERMISSION_DENIED":
            return result, None, None, False
        invalid = CliResult(False, "MALFORMED_OUTPUT", None, {"exit_code": exit_code})
        return invalid, None, None, False
    return result, terminal, steps, "model" in init


def decode_stream_result(exit_code: int, stdout: bytes, stderr: bytes, overflow: bool, *, workspace: Path,
                         expected_model_id: str | None = None) -> CliResult:
    result, _, steps, model_reported = _stream_result(exit_code, stdout, stderr, overflow, workspace)
    if not result.ok:
        return result
    if reason := _ordinary_tool_issue(steps):
        return CliResult(False, reason, None, result.metadata)
    reported_model = result.metadata.get("reported_model_id")
    if expected_model_id is not None and model_reported and reported_model != expected_model_id:
        reason = "MALFORMED_OUTPUT" if reported_model is None else "MODEL_MISMATCH"
        return CliResult(False, reason, None, result.metadata)
    return result


def _ordinary_tool_issue(steps):
    errors = [message for step in steps if step["step_type"] == "tool"
              and (message := _stream_call_error(step)) is not None]
    if any(message.startswith(_TOOL_GUARD_ERROR) for message in errors):
        return "TOOL_GUARD_FAILED"
    if any(message == _TOOL_GUARD_DENIAL or _DENIED.search(message) for message in errors):
        return "PERMISSION_DENIED"
    for step in steps:
        if "subagent_info" in step:
            return "TOOL_POLICY_VIOLATION"
        if step["step_type"] != "tool":
            continue
        if step.get("tool_name") not in _FILE_TOOLS:
            return "TOOL_POLICY_VIOLATION"
        if step["state"] == "ERROR":
            return "CLI_TOOL_ERROR"
        info = step.get("tool_info")
        if (not isinstance(info, dict) or info.get("name") != step["tool_name"]
                or not isinstance(info.get("parameters"), dict) or "error" in info):
            return "MALFORMED_OUTPUT"
    return None


def decode_mcp_inventory(exit_code: int, stdout: bytes, stderr: bytes, overflow: bool) -> CliResult:
    if exit_code == 0 and not overflow and not stderr and stdout.rstrip(b"\r\n") == b"No MCP servers configured.":
        return CliResult(True, "OK", None, {"exit_code": exit_code, "tool_absence": True})
    result = decode_result(exit_code, stdout, stderr, overflow)
    if result.ok or result.reason == "MALFORMED_OUTPUT":
        return CliResult(False, "CHECK_UNPROVEN", None, {"exit_code": exit_code})
    return result


def _inspection_diagnostics(stdout, stderr, overflow, unavailable, *, allowed_stderr=""):
    if overflow or len(stdout) > MAX_STDOUT_BYTES or len(stderr) > MAX_STDERR_BYTES:
        raise CliPreflightError("OUTPUT_OVERFLOW")
    try:
        diagnostic = _ANSI_SGR.sub("", stderr.decode("utf-8")).strip()
    except UnicodeError:
        raise CliPreflightError(unavailable) from None
    if reason := _runtime_failure(diagnostic):
        raise CliPreflightError(reason)
    if diagnostic and diagnostic != allowed_stderr:
        raise CliPreflightError(unavailable)


def _catalog_label(value, limit=160):
    return isinstance(value, str) and 0 < len(value) <= limit and value == value.strip() and value.isprintable()


def decode_model_catalog(exit_code: int, stdout: bytes, stderr: bytes, overflow: bool) -> list[dict[str, str]]:
    """Read the signed CLI's headerless model ID and label TSV."""
    unavailable = "MODEL_CATALOG_UNAVAILABLE"
    _inspection_diagnostics(stdout, stderr, overflow, unavailable, allowed_stderr="Fetching available models...")
    if exit_code != 0:
        raise CliPreflightError(unavailable)
    try:
        lines = stdout.decode("utf-8").replace("\r\n", "\n").removesuffix("\n").split("\n")
    except UnicodeError:
        raise CliPreflightError(unavailable) from None
    if not 1 <= len(lines) <= _MAX_MODELS:
        raise CliPreflightError(unavailable)
    models = []
    ids = set()
    for line in lines:
        parts = line.split("\t")
        if len(parts) != 2:
            raise CliPreflightError(unavailable)
        model_id, label = parts
        if not _valid_model_id(model_id) or model_id in ids or not _catalog_label(label):
            raise CliPreflightError(unavailable)
        ids.add(model_id)
        models.append({"id": model_id, "label": label})
    return models


def _quota_envelope(stdout: bytes):
    terminal = _strict_json(stdout, parse_float=Decimal)
    if not isinstance(terminal, dict):
        raise ValueError("invalid usage terminal")
    error = terminal.get("error", "")
    if terminal.get("status") == "ERROR" and isinstance(error, str) and (reason := _runtime_failure(error)):
        raise CliPreflightError(reason)
    usage, turns, command = terminal.get("usage"), terminal.get("num_turns"), terminal.get("command")
    if (terminal.get("status") != "SUCCESS" or error != "" or type(turns) is not int or turns != 0
            or not isinstance(usage, dict) or not all(key in usage for key in _USAGE_KEYS)
            or any(type(value) is not int or value != 0 for value in usage.values())
            or not isinstance(command, dict) or command.get("name") != "usage"):
        raise ValueError("invalid usage terminal")
    data = command.get("data")
    groups = data.get("groups") if isinstance(data, dict) else None
    if not isinstance(groups, list) or not 1 <= len(groups) <= _MAX_QUOTA_GROUPS:
        raise ValueError("invalid groups")
    return groups


def _quota_windows(buckets, now):
    if not isinstance(buckets, list) or len(buckets) > 16:
        raise ValueError("invalid buckets")
    windows = {}
    seen = set()
    for bucket in buckets:
        if not isinstance(bucket, dict):
            raise ValueError("invalid bucket")
        window = bucket.get("window")
        if window not in _USAGE_WINDOWS:
            continue
        if window in seen or not _valid_model_id(bucket.get("id")):
            raise ValueError("invalid quota window identity")
        seen.add(window)
        fraction, reset = bucket.get("remaining_fraction"), bucket.get("reset_time")
        if fraction is None or reset is None or bucket.get("disabled", False) is not False:
            continue
        if (type(fraction) not in (int, Decimal) or not 0 <= fraction <= 1
                or not isinstance(reset, str) or not _RESET_UTC.fullmatch(reset)):
            raise ValueError("invalid quota window values")
        display_fraction = float(fraction)
        if display_fraction == 0 and fraction != 0:
            continue
        if datetime.fromisoformat(reset) > now:
            windows[window] = {"remaining_fraction": display_fraction, "reset_time": reset}
    return windows


def _quota_members(group):
    for key in ("model_ids", "models"):
        if key not in group:
            continue
        values = group[key]
        if not isinstance(values, list) or len(values) > _MAX_MODELS:
            raise ValueError("invalid model membership")
        ids = [value.get("id") if isinstance(value, dict) and key == "models" else value for value in values]
        if not all(_valid_model_id(value) for value in ids) or len(set(ids)) != len(ids):
            raise ValueError("invalid model membership")
        return True, ids
    description = group.get("description")
    if _catalog_label(description, 1024) and description.startswith(_USAGE_GROUP_DESCRIPTION):
        labels = [label.strip() for label in description.removeprefix(_USAGE_GROUP_DESCRIPTION).split(",")]
        return False, [label for label in labels if _catalog_label(label)]
    return False, []


def decode_quota_report(exit_code: int, stdout: bytes, stderr: bytes, overflow: bool, *, models) -> dict:
    """Return observed quota windows and mappings for this catalog, omitting unknown memberships."""
    unavailable = "QUOTA_UNAVAILABLE"
    _inspection_diagnostics(stdout, stderr, overflow, unavailable)
    groups, windows_by_group, by_id, by_label = [], {}, {}, {}
    now = datetime.now(timezone.utc)
    try:
        observed_groups = _quota_envelope(stdout)
        if exit_code != 0:
            raise ValueError("usage failed")
        for group in observed_groups:
            name = group.get("name") if isinstance(group, dict) else None
            if not _catalog_label(name) or name in windows_by_group:
                raise ValueError("invalid quota group name")
            windows = _quota_windows(group.get("buckets"), now)
            groups.append({"name": name, "windows": windows})
            windows_by_group[name] = windows
            explicit, values = _quota_members(group)
            memberships = by_id if explicit else by_label
            for value in values:
                memberships.setdefault(value, set()).add(name)
    except (ValueError, RecursionError, InvalidOperation):
        raise CliPreflightError(unavailable) from None
    if not any(windows_by_group.values()):
        raise CliPreflightError(unavailable)
    model_groups, exhausted = {}, []
    for model in models:
        model_id, label = model["id"], model["label"]
        matches = by_id.get(model_id) or by_label.get(label) or by_label.get(_EFFORT_SUFFIX.sub("", label), ())
        if len(matches) == 1:
            name, = matches
            model_groups[model_id] = name
            if any(window["remaining_fraction"] == 0 for window in windows_by_group[name].values()):
                exhausted.append(model_id)
    return {"groups": groups, "model_groups": model_groups, "exhausted_model_ids": exhausted}


def _denial_matches(step, actions, matches_target):
    message = _stream_call_error(step)
    match = _PERMISSION_ERROR.fullmatch(message) if message else None
    if match is None or match["action"] not in actions or not matches_target(match["target"]):
        return None
    resource = match["action"] + "(" + match["target"] + ")"
    detail = match["detail"]
    if detail in {
        f"Permission denied for {resource}. Matches user-configured deny rule.",
        f"Permission denied for {resource}. Matches hardcoded system protection boundary rule.",
        f"user denied permission for {resource}\n{_REFUSAL_FOLLOWUP}",
    }:
        return match["action"]
    return None


def _canary_disclosed(marker, stdout, stderr, terminal, steps):
    raw = marker.encode("ascii")
    if raw in stdout or raw in stderr or marker in json.dumps((terminal, steps), ensure_ascii=False):
        return True
    deltas = (step.get("text_delta") for step in steps if step["step_type"] == "agent_response")
    return marker in "".join(delta for delta in deltas if isinstance(delta, str))


def decode_check_result(
    exit_code: int, stdout: bytes, stderr: bytes, overflow: bool, *, workspace: Path,
    tool: str, target: str, denied: bool, marker: str,
) -> CliResult:
    result, terminal, steps, _ = _stream_result(exit_code, stdout, stderr, overflow, workspace)
    if steps is None or (not denied and not result.ok):
        return result
    unproven = CliResult(False, "CHECK_UNPROVEN", None, {"exit_code": exit_code})
    calls = [step for step in steps if step["step_type"] == "tool"]
    if len(calls) != 2 or any("subagent_info" in step for step in steps):
        return unproven
    start, end = calls
    if (start["state"] != "ACTIVE" or end["state"] != ("ERROR" if denied else "DONE")
            or start["step_index"] != end["step_index"]
            or start.get("tool_name") != tool or end.get("tool_name") != tool):
        return unproven

    field, actions = _CHECK_TOOLS[tool]
    before, after = start.get("tool_info"), end.get("tool_info")
    if not isinstance(before, dict) or not isinstance(after, dict):
        return unproven
    params = before.get("parameters")
    if (before.keys() != {"name", "parameters"} or before["name"] != tool
            or after.get("name") != tool or not isinstance(params, dict)
            or params != after.get("parameters")):
        return unproven

    def matches_target(value):
        if field == "Path":
            return _same_path(value, workspace / target, workspace)
        return value == target

    if not matches_target(params.get(field)):
        return unproven
    if not denied:
        return result if "error" not in after else unproven

    guard_denied = tool in ("run_command", "search_web") and _stream_call_error(end) == _TOOL_GUARD_DENIAL
    action = _denial_matches(end, actions, matches_target)
    if (action is None and not guard_denied) or after.keys() != {"name", "parameters", "error"}:
        return unproven
    diagnostic = _ANSI_SGR.sub("", stderr.decode("utf-8")).strip()
    if diagnostic and (action is None or diagnostic != _HEADLESS_DENIAL.format(action=action)):
        return unproven
    reported = terminal.get("denied_actions", [])
    if not isinstance(reported, list) or len(reported) > 1:
        return unproven
    if reported:
        report = reported[0]
        if (action is None or not isinstance(report, dict) or report.keys() != {"action", "display_name"}
                or report["action"] != action or not isinstance(report["display_name"], str)):
            return unproven
    if (not _valid_success_response(terminal, allow_empty_response=bool(reported and diagnostic))
            or _canary_disclosed(marker, stdout, stderr, terminal, steps)):
        return unproven
    return CliResult(True, "OK", None, {"exit_code": exit_code, "runtime_denial": True})
