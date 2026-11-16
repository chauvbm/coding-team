import importlib.util
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
        with patch.object(agy, "subprocess") as process:
            preflight = self.inspect()
            self.assertEqual(preflight.reason, "CUSTOMIZATIONS_BLOCKED")
            with self.assertRaises(agy.CliPreflightError) as error:
                agy.build_launch(preflight, "READ", "sentinel", self.workspace, self.private, 300)
            self.assertEqual(error.exception.reason, "CUSTOMIZATIONS_BLOCKED")
            self.assertFalse(process.run.called)
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

    def test_known_disabled_sources_are_fingerprinted(self):
        config = self.home / ".gemini" / "config"
        config.mkdir(parents=True)
        hooks = config / "hooks.json"
        hooks.write_text('{"hooks":{}}', encoding="utf-8")
        mcp = config / "mcp_config.json"
        mcp.write_text('{"mcpServers":{"test":{"command":"inert","disabled":true}}}', encoding="utf-8")
        preflight = self.qualified()
        hooks.write_text('{"hooks": {}}', encoding="utf-8")
        observed = self.inspect()
        self.assertEqual(observed.reason, "CUSTOMIZATIONS_BLOCKED")
        self.assertNotEqual(observed.config_fingerprint, preflight.config_fingerprint)
        mcp.write_text('{"mcpServers":{"test":{"command":"inert"}}}', encoding="utf-8")
        self.assertEqual(self.inspect().reason, "CUSTOMIZATIONS_BLOCKED")

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

    def test_launch_keeps_prompt_off_argv_env_and_private_files(self):
        prompt = "private-prompt-sentinel\nextract-sentinel-Đ"
        spec = agy.build_launch(self.qualified(), "READ", prompt, self.workspace, self.private, 31)
        self.assertEqual(spec.executable, self.binary)
        self.assertEqual(spec.cwd, self.workspace)
        self.assertEqual(spec.stdin_bytes, prompt.encode("utf-8"))
        self.assertIn("--disable-slash-commands", spec.args)
        self.assertEqual(spec.args[spec.args.index("--mode") + 1], "plan")
        self.assertEqual(spec.args[spec.args.index("--input-format") + 1], "text")
        self.assertEqual(spec.args[spec.args.index("--output-format") + 1], "json")
        self.assertEqual(spec.args[spec.args.index("--print-timeout") + 1], "31s")
        self.assertNotIn("--dangerously-skip-permissions", spec.args)
        settings = Path(spec.args[spec.args.index("--settings") + 1])
        self.assertEqual(settings.parent, self.private)
        log = Path(spec.args[spec.args.index("--log-file") + 1])
        self.assertEqual(log.parent, self.private)
        profile = json.loads(settings.read_text(encoding="utf-8"))
        self.assertFalse(profile["allowNonWorkspaceAccess"])
        self.assertEqual(profile["toolPermission"], "request-review")
        self.assertEqual(profile["permissions"]["allow"], [])
        self.assertIn("write_file(*)", profile["permissions"]["deny"])
        for deny in ("command(*)", "mcp(*)", "read_url(*)", "execute_url(*)"):
            self.assertIn(deny, profile["permissions"]["deny"])
        self.assertEqual(set(spec.env), {"LOCALAPPDATA", "USERPROFILE", "TEMP"})

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

    def test_edit_profile_disallows_permissions_beyond_edit(self):
        spec = agy.build_launch(self.qualified(), "EDIT", "fixture", self.workspace, self.private, 10)
        self.assertEqual(spec.args[spec.args.index("--mode") + 1], "accept-edits")
        settings = Path(spec.args[spec.args.index("--settings") + 1])
        profile = json.loads(settings.read_text(encoding="utf-8"))
        self.assertNotIn("write_file(*)", profile["permissions"]["deny"])
        self.assertIn("command(*)", profile["permissions"]["deny"])

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
            for path in (self.binary, settings):
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
        out = b'{"status":"SUCCESS","response":"A helpful summary"}'
        stderr = b"Tool run_command denied: requires approval in headless mode; private-sentinel"
        result = agy.decode_result(0, out, stderr, False)
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "PERMISSION_DENIED")
        self.assertIsNone(result.response)
        self.assertIs(result.metadata["deny_observed"], True)
        self.assertEqual(result.metadata["denied_tools"], ("shell",))
        self.assertNotIn("private-sentinel", repr(result))

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
