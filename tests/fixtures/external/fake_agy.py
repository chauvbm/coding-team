"""Provider-free stand-in for the Antigravity CLI used by installed-package tests."""
import argparse
import json
import os
from pathlib import Path
import sys
import time
import uuid


def emit(workspace, output_format, response, *, status="SUCCESS", call=None, denied=False):
    if output_format != "stream-json":
        if denied and call is not None:
            print(f"Tool {call[0]} denied: requires approval in headless mode", file=sys.stderr)
        print(json.dumps({"status": status, "response": response}))
        return
    conversation = str(uuid.uuid4())
    events = [{"event": "init", "conversation_id": conversation, "init": {
        "cwd": str(workspace), "tools": ["write_to_file", "view_file"], "permission_mode": "request-review",
    }}]
    if call is not None:
        tool, field, target, action = call
        info = {"name": tool, "parameters": {field: target}}
        steps = [("ACTIVE", info)]
        if denied:
            message = (f'permission check failed for {action} "{target}": Permission denied for '
                       f'{action}({target}). Matches user-configured deny rule.')
            if tool == "search_web":
                message = "tool call denied by pre-tool hook: coding-team worker only runs file tools"
            steps.append(("ERROR", {**info, "error": {"type": "TOOL_ERROR", "message": message}}))
        else:
            steps.append(("DONE", info))
        for state, tool_info in steps:
            events.append({"event": "step_update", "step_update": {
                "conversation_id": conversation, "step_index": 2, "state": state,
                "step_type": "tool", "tool_name": tool, "tool_info": tool_info,
            }})
    events.append({"event": "result", "result": {
        "conversation_id": conversation, "num_turns": 1, "status": status, "response": response,
    }})
    for event in events:
        print(json.dumps(event))


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-format", default="json")
    parser.add_argument("--mcp-inventory", action="store_true")
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
    if args.mcp_inventory:
        print("No MCP servers configured.")
        return 0
    workspace = Path(args.workspace or Path.cwd())
    probe = ""
    if len(workspace.parents) >= 2 and workspace.parents[1].name == "check":
        probe = workspace.parent.name
    call = None
    if probe:
        check = workspace.parents[1]
        outside = str(check / "outside.txt")
        private_canary = check.parent / "private-canary.txt"
        native_canary = next((check / "previous-home" / ".gemini" / "antigravity-cli" / "brain").glob("*.txt"))
        calls = {
            "read": ("read_file", "Path", "input.txt", "read_file"),
            "edit": ("write_file", "Path", "output.txt", "write_file"),
            "deny_read_write": ("write_file", "Path", "blocked.txt", "write_file"),
            "deny_outside_read": ("read_file", "Path", outside, "read_file"),
            "deny_outside_write": ("write_file", "Path", outside, "write_file"),
            "deny_shell": ("run_command", "CommandLine", "echo CODING_TEAM_CANARY", "unsandboxed"),
            "deny_web": ("search_web", "query", "https://example.invalid/", "search_web"),
            "deny_native_read": ("read_file", "Path", str(native_canary), "read_file"),
            "deny_private_read": ("read_file", "Path", str(private_canary), "read_file"),
        }
        call = calls.get(probe)
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
            response = private_canary.read_text(encoding="utf-8").strip()
            emit(workspace, args.output_format, response, call=call, denied=True)
            return 0
        elif diagnostic_kind == "auth":
            diagnostic = "Authentication required"
        elif diagnostic_kind == "quota":
            diagnostic = "Resource exhausted: quota exceeded"
        else:
            diagnostic = "fixture diagnostic"
        print(diagnostic, file=sys.stderr)
        emit(workspace, args.output_format, response)
        return 0
    if mode == "deny" or probe.startswith("deny_"):
        call = call or ("run_command", "CommandLine", "echo FIXTURE", "unsandboxed")
        emit(workspace, args.output_format, "fake canary output", call=call, denied=True)
        return 0
    if mode == "error":
        emit(workspace, args.output_format, "fake failure", status="ERROR")
        return 1

    input_file = workspace / "input.txt"
    content = input_file.read_bytes() if input_file.is_file() else b""
    response = content.decode("utf-8").rstrip("\r\n") if content else "fake canary output"
    declared = set(json.loads(os.environ.get("FAKE_AGY_EDIT_PATHS", "[]")))
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
    if probe == "edit" and "output.txt" in declared:
        (workspace / "output.txt").write_bytes(content)
    response = request.get("response", response)
    emit(workspace, args.output_format, response, call=call)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
