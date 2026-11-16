import importlib.util
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
if importlib.util.find_spec("ct_antigravity") is not None:
    import ct_antigravity as agy
else:
    agy = None


class AntigravityCliTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(agy, "Antigravity CLI adapter has not been implemented")
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.home = self.root / "user profile"
        self.home.mkdir()
        self.managed = self.root / "organization"
        self.workspace = self.root / "projection one"
        self.workspace.mkdir()
        self.private = self.root / "private one"
        self.private.mkdir()
        self.binary = self.root / "local app data" / "agy" / "bin" / "agy.exe"
        self.binary.parent.mkdir(parents=True)
        self.binary.write_bytes(b"fake signed binary")
        self.env = {
            "LOCALAPPDATA": str(self.binary.parents[2]),
            "userprofile": str(self.home),
            "TEMP": str(self.private),
            "GEMINI_API_KEY": "gateway-sentinel",
            "OPENAI_API_KEY": "key-sentinel",
            "CODEX_THREAD_ID": "orchestration-sentinel",
            "PATH": "untrusted-path-sentinel",
        }
        verifier = patch.object(agy, "_read_windows_signature", return_value=("Google LLC", "1.5.9"))
        self.signer = verifier.start()
        self.addCleanup(verifier.stop)

    def inspect(self, workspace=None):
        return agy.inspect_cli(self.env, (self.managed,), workspace or self.workspace)

    def qualified(self):
        observed = self.inspect()
        evidence = patch.object(
            agy,
            "_REVIEWED_BOOTSTRAPS",
            ((observed.binary_sha256, observed.version, observed.config_fingerprint),),
        )
        evidence.start()
        self.addCleanup(evidence.stop)
        self.assertEqual(self.inspect().reason, "OK")
        return self.inspect()

    def test_readonly_metadata_reads_signed_binary_without_bootstrap(self):
        with patch.object(agy, "subprocess") as process:
            metadata = agy.inspect_binary_metadata(self.env)
            self.assertEqual((metadata.reason, metadata.version, metadata.signer), ("OK", "1.5.9", "Google LLC"))
            self.assertEqual(metadata.binary_path, self.binary)
            self.assertRegex(metadata.binary_sha256, r"^[a-f0-9]{64}$")
            self.assertFalse(process.run.called)

    def test_google_name_is_required_after_signature_validation(self):
        self.signer.return_value = ("Someone Else", "1.5.9")
        self.assertEqual(agy.inspect_binary_metadata(self.env).reason, "CLI_UNVERIFIED")

    def test_unknown_bootstrap_prevents_any_provider_launch(self):
        with patch.object(agy.subprocess, "run") as process:
            preflight = self.inspect()
            self.assertEqual(preflight.reason, "CUSTOMIZATIONS_BLOCKED")
            with self.assertRaises(agy.CliPreflightError) as error:
                agy.build_launch(preflight, "READ", "sentinel", self.workspace, self.private, 300)
            self.assertEqual(error.exception.reason, "CUSTOMIZATIONS_BLOCKED")
            process.assert_not_called()
        self.assertEqual(list(self.private.iterdir()), [])

    def test_changed_binary_blocks_before_spawn(self):
        preflight = self.qualified()
        self.binary.write_bytes(b"replaced signed binary")
        with self.assertRaises(agy.CliPreflightError) as error:
            agy.build_launch(preflight, "READ", "sentinel", self.workspace, self.private, 300)
        self.assertEqual(error.exception.reason, "CHECK_REQUIRED")
        self.assertEqual(list(self.private.iterdir()), [])

    def test_projection_config_drift_blocks_before_spawn(self):
        preflight = self.qualified()
        custom = self.workspace / ".agent"
        custom.mkdir()
        (custom / "hooks.json").write_text('{"hooks":{"SessionStart":[{}]}}', encoding="utf-8")
        self.assertEqual(self.inspect().reason, "CUSTOMIZATIONS_BLOCKED")
        with self.assertRaises(agy.CliPreflightError) as error:
            agy.build_launch(preflight, "EDIT", "sentinel", self.workspace, self.private, 1)
        self.assertEqual(error.exception.reason, "CUSTOMIZATIONS_BLOCKED")

    def test_personal_customizations_do_not_enter_managed_discovery(self):
        config = self.home / ".gemini" / "config"
        config.mkdir(parents=True)
        hooks = config / "hooks.json"
        hooks.write_text('{"hooks":{}}', encoding="utf-8")
        mcp = config / "mcp_config.json"
        mcp.write_text('{"mcpServers":{"test":{"command":"inert","disabled":true}}}', encoding="utf-8")
        preflight = self.qualified()
        hooks.write_text('{"hooks": {}}', encoding="utf-8")
        observed = self.inspect()
        self.assertEqual(observed.reason, "OK")
        self.assertEqual(observed.config_fingerprint, preflight.config_fingerprint)
        mcp.write_text('{"mcpServers":{"test":{"command":"inert"}}}', encoding="utf-8")
        self.assertEqual(self.inspect().reason, "OK")

    def test_fingerprint_excludes_random_projection(self):
        other = self.root / "projection two"
        other.mkdir()
        self.assertEqual(self.inspect().config_fingerprint, self.inspect(other).config_fingerprint)

    def test_admission_can_fingerprint_a_future_standalone_projection(self):
        future = self.root / "future attempt" / "projection"
        preflight = self.qualified()
        self.assertFalse(future.exists())
        self.assertEqual(self.inspect(future).reason, "OK")
        self.assertEqual(self.inspect(future).config_fingerprint, preflight.config_fingerprint)
        with self.assertRaises(agy.CliPreflightError):
            agy.build_launch(preflight, "READ", "fixture", future, self.private, 30)

    def test_managed_unknown_source_blocks_previously_qualified_profile(self):
        self.qualified()
        self.managed.mkdir()
        (self.managed / "different-startup.json").write_text("{}", encoding="utf-8")
        self.assertEqual(self.inspect().reason, "CUSTOMIZATIONS_BLOCKED")

    def test_parent_discovery_marker_invalidates_projection_contract(self):
        project = self.root / "project"
        nested = project / "nested"
        nested.mkdir(parents=True)
        (project / ".git").mkdir()
        (project / ".agents").mkdir()
        (project / ".agents" / "plugins.json").write_text('{"path":"./fake-plugin"}', encoding="utf-8")
        self.assertIsNone(self.inspect(nested).config_fingerprint)

    def test_managed_projection_rejects_inherited_gemini_redirect(self):
        project = self.root / "external-state"
        nested = project / "attempt" / "projection"
        nested.mkdir(parents=True)
        preflight = self.qualified()
        self.assertEqual(self.inspect(nested).config_fingerprint, preflight.config_fingerprint)
        (project / ".git").mkdir()
        (project / ".gemini").mkdir()
        (project / ".gemini" / "config.json").write_bytes(b"{}")
        self.assertEqual(self.inspect(nested).reason, "CUSTOMIZATIONS_BLOCKED")

    def test_machine_settings_block_preflight_and_launch_revalidation(self):
        common = self.root / "common application data"
        child = self.root / "child program data"
        self.env["PROGRAMDATA"] = str(child)
        paths = [folder / "Antigravity" / "admin_settings.json" for folder in (common, child)]
        with patch.object(agy, "_common_application_data", return_value=common, create=True):
            preflight = self.qualified()
            launch = agy.build_launch(preflight, "READ", "fixture", self.workspace, self.private, 30)
            for path in paths:
                with self.subTest(path=path):
                    path.parent.mkdir(parents=True)
                    path.write_bytes(b"managed settings must be checked only by metadata")
                    self.assertEqual(self.inspect().reason, "CUSTOMIZATIONS_BLOCKED")
                    with self.assertRaises(agy.CliPreflightError):
                        agy.revalidate_launch(preflight, launch)
                    path.unlink()

    def test_changed_tool_guard_binaries_invalidate_setup(self):
        executable = self.root / "isolated-tool-guard.exe"
        executable.write_bytes(b"fixture interpreter")
        with patch.object(agy, "_tool_guard_binaries", return_value=(executable,)):
            preflight = self.qualified()
            launch = agy.build_launch(preflight, "READ", "fixture", self.workspace, self.private, 30)
            executable.write_bytes(b"unexpected interpreter update")
            self.assertEqual(self.inspect().reason, "CUSTOMIZATIONS_BLOCKED")
            with self.assertRaises(agy.CliPreflightError) as error:
                agy.revalidate_launch(preflight, launch)
            self.assertEqual(error.exception.reason, "CHECK_REQUIRED")

    def _check_stream(self, *, tool="write_file", target="blocked.txt", state="ERROR"):
        conversation = "56d3fb81-ab69-412e-8b48-d16b36f0205e"
        path = (self.workspace / target).as_posix()
        def tool_step(step_state):
            info = {"name": tool, "parameters": {"Path": path}}
            if step_state == "ERROR":
                info["error"] = {
                    "type": "TOOL_ERROR",
                    "message": f'permission check failed for {tool} "{path}": Permission denied for '
                               f'{tool}({path}). Matches user-configured deny rule.',
                }
            return {"event": "step_update", "step_update": {
                "conversation_id": conversation, "step_index": 2, "state": step_state,
                "step_type": "tool", "tool_name": tool, "tool_info": info,
            }}
        return [
            {"event": "init", "conversation_id": conversation, "init": {
                "cwd": str(self.workspace), "tools": ["write_to_file", "view_file"],
                "permission_mode": "request-review",
            }},
            tool_step("ACTIVE"), tool_step(state),
            {"event": "result", "result": {
                "conversation_id": conversation, "status": "SUCCESS", "response": "DONE", "num_turns": 1,
            }},
        ]

    def _decode_check(self, records, *, tool="write_file", target="blocked.txt", denied=True, stderr=b""):
        wire = b"".join(json.dumps(record).encode("utf-8") + b"\n" for record in records)
        return agy.decode_check_result(0, wire, stderr, False, workspace=self.workspace, tool=tool,
                                       target=target, denied=denied, marker="private-canary")

    def test_check_links_permission_error_to_the_requested_tool_call(self):
        result = self._decode_check(self._check_stream())
        self.assertTrue(result.ok)
        self.assertTrue(result.metadata["runtime_denial"])
        self.assertIsNone(result.response)
        self.assertNotIn("blocked.txt", repr(result))
        self.assertNotIn("DONE", repr(result))

    def test_check_rejects_incomplete_or_contradictory_permission_evidence(self):
        for problem in ("wrong target", "changed parameters", "wrong conversation", "missing active",
                        "different step", "wrong mode", "wrong cwd", "unknown error", "extra result",
                        "denial with output", "disclosed canary", "unclassified diagnostics"):
            with self.subTest(problem=problem):
                records = self._check_stream(target="elsewhere.txt" if problem == "wrong target" else "blocked.txt")
                step = records[2]["step_update"]
                info = step["tool_info"]
                stderr = b""
                if problem == "changed parameters":
                    info["parameters"]["Unrelated"] = True
                if problem == "wrong conversation":
                    step["conversation_id"] = "another-conversation"
                if problem == "missing active":
                    records.pop(1)
                if problem == "different step":
                    step["step_index"] = 3
                if problem == "wrong mode":
                    records[0]["init"]["permission_mode"] = "always-proceed"
                if problem == "wrong cwd":
                    records[0]["init"]["cwd"] = str(self.root)
                if problem == "unknown error":
                    info["error"]["message"] = "example: permission denied in documentation"
                if problem == "extra result":
                    records.append(records[-1])
                if problem == "denial with output":
                    info["output"] = "unexpected"
                if problem == "disclosed canary":
                    records[-1]["result"]["response"] = "private-canary"
                if problem == "unclassified diagnostics":
                    stderr = b"warning: session settings ignored"
                self.assertFalse(self._decode_check(records, stderr=stderr).ok)

    def test_check_requires_the_positive_tool_step(self):
        records = self._check_stream(tool="read_file", target="input.txt", state="DONE")
        records[-1]["result"]["response"] = "private-canary"
        self.assertTrue(self._decode_check(records, tool="read_file", target="input.txt", denied=False).ok)
        self.assertFalse(self._decode_check([records[0], records[-1]], tool="read_file",
                                           target="input.txt", denied=False).ok)

    def test_stream_reports_a_runtime_denial_even_with_success_and_empty_stderr(self):
        records = self._check_stream()
        records[0]["init"]["model"] = "example-model"
        wire = b"".join(json.dumps(record).encode("utf-8") + b"\n" for record in records)
        result = agy.decode_stream_result(0, wire, b"", False, workspace=self.workspace,
                                         expected_model_id="requested-model")
        self.assertEqual(result.reason, "PERMISSION_DENIED")
        self.assertIsNone(result.response)
        self.assertEqual(result.metadata.get("reported_model_id"), "example-model")

    def test_stream_cannot_publish_success_after_an_unsafe_or_failed_call(self):
        for tool, state, reason in (("run_command", "DONE", "TOOL_POLICY_VIOLATION"),
                                    ("read_file", "ERROR", "CLI_TOOL_ERROR")):
            with self.subTest(tool=tool, state=state):
                records = self._check_stream(tool=tool, state=state)
                records[0]["init"]["model"] = "example-model"
                info = records[2]["step_update"]["tool_info"]
                if state == "ERROR":
                    info["error"] = {"type": "UNRECOGNIZED", "message": "failed"}
                wire = b"".join(json.dumps(record).encode("utf-8") + b"\n" for record in records)
                result = agy.decode_stream_result(0, wire, b"", False, workspace=self.workspace)
                self.assertEqual(result.reason, reason)
                self.assertIsNone(result.response)
                self.assertEqual(result.metadata.get("reported_model_id"), "example-model")

    def test_check_rejects_a_canary_split_between_response_deltas(self):
        records = self._check_stream()
        conversation = records[0]["conversation_id"]
        for state, fragment in (("ACTIVE", "private-"), ("DONE", "canary")):
            records.insert(-1, {"event": "step_update", "step_update": {
                "conversation_id": conversation, "step_index": 3, "state": state,
                "step_type": "agent_response", "text_delta": fragment,
            }})
        self.assertFalse(self._decode_check(records).ok)

    def test_check_can_correlate_the_known_headless_notice_and_tool_error(self):
        target = str(self.root / "outside.txt")
        records = self._check_stream(tool="read_file", target=target)
        path = Path(target).as_posix()
        records[2]["step_update"]["tool_info"]["error"]["message"] = (
            f'permission check failed for read_file "{path}": user denied permission for read_file({path})\n'
            + agy._REFUSAL_FOLLOWUP
        )
        records[-1]["result"]["denied_actions"] = [{"action": "read_file", "display_name": "ReadFile"}]
        records[-1]["result"]["response"] = ""
        diagnostic = (agy._HEADLESS_DENIAL.format(action="read_file") + "\r\n").encode("utf-8")
        result = self._decode_check(records, tool="read_file", target=target, stderr=diagnostic)
        self.assertTrue(result.ok)
        self.assertFalse(self._decode_check(records, tool="read_file", target=target).ok)
        self.assertFalse(self._decode_check(records, tool="read_file", target=target,
                                           stderr=diagnostic + b"unrelated warning\n").ok)

    def test_managed_hook_allows_file_tools_and_blocks_other_names(self):
        spec = agy.build_launch(self.qualified(), "READ", "fixture", self.workspace, self.private, 30)
        files = agy._managed_config(self.private)
        script = self.private / agy._TOOL_GUARD_FILE
        hook = json.loads(files[Path(spec.env["HOME"]) / ".gemini" / "config" / "hooks.json"])
        guard = hook[agy._TOOL_GUARD_NAME]["PreToolUse"][0]
        self.assertEqual(guard["matcher"], "*")
        self.assertEqual(guard["hooks"][0]["command"], agy._tool_guard_command(script))
        for tool, decision in (("read_file", "allow"), ("write_file", "allow"), ("search_web", "deny"),
                               ("call_mcp_tool", "deny"), ("invoke_subagent", "deny"), ([], "deny")):
            with self.subTest(tool=tool):
                result = subprocess.run(
                    [sys.executable, "-I", "-S", "-B", str(script)],
                    input=json.dumps({"toolCall": {"name": tool}}).encode("ascii"),
                    capture_output=True, check=True,
                )
                self.assertEqual(json.loads(result.stdout)["decision"], decision)

    def test_mcp_inventory_qualifies_only_empty_real_inventory_output(self):
        preflight = self.qualified()
        launch = agy.build_launch(preflight, "READ", "", self.workspace, self.private, 30, mcp_inventory=True)
        self.assertEqual(launch.args[-2:], ("mcp", "list"))
        self.assertEqual(launch.stdin_bytes, b"")
        agy.revalidate_launch(preflight, launch)
        absent = agy.decode_mcp_inventory(0, b"No MCP servers configured.\n", b"", False)
        self.assertTrue(absent.ok)
        self.assertTrue(absent.metadata["tool_absence"])
        for output, diagnostic in ((b"fixture_mcp (disabled)\n", b""), (b"No MCP servers configured.\n", b"warning")):
            with self.subTest(output=output, diagnostic=diagnostic):
                self.assertFalse(agy.decode_mcp_inventory(0, output, diagnostic, False).ok)

    def test_guard_execution_failure_cannot_qualify_or_complete_a_job(self):
        records = self._check_stream(tool="search_web", target="query")
        for record in records[1:3]:
            record["step_update"]["tool_info"]["parameters"] = {"query": "fixture"}
        error = records[2]["step_update"]["tool_info"]["error"]
        error["message"] = agy._TOOL_GUARD_ERROR + "command failed"
        wire = b"".join(json.dumps(record).encode("utf-8") + b"\n" for record in records)
        self.assertEqual(agy.decode_stream_result(0, wire, b"", False, workspace=self.workspace).reason,
                         "TOOL_GUARD_FAILED")
        self.assertFalse(self._decode_check(records, tool="search_web", target="fixture").ok)
        error["message"] = agy._TOOL_GUARD_DENIAL
        self.assertTrue(self._decode_check(records, tool="search_web", target="fixture").ok)

    def test_launch_keeps_prompt_off_argv_env_and_private_files(self):
        prompt = "private-prompt-sentinel\nextract-sentinel-Đ"
        spec = agy.build_launch(self.qualified(), "READ", prompt, self.workspace, self.private, 31)
        self.assertEqual(spec.executable, self.binary)
        self.assertEqual(spec.cwd, self.workspace)
        self.assertEqual(spec.stdin_bytes, prompt.encode("utf-8"))
        self.assertIn("--disable-slash-commands", spec.args)
        self.assertNotIn("--mode", spec.args)
        self.assertNotIn("--model", spec.args)
        self.assertEqual(spec.args[spec.args.index("--input-format") + 1], "text")
        self.assertEqual(spec.args[spec.args.index("--output-format") + 1], "stream-json")
        self.assertEqual(spec.args[spec.args.index("--print-timeout") + 1], "31s")
        self.assertNotIn("--dangerously-skip-permissions", spec.args)
        settings = Path(spec.args[spec.args.index("--settings") + 1])
        self.assertEqual(settings.parent, self.private)
        log = Path(spec.args[spec.args.index("--log-file") + 1])
        self.assertEqual(log.parent, self.private)
        profile = json.loads(settings.read_text(encoding="utf-8"))
        self.assertNotIn("agentMode", profile)
        self.assertFalse(profile["allowNonWorkspaceAccess"])
        self.assertEqual(profile["toolPermission"], "request-review")
        self.assertEqual(profile["permissions"]["allow"], [])
        self.assertIn("write_file(*)", profile["permissions"]["deny"])
        for deny in ("command(*)", "mcp(*)", "read_url(*)", "execute_url(*)"):
            self.assertIn(deny, profile["permissions"]["deny"])
        profile_home = Path(spec.env["USERPROFILE"])
        self.assertTrue(profile_home.is_relative_to(self.private))
        self.assertEqual(spec.env["HOME"], str(profile_home))
        self.assertNotEqual(Path(spec.env["LOCALAPPDATA"]), self.binary.parents[2])
        self.assertEqual(Path(spec.args[spec.args.index("--gemini_dir") + 1]), profile_home / ".gemini")
        self.assertTrue(Path(spec.env["TEMP"]).is_relative_to(self.private))

        # A separate harmless process observes only the OS argument/input boundary.
        recorder = (
            "import json, os, sys;"
            "print(json.dumps(dict(args=sys.argv[1:],stdin=sys.stdin.buffer.read().decode('utf-8'),"
            "env=list(os.environ.values()))))"
        )
        captured = subprocess.run(
            [sys.executable, "-c", recorder, *spec.args],
            input=spec.stdin_bytes, cwd=spec.cwd, env=spec.env, capture_output=True, check=True,
        )
        observed = json.loads(captured.stdout)
        self.assertEqual(observed.pop("stdin"), prompt)
        for sentinel in ("private-prompt-sentinel", "extract-sentinel", "gateway-sentinel", "key-sentinel",
                         "orchestration-sentinel", "untrusted-path-sentinel"):
            self.assertNotIn(sentinel, json.dumps(observed) + settings.read_text(encoding="utf-8"))

    def test_model_pin_is_literal_and_cannot_drift(self):
        preflight = self.qualified()
        chosen = "gemini-3.8-flash-low"
        for model in ("--dangerously-skip-permissions", "one two", "invalid\nslug", "x" * 129):
            with self.assertRaises(agy.CliPreflightError) as error:
                agy.build_launch(preflight, "READ", "fixture", self.workspace, self.private, 31, model_id=model)
            self.assertEqual(error.exception.reason, "INVALID_REQUEST")
        spec = agy.build_launch(preflight, "READ", "fixture", self.workspace, self.private, 31, model_id=chosen)
        self.assertEqual(spec.args[-2:], ("--model", chosen))
        self.assertIn("--disable-slash-commands", spec.args)
        with agy.guard_launch(preflight, spec):
            pass
        with self.assertRaises(agy.CliPreflightError) as error:
            agy.revalidate_launch(preflight, replace(spec, args=(*spec.args[:-1], "another-model")))
        self.assertEqual(error.exception.reason, "CHECK_REQUIRED")

    def test_inspections_have_fixed_argv_and_private_empty_cwd(self):
        preflight = self.qualified()
        (self.workspace / "project-data.txt").write_text("caller data", encoding="utf-8")
        for mode in ("models", "usage"):
            private = self.private / mode
            private.mkdir()
            spec = agy.build_launch(preflight, "READ", "", self.workspace, private, 8, inspection=mode)
            self.assertEqual(spec.cwd, private / "inspection")
            self.assertEqual(list(spec.cwd.iterdir()), [])
            self.assertEqual(spec.stdin_bytes, b"")
            self.assertEqual(Path(spec.env["HOME"]), private / "home")
            settings = json.loads(spec.settings_path.read_bytes())
            self.assertFalse(settings["allowNonWorkspaceAccess"])
            self.assertIn("write_file(*)", settings["permissions"]["deny"])
            self.assertEqual(settings["disableSlashCommands"], mode == "models")
            if mode == "models":
                self.assertEqual(spec.args[-1], "models")
                self.assertNotIn("--output-format", spec.args)
            else:
                index = spec.args.index("--print")
                self.assertEqual(spec.args[index:index + 4], ("--print", "/usage", "--output-format", "json"))
                self.assertNotIn("--disable-slash-commands", spec.args)
            with agy.guard_launch(preflight, spec):
                pass
            for tampered in (replace(spec, stdin_bytes=b"/arbitrary"), replace(spec, cwd=self.workspace),
                             replace(spec, args=(*spec.args, "--dangerously-skip-permissions"))):
                with self.assertRaises(agy.CliPreflightError) as error:
                    agy.revalidate_launch(preflight, tampered)
                self.assertEqual(error.exception.reason, "CHECK_REQUIRED")

    def test_inspections_reject_model_requests_and_workspaces_inside_git(self):
        preflight = self.qualified()
        cases = (("READ", "some other prompt", {"inspection": "usage"}),
                 ("EDIT", "", {"inspection": "models"}),
                 ("READ", "", {"inspection": "usage", "mcp_inventory": True}),
                 ("READ", "", {"inspection": "usage", "model_id": "a-model"}),
                 ("READ", "", {"inspection": "usage", "edit_paths": ("target",)}),
                 ("READ", "", {"inspection": "/arbitrary"}))
        for role, prompt, options in cases:
            with self.subTest(options=options), self.assertRaises(agy.CliPreflightError) as error:
                agy.build_launch(preflight, role, prompt, self.workspace, self.private, 8, **options)
            self.assertEqual(error.exception.reason, "INVALID_REQUEST")
        (self.private / ".git").mkdir()
        with self.assertRaises(agy.CliPreflightError) as error:
            agy.build_launch(preflight, "READ", "", self.workspace, self.private, 8, inspection="models")
        self.assertEqual(error.exception.reason, "CUSTOMIZATIONS_BLOCKED")

    def test_model_catalog_decodes_observed_tsv_and_rejects_invalid_or_ambiguous_results(self):
        rows = (b"gemini-3.8-flash-low\tGemini 3.8 Flash (Low)\r\n"
                b"claude-opus-4-6-thinking\tClaude Opus 4.6 (Thinking)\r\n")
        models = agy.decode_model_catalog(0, rows, b"", False)
        self.assertEqual([(m["id"], m["label"]) for m in models],
                         [("gemini-3.8-flash-low", "Gemini 3.8 Flash (Low)"),
                          ("claude-opus-4-6-thinking", "Claude Opus 4.6 (Thinking)")])
        invalid = (b"", b'{"models": []}', rows * 2, rows + b"na\tcontrol\x1b\n",
                   rows + b"-option\tlabel\n", rows + b"model\tlabel\textra\n")
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(agy.CliPreflightError) as error:
                agy.decode_model_catalog(0, value, b"", False)
            self.assertEqual(error.exception.reason, "MODEL_CATALOG_UNAVAILABLE")
        with self.assertRaises(agy.CliPreflightError) as error:
            agy.decode_model_catalog(0, rows, b"", True)
        self.assertEqual(error.exception.reason, "OUTPUT_OVERFLOW")

    def _usage_wire(self, groups, **updates):
        terminal = {"status": "SUCCESS", "num_turns": 0, "duration_seconds": 0,
                    "usage": dict.fromkeys(("input_tokens", "output_tokens", "thinking_tokens",
                                            "cache_read_tokens", "total_tokens"), 0),
                    "command": {"name": "usage", "data": {"groups": groups}}}
        terminal.update(updates)
        return json.dumps(terminal).encode("utf-8")

    def test_quota_maps_exact_labels_or_members_and_only_current_zero_exhausts(self):
        models = [{"id": "alpha-hi", "label": "Example Flash (High)"},
                  {"id": "alpha-low", "label": "Example Flash (Low)"},
                  {"id": "beta", "label": "Example Pro (Thinking)"},
                  {"id": "gamma", "label": "Example Flash 2"},
                  {"id": "unknown", "label": "Unknown (High)"}]
        groups = [
            {"name": "Example", "description": "Models within this group: Example Flash",
             "buckets": [{"id": "alpha-weekly", "window": "weekly", "remaining_fraction": 0.25,
                          "reset_time": "2080-11-22T10:07:30Z"},
                         {"id": "alpha-5h", "window": "5h", "remaining_fraction": 0,
                          "reset_time": "2080-11-16T08:17:47Z"}]},
            {"name": "Other", "model_ids": ["beta"],
             "description": "Models within this group: Example Flash 2, Unknown",
             "buckets": [{"id": "beta-5h", "window": "5h", "remaining_fraction": 0,
                          "reset_time": "2000-11-16T11:42:16Z"},
                         {"id": "beta-weekly", "window": "weekly", "remaining_fraction": 0.5,
                          "reset_time": "2080-11-16T11:42:16Z"}]},
        ]
        report = agy.decode_quota_report(0, self._usage_wire(groups), b"", False, models=models)
        self.assertEqual(report["model_groups"], {"alpha-hi": "Example", "alpha-low": "Example", "beta": "Other"})
        self.assertEqual(report["exhausted_model_ids"], ["alpha-hi", "alpha-low"])
        self.assertEqual(report["groups"][0]["windows"]["weekly"]["remaining_fraction"], 0.25)
        self.assertNotIn("5h", report["groups"][1]["windows"])

    def test_quota_rejects_invalid_diagnostics_and_preserves_auth_reason(self):
        invalid = (self._usage_wire([]), self._usage_wire([], num_turns=1),
                   self._usage_wire([{"name": "Invalid", "buckets": [
                       {"id": "invalid", "window": "weekly", "remaining_fraction": True,
                        "reset_time": "2080-11-16T11:42:16Z"}]}]),
                   b'{"command":{"name":"usage","data":{"groups":[]}}}')
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(agy.CliPreflightError) as error:
                agy.decode_quota_report(0, value, b"", False, models=[])
            self.assertEqual(error.exception.reason, "QUOTA_UNAVAILABLE")
        for stdout, stderr in ((b"", b"authentication required"),
                               (b'{"status":"ERROR","error":"authentication required"}', b"")):
            with self.assertRaises(agy.CliPreflightError) as error:
                agy.decode_quota_report(1, stdout, stderr, False, models=[])
            self.assertEqual(error.exception.reason, "AUTH_REQUIRED")

    def test_quota_never_rounds_a_positive_balance_down_to_exhausted(self):
        groups = [{"name": "Example", "model_ids": ["example-flash"], "buckets": [
            {"id": "tiny", "window": "5h", "remaining_fraction": "TINY",
             "reset_time": "2080-11-16T11:42:16Z"},
            {"id": "known", "window": "weekly", "remaining_fraction": 1,
             "reset_time": "2080-11-16T11:42:16Z"},
        ]}]
        wire = self._usage_wire(groups).replace(b'"TINY"', b"1e-500")
        models = [{"id": "example-flash", "label": "Example Flash"}]
        report = agy.decode_quota_report(0, wire, b"", False, models=models)
        self.assertEqual(report["exhausted_model_ids"], [])
        self.assertNotIn("5h", report["groups"][0]["windows"])

    def test_quota_prefers_explicit_id_then_exact_label_and_preserves_ambiguity(self):
        models = [{"id": "example", "label": "Example"},
                  {"id": "example-ultra", "label": "Example (Ultra)"},
                  {"id": "example-hi", "label": "Example (High)"},
                  {"id": "preferred", "label": "Example"}]
        groups = [
            {"name": "Base", "description": "Models within this group: Example"},
            {"name": "Ultra", "description": "Models within this group: Example (Ultra)"},
            {"name": "Direct", "model_ids": ["preferred"]},
        ]
        for group, fraction in zip(groups, (0, 1, 1)):
            group["buckets"] = [{"id": group["name"].lower(), "window": "weekly", "remaining_fraction": fraction,
                                "reset_time": "2080-11-16T11:42:16Z"}]
        report = agy.decode_quota_report(0, self._usage_wire(groups), b"", False, models=models)
        self.assertEqual(report["model_groups"], {"example": "Base", "example-hi": "Base",
                                                 "example-ultra": "Ultra", "preferred": "Direct"})
        self.assertEqual(report["exhausted_model_ids"], ["example", "example-hi"])
        groups.append({**groups[1], "name": "Conflicting Ultra"})
        report = agy.decode_quota_report(0, self._usage_wire(groups), b"", False, models=models)
        self.assertNotIn("example-ultra", report["model_groups"])
        self.assertNotIn("example-ultra", report["exhausted_model_ids"])

    def test_stream_reports_exact_model_and_refuses_a_verified_mismatch(self):
        records = self._check_stream()[::3]
        def decode(**options):
            wire = b"\n".join(json.dumps(record).encode("utf-8") for record in records)
            return agy.decode_stream_result(0, wire, b"", False, workspace=self.workspace, **options)

        chosen = "gemini-3.8-flash-low"
        unverified = decode(expected_model_id=chosen)
        self.assertTrue(unverified.ok)
        self.assertNotIn("reported_model_id", unverified.metadata)
        for invalid in (None, "", "Example model (High)", {"id": chosen}, "--unsafe"):
            with self.subTest(invalid=invalid):
                records[0]["init"]["model"] = invalid
                malformed = decode(expected_model_id=chosen)
                self.assertEqual((malformed.ok, malformed.reason, malformed.response),
                                 (False, "MALFORMED_OUTPUT", None))
                self.assertNotIn("reported_model_id", malformed.metadata)
        records[0]["init"]["model"] = chosen
        self.assertEqual(decode(expected_model_id=chosen).metadata["reported_model_id"], chosen)
        mismatch = decode(expected_model_id="gemini-3.8-flash-high")
        self.assertEqual((mismatch.ok, mismatch.reason, mismatch.response), (False, "MODEL_MISMATCH", None))
        self.assertEqual(mismatch.metadata["reported_model_id"], chosen)

    def test_stream_runtime_quota_failure_preserves_only_valid_model_evidence(self):
        records = self._check_stream()[::3]
        choice = "example-model"
        records[-1]["result"].update(status="ERROR", response="", error="quota exceeded", num_turns=0)

        def decode():
            wire = b"\n".join(json.dumps(record).encode("utf-8") for record in records)
            result = agy.decode_stream_result(1, wire, b"", False, workspace=self.workspace,
                                             expected_model_id="requested-model")
            self.assertEqual((result.reason, result.response), ("QUOTA_EXCEEDED", None))
            return result.metadata

        self.assertNotIn("reported_model_id", decode())
        records[0]["init"]["model"] = None
        self.assertNotIn("reported_model_id", decode())
        records[0]["init"]["model"] = choice
        self.assertEqual(decode().get("reported_model_id"), choice)
        records[-1]["result"]["conversation_id"] = "78963642-d092-43b1-8d96-45abc0494831"
        self.assertNotIn("reported_model_id", decode())

    def test_runtime_invalid_model_errors_are_not_confused_with_model_prose(self):
        message = 'invalid model selection (--model "does-not-exist" --effort ""): model is not recognized'
        error = json.dumps({"status": "ERROR", "error": message, "num_turns": 0}).encode("utf-8")
        for stdout, stderr in ((error, b""), (b"", message.encode("utf-8"))):
            result = agy.decode_stream_result(1, stdout, stderr, False, workspace=self.workspace)
            self.assertEqual(result.reason, "MODEL_UNAVAILABLE")
        result = agy.decode_result(0, json.dumps({"status": "SUCCESS", "response": message}).encode(), b"", False)
        self.assertTrue(result.ok)

    def test_edit_profile_disallows_permissions_beyond_edit(self):
        edits = ("source/main.py", "docs/notes draft.md")
        spec = agy.build_launch(self.qualified(), "EDIT", "fixture", self.workspace, self.private, 10,
                                edit_paths=edits)
        self.assertNotIn("--mode", spec.args)
        self.assertNotIn("--model", spec.args)
        settings = Path(spec.args[spec.args.index("--settings") + 1])
        profile = json.loads(settings.read_text(encoding="utf-8"))
        self.assertNotIn("agentMode", profile)
        self.assertEqual(profile["permissions"]["allow"], [
            f"write_file({(self.workspace / path).as_posix()})" for path in sorted(edits)
        ])
        self.assertNotIn("write_file(*)", profile["permissions"]["deny"])
        self.assertIn("command(*)", profile["permissions"]["deny"])

    def test_rejects_write_grants_outside_edit_scope(self):
        preflight = self.qualified()
        for role, target in (("READ", "output.txt"), ("EDIT", "../outside.txt"), ("EDIT", "*")):
            with self.subTest(role=role, target=target), self.assertRaises(agy.CliPreflightError) as error:
                agy.build_launch(preflight, role, "fixture", self.workspace, self.private, 10,
                                 edit_paths=(target,))
            self.assertEqual(error.exception.reason, "INVALID_REQUEST")
        self.assertEqual(list(self.private.iterdir()), [])

    def test_poll_revalidates_profile_and_file_identity_without_signing_again(self):
        preflight = self.qualified()
        launch = agy.build_launch(preflight, "READ", "fixture", self.workspace, self.private, 30)
        before = self.signer.call_count
        agy.revalidate_launch(preflight, launch)
        self.assertEqual(self.signer.call_count, before)
        file = Path(launch.args[launch.args.index("--settings") + 1])
        file.write_text("{}", encoding="utf-8")
        with self.assertRaises(agy.CliPreflightError) as error:
            agy.revalidate_launch(preflight, launch)
        self.assertEqual(error.exception.reason, "CHECK_REQUIRED")

    @unittest.skipUnless(os.name == "nt", "Windows sharing protects the spawn/resume interval")
    def test_guard_prevents_binary_and_profile_replacement_only_while_held(self):
        preflight = self.qualified()
        launch = agy.build_launch(preflight, "READ", "fixture", self.workspace, self.private, 30)
        settings = Path(launch.args[launch.args.index("--settings") + 1])
        with agy.guard_launch(preflight, launch):
            for path in (self.binary, settings, *agy._managed_config(self.private)):
                with self.subTest(path=path.name), self.assertRaises(PermissionError):
                    path.replace(path.with_suffix(".replaced"))
        self.binary.replace(self.binary.with_suffix(".replaced"))
        settings.replace(settings.with_suffix(".replaced"))

    def test_private_settings_cannot_be_placed_in_projection(self):
        with self.assertRaises(agy.CliPreflightError) as error:
            agy.build_launch(self.qualified(), "READ", "fixture", self.workspace, self.workspace, 31)
        self.assertEqual(error.exception.reason, "CUSTOMIZATIONS_BLOCKED")

    def test_prompt_limit_applies_to_utf8_bytes(self):
        accepted = "é" * (32 * 1024)
        self.assertEqual(len(agy.validate_prompt(accepted)), 64 * 1024)
        with self.assertRaises(agy.CliPreflightError) as error:
            agy.validate_prompt(accepted + "é")
        self.assertEqual(error.exception.reason, "REQUEST_TOO_LARGE")

    def test_soft_deny_zero_exit_is_failure(self):
        out = b'{"status":"SUCCESS","response":"A helpful summary","conversation_id":"private-sentinel"}'
        stderr = b"Tool run_command denied: requires approval in headless mode"
        result = agy.decode_result(0, out, stderr, False)
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "PERMISSION_DENIED")
        self.assertIsNone(result.response)
        self.assertIs(result.metadata["deny_observed"], True)
        self.assertEqual(result.metadata["denied_tools"], ("shell",))
        self.assertNotIn("private-sentinel", repr(result))

    def test_denial_evidence_requires_a_valid_successful_exit(self):
        denial = b"Tool read_file denied: requires approval in headless mode"
        success = b'{"status":"SUCCESS","response":"File access was blocked"}'
        for code, stdout, stderr, expected in (
            (3, success, denial, "CLI_EXIT_NONZERO"),
            (0, b"not json", denial, "PERMISSION_DENIED"),
            (0, b'{"status":"SUCCESS","response":"\\ud800"}', denial, "PERMISSION_DENIED"),
            (0, b'{"status":"SUCCESS","response":"Blocked","error":false}', denial, "PERMISSION_DENIED"),
            (0, success, denial + b"\nwarning: session settings ignored", "PERMISSION_DENIED"),
            (0, success, denial + b"; warning: session settings ignored", "PERMISSION_DENIED"),
            (0, b'{"status":"ERROR","error":"authentication required"}', denial, "AUTH_REQUIRED"),
            (0, success, denial + b"\nquota exceeded", "QUOTA_EXCEEDED"),
        ):
            with self.subTest(code=code, stdout=stdout, expected=expected):
                result = agy.decode_result(code, stdout, stderr, False)
                self.assertEqual(result.reason, expected)
                self.assertNotIn("deny_observed", result.metadata)
                self.assertNotIn("denied_tools", result.metadata)

    def test_model_prose_is_neither_diagnostic_nor_canary_evidence(self):
        out = b'{"status":"SUCCESS","response":"Tool run_command denied: requires approval in headless mode"}'
        result = agy.decode_result(0, out, b"", False)
        self.assertTrue(result.ok)
        self.assertIn("denied", result.response)
        self.assertIs(result.metadata.get("deny_observed", False), False)

    def test_ambiguous_denial_is_failure_without_canary_evidence(self):
        out = b'{"status":"SUCCESS","response":"summary"}'
        result = agy.decode_result(0, out, b"permission denied", False)
        self.assertEqual(result.reason, "PERMISSION_DENIED")
        self.assertIs(result.metadata.get("deny_observed", False), False)

    def test_private_diagnostics_cannot_supply_a_missing_deny_canary(self):
        result = agy.decode_result(0, b'{"status":"SUCCESS","response":"summary"}',
                                   b"Tool RUN_COMMAND already denied in example documentation", False)
        self.assertFalse(result.ok)
        self.assertNotIn("denied_tools", result.metadata)

    def test_unclassified_stderr_on_apparent_success_fails(self):
        result = agy.decode_result(0, b'{"status":"SUCCESS","response":"summary"}',
                                   b"unclassified warning", False)
        self.assertEqual(result.reason, "MALFORMED_OUTPUT")

    def test_result_reason_and_output_caps(self):
        success = b'{"status":"SUCCESS","response":"summary"}'
        for exit_code, out, err, overflow, reason in (
            (1, b"", b"authentication required", False, "AUTH_REQUIRED"),
            (1, b'{"status":"ERROR","error":"quota exceeded"}', b"", False, "QUOTA_EXCEEDED"),
            (1, success, b"unspecified reason", False, "CLI_EXIT_NONZERO"),
            (0, b"bad json", b"", False, "MALFORMED_OUTPUT"),
            (0, success + success, b"", False, "MALFORMED_OUTPUT"),
            (0, b'{"status":"ERROR","status":"SUCCESS","response":"summary"}', b"", False, "MALFORMED_OUTPUT"),
            (0, b'{"status":"SUCCESS","response":""}', b"", False, "MALFORMED_OUTPUT"),
            (0, b'{"status":"SUCCESS","response":"summary","error":"inconsistent"}', b"", False, "MALFORMED_OUTPUT"),
            (0, success, b"", True, "OUTPUT_OVERFLOW"),
            (0, b"x" * (1024 * 1024 + 1), b"", False, "OUTPUT_OVERFLOW"),
            (0, success, b"x" * (256 * 1024 + 1), False, "OUTPUT_OVERFLOW"),
        ):
            with self.subTest(reason=reason, stderr_bytes=len(err)):
                result = agy.decode_result(exit_code, out, err, overflow)
                self.assertFalse(result.ok)
                self.assertEqual(result.reason, reason)
                self.assertIsNone(result.response)

    def test_success_metadata_drops_unknown_and_credential_fields(self):
        out = b'{"status":"SUCCESS","response":"summary","num_turns":1,"duration_seconds":2.5,"conversation_id":' \
              b'"private-conversation","api_key":"private-secret","usage":{"input_tokens":4,"token":"private-value"}}'
        result = agy.decode_result(0, out, b"", False)
        self.assertTrue(result.ok)
        self.assertEqual(result.metadata["num_turns"], 1)
        self.assertEqual(result.metadata["duration_seconds"], 2.5)
        self.assertEqual(result.metadata["usage"], {"input_tokens": 4})
        self.assertNotIn("private-", repr(result))


if __name__ == "__main__":
    unittest.main()
