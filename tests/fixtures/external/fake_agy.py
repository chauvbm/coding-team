"""Provider-free stand-in for the Antigravity CLI used by installed-package tests."""
import argparse
import json
import os
from pathlib import Path
import sys
import time


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-format", default="json")
    parser.add_argument("--workspace", default="")
    args, _ = parser.parse_known_args(argv)

    raw_prompt = sys.stdin.buffer.read()
    try:
        prompt = raw_prompt.decode("utf-8")
    except UnicodeError:
        prompt = ""
    try:
        request = json.loads(prompt)
    except (TypeError, ValueError):
        request = {}
    if not isinstance(request, dict):
        request = {}

    marker = request.get("spawn_marker") or os.environ.get("FAKE_AGY_MARKER")
    if marker:
        with open(marker, "a", encoding="utf-8") as stream:
            stream.write("spawn\n")

    barrier = request.get("barrier") or os.environ.get("FAKE_AGY_BARRIER")
    if barrier:
        timeout = request.get("barrier_timeout", os.environ.get("FAKE_AGY_BARRIER_TIMEOUT", "10"))
        deadline = time.monotonic() + float(timeout)
        while not Path(barrier).exists() and time.monotonic() < deadline:
            time.sleep(0.02)

    mode = os.environ.get("FAKE_AGY_MODE", "success")
    probe = ""
    parts = Path.cwd().parts
    if "check" in parts:
        index = parts.index("check")
        if index + 1 < len(parts):
            probe = parts[index + 1]
    denied_tools = {
        "deny_read_write": "write", "deny_outside_read": "read",
        "deny_outside_write": "write", "deny_shell": "shell",
        "deny_mcp": "mcp", "deny_web": "web",
        "deny_native_read": "read", "deny_private_read": "read",
    }
    fixture_config = {}
    try:
        fixture_config = json.loads(Path(__file__).with_suffix(".json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    diagnostic_probe = fixture_config.get("diagnostic_probe")
    diagnostic_kind = fixture_config.get("diagnostic_kind")
    if probe == diagnostic_probe:
        response = "fake canary output"
        if diagnostic_kind == "contradictory_private_read":
            private_canary = Path.cwd().parents[2] / "private-canary.txt"
            response = private_canary.read_text(encoding="utf-8").strip()
            diagnostic = "Tool read_file denied: requires approval in headless mode"
        elif diagnostic_kind == "auth":
            diagnostic = "Authentication required"
        elif diagnostic_kind == "quota":
            diagnostic = "Resource exhausted: quota exceeded"
        else:
            diagnostic = "fixture diagnostic"
        print(diagnostic, file=sys.stderr)
        print(json.dumps({"status": "SUCCESS", "response": response}))
        return 0
    if mode == "deny" or probe in denied_tools:
        tool = denied_tools.get(probe, "shell" if mode == "deny" else "tool")
        tool_names = {"write": "write_file", "read": "read_file", "shell": "run_command",
                      "mcp": "mcp_tool", "web": "read_url"}
        print("Tool " + tool_names.get(tool, tool) + " denied: requires approval in headless mode",
              file=sys.stderr)
        print(json.dumps({"status": "SUCCESS", "response": "fake canary output"}))
        return 0
    if mode == "error":
        print(json.dumps({"status": "ERROR", "response": "fake failure"}))
        return 1

    workspace = Path(args.workspace or Path.cwd())
    input_file = workspace / "input.txt"
    content = input_file.read_bytes() if input_file.is_file() else b""
    response = content.decode("utf-8").rstrip("\r\n") if content else "fake canary output"
    declared = set(request.get("edit_paths", ()))
    fill = request.get("write_byte", "x")
    sizes = request.get("write_sizes", {})
    if not isinstance(sizes, dict) or not isinstance(fill, str) or len(fill) != 1:
        sizes = {}
    sizes = {path: size for path, size in sizes.items()
             if isinstance(path, str) and type(size) is int and 0 <= size <= 4 * 1024 * 1024}
    for relpath, value in request.get("write_files", {}).items():
        if relpath not in declared:
            continue
        target = workspace / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(value.encode("utf-8") if isinstance(value, str) else bytes(value))
    for relpath, size in sizes.items():
        if relpath not in declared or relpath in request.get("write_files", {}):
            continue
        target = workspace / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(fill.encode("utf-8") * size)
    if "edit" in parts and not request.get("write_files"):
        (workspace / "output.txt").write_bytes(content)
    response = request.get("response", response)
    print(json.dumps({"status": "SUCCESS", "response": response}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
