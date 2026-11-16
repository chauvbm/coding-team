"""Build and install the real Codex runtime package with only its backend replaced."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import zipfile


ROOT = Path(__file__).resolve().parents[3]
FAKE_AGY = Path(__file__).with_name("fake_agy.py")


def _load_builder():
    spec = importlib.util.spec_from_file_location("runtime_builder", ROOT / "scripts" / "build_runtime_package.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _adapter_source(fake_agy: Path = FAKE_AGY) -> str:
    """Return an API-compatible test adapter; it never changes production runtime code."""
    fake = str(fake_agy.resolve())
    return '''"""Test-only backend adapter inserted into an isolated runtime package."""
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import sys
from contextlib import contextmanager
import ct_antigravity_production as production_decoder
import time

FAKE_AGY = Path(%r)
FAKE_CONFIG = hashlib.sha256(b"fake-config-v1").hexdigest()

@dataclass(frozen=True)
class BinaryMetadata:
    binary_path: object
    binary_sha256: str
    version: str
    reason: str = "OK"
    signer: str = "fixture"
    _identity: tuple = ()

@dataclass(frozen=True)
class Preflight(BinaryMetadata):
    config_fingerprint: str = FAKE_CONFIG
    _runtime_env: tuple = ()
    _config_roots: tuple = ()

@dataclass(frozen=True)
class LaunchSpec:
    executable: object
    args: tuple
    cwd: object
    env: dict
    stdin_bytes: bytes = b""

@dataclass(frozen=True)
class DecodeResult:
    ok: bool
    reason: str
    response: str = ""
    metadata: dict = None

class CliPreflightError(RuntimeError):
    def __init__(self, reason, message="fixture preflight failed"):
        super().__init__(message)
        self.reason = reason

def inspect_cli(env, native_config_roots, workspace):
    try:
        delay_config = json.loads(FAKE_AGY.with_suffix(".json").read_text(encoding="utf-8"))
        delay_s = delay_config.get("metadata_delay_s", 0)
        if type(delay_s) in (int, float) and 0 < delay_s <= 3:
            time.sleep(delay_s)
    except (OSError, ValueError):
        pass
    digest = hashlib.sha256(FAKE_AGY.read_bytes()).hexdigest()
    fingerprint = hashlib.sha256((FAKE_CONFIG + digest).encode("ascii")).hexdigest()
    path = FAKE_AGY.resolve()
    return Preflight(path, digest, "fake-agy/1", "OK", "fixture",
                     (path.stat().st_size, path.stat().st_mtime_ns), fingerprint, (), tuple(native_config_roots))

def inspect_binary_metadata(env):
    path = FAKE_AGY
    return BinaryMetadata(path.resolve(), hashlib.sha256(path.read_bytes()).hexdigest(), "fake-agy/1", "OK",
                          "fixture", (path.stat().st_size, path.stat().st_mtime_ns))

def build_launch(preflight, role, prompt, workspace, private_root, timeout_s, *, edit_paths=(), mcp_inventory=False):
    if (not production_decoder._outside_projection(private_root, workspace)
            or not production_decoder._dir_exists(private_root)):
        raise CliPreflightError("CUSTOMIZATIONS_BLOCKED")
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("CODEX_", "ANTIGRAVITY_"))
           and k not in {"GEMINI_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"}}
    env["FAKE_AGY_MODE"] = os.environ.get("FAKE_AGY_MODE", "success")
    env["FAKE_AGY_EDIT_PATHS"] = json.dumps(edit_paths)
    for name in ("FAKE_AGY_MARKER", "FAKE_AGY_BARRIER", "FAKE_AGY_BARRIER_TIMEOUT"):
        if name in os.environ: env[name] = os.environ[name]
    args = (str(FAKE_AGY), "--mcp-inventory") if mcp_inventory else (str(FAKE_AGY), "--output-format", "stream-json")
    return LaunchSpec(Path(sys.executable), args, Path(workspace), env, prompt.encode("utf-8"))

def revalidate_launch(preflight, launch):
    current = inspect_cli({}, (), launch.cwd)
    if current.binary_sha256 != preflight.binary_sha256 or current._identity != preflight._identity:
        raise CliPreflightError("CHECK_REQUIRED")
    return None

@contextmanager
def guard_launch(preflight, launch):
    yield

def validate_prompt(prompt):
    if not isinstance(prompt, str) or not prompt.strip() or "\\0" in prompt:
        raise CliPreflightError("INVALID_REQUEST")
    return prompt.encode("utf-8")

decode_stream_result = production_decoder.decode_stream_result
decode_check_result = production_decoder.decode_check_result
decode_mcp_inventory = production_decoder.decode_mcp_inventory
''' % fake


def make_fake_bundle(root, *, home=None, python=None, source_root=ROOT):
    """Return ``(installed_cli, env)`` rooted entirely under a temporary directory."""
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    home = Path(home or root / "home").resolve()
    home.mkdir(parents=True, exist_ok=True)
    python = str(python or sys.executable)
    fake_agy = root / "fake_agy.py"
    shutil.copy2(FAKE_AGY, fake_agy)
    package_dir = root / "package-source"
    if package_dir.exists():
        shutil.rmtree(package_dir)
    shutil.copytree(source_root, package_dir, ignore=shutil.ignore_patterns(".git", "__pycache__", "graphify-out"))

    adapter_path = package_dir / "scripts" / "ct_antigravity.py"
    production_decoder = package_dir / "scripts" / "ct_antigravity_production.py"
    shutil.copy2(adapter_path, production_decoder)
    adapter_path.write_text(_adapter_source(fake_agy), encoding="utf-8")
    archive_path = root / "runtime.zip"
    builder = _load_builder()
    builder.build_package(package_dir, archive_path, platform="codex")
    extracted = root / "extracted"
    extracted.mkdir(exist_ok=True)
    with zipfile.ZipFile(archive_path) as archive:
        archive.extractall(extracted)
    installed_cli = extracted / "coding-team" / "scripts" / "coding_team.py"
    env = {k: v for k, v in os.environ.items()
           if k not in {"CLAUDECODE", "CLAUDE_CONFIG_DIR", "CLAUDE_CODE_SESSION_ID"}
           and not k.startswith(("CODEX_", "ANTIGRAVITY_"))}
    env.update(HOME=str(home), USERPROFILE=str(home), CODEX_HOME=str(home / ".codex"),
               CODEX_THREAD_ID="test-chat", PYTHONIOENCODING="utf-8",
               GIT_CEILING_DIRECTORIES=str(root.parent))
    install = subprocess.run([python, str(installed_cli), "install", "--platform", "codex", "--default", "on"],
                             cwd=str(root), env=env, capture_output=True, text=True, encoding="utf-8", timeout=60)
    if install.returncode != 0:
        raise AssertionError("fixture runtime install failed: %s\n%s" % (install.stdout, install.stderr))
    skill_dir = home / ".agents" / "skills" / "coding-team"
    installed_cli = skill_dir / "scripts" / "coding_team.py"
    return installed_cli, env


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("--python", default=sys.executable)
    args = parser.parse_args()
    cli, _ = make_fake_bundle(args.root, python=args.python)
    print(json.dumps({"cli": str(cli)}))
