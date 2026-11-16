"""Installed-package integration tests for the provider-free external runner."""
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from tests.fixtures.external.make_fake_bundle import make_fake_bundle


ROOT = Path(__file__).resolve().parents[1]


class InstalledExternalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.cli, self.env = make_fake_bundle(self.root / "bundle")
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self._git("init", "-q")
        (self.workspace / "input.txt").write_text("safe fixture input\n", encoding="utf-8")
        self._git("add", "input.txt")
        self._git("-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid",
                  "-c", "commit.gpgsign=false", "commit", "-q", "-m", "base")

    def _git(self, *args):
        result = subprocess.run(["git", *args], cwd=self.workspace, env=self.env,
                                capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def cli_call(self, *args, cwd=None, env=None, input_text=None, timeout=45):
        child_env = dict(self.env)
        if env:
            child_env.update(env)
        result = subprocess.run([sys.executable, str(self.cli), *map(str, args)],
                                cwd=str(cwd or self.workspace), env=child_env,
                                input=input_text, capture_output=True, text=True,
                                encoding="utf-8", timeout=timeout)
        try:
            payload = json.loads(result.stdout)
        except ValueError:
            self.fail("installed CLI did not return one JSON object: rc=%r stdout=%r stderr=%r" %
                      (result.returncode, result.stdout, result.stderr))
        return result.returncode, payload

    def control(self, text):
        return self.cli_call("command", text, "--platform", "codex")

    def fake_config(self, **values):
        config_path = self.root / "bundle" / "fake_agy.json"
        config_path.write_text(json.dumps(values), encoding="utf-8")

    def external(self, operation, task_id=None, attempt=1, *, stdin=None, extra=(), env=None, timeout=30):
        args = ["external", operation, "--platform", "codex", "--workspace", str(self.workspace)]
        if task_id is not None:
            args.extend(("--task-id", task_id, "--attempt", str(attempt)))
        args.extend(extra)
        return self.cli_call(*args, input_text=stdin, env=env, timeout=timeout)

    def reserve_selected(self, task, request):
        code, options = self.external("discover", task)
        self.assertEqual(code, 0, options)
        self.assertEqual(options["state"], "MODEL_OPTIONS", options)
        payload = dict(request, catalog_id=options["catalog_id"], model_id="gemini-fixture-low")
        return self.external("reserve", task, stdin=json.dumps(payload), extra=("--request-stdin",))

    def wait_terminal(self, task_id, attempt=1, *, timeout=20):
        import time
        end = time.monotonic() + timeout
        last = None
        while time.monotonic() < end:
            code, last = self.external("status", task_id, attempt)
            self.assertEqual(code, 0, last)
            if last.get("state") in {"COMPLETED", "FAILED", "CANCELED", "TIMED_OUT", "RECONCILE_REQUIRED"}:
                return last
            time.sleep(0.1)
        self.fail("attempt did not reach a terminal/reconcile state: %r" % last)

    def make_ready(self):
        code, enabled = self.control("/coding-team antigravity on")
        self.assertEqual(code, 0, enabled)
        code, ack = self.control("/coding-team antigravity check")
        self.assertEqual(code, 0, ack)
        self.assertTrue(ack.get("acknowledged"), ack)
        completed = self.wait_terminal(ack["task_id"], ack["attempt_no"], timeout=45)
        self.assertEqual(completed["state"], "COMPLETED", completed)
        code, status = self.control("/coding-team antigravity status")
        self.assertEqual(code, 0, status)
        self.assertEqual(status["state"], "READY", status)
        return ack

    def assert_external_shape(self, payload, *, has_attempt=False):
        fields = ["ok", "state", "reason"]
        if has_attempt:
            fields.extend(("task_id", "attempt_no"))
        for field in fields:
            self.assertIn(field, payload, payload)

    def test_discovery_reads_current_catalog_and_quota_before_any_worker(self):
        self.make_ready()
        self.fake_config(gemini_weekly_remaining=0)
        code, options = self.external("discover", "quota-route")
        self.assertEqual(code, 0, options)
        self.assertEqual(options["state"], "MODEL_OPTIONS", options)
        self.assertEqual(set(options["exhausted_model_ids"]), {"gemini-fixture-low", "gemini-fixture-high"})
        prior_catalog = options["catalog_id"]

        self.fake_config(gemini_weekly_remaining=0, claude_five_hour_remaining=0)
        code, options = self.external("discover", "quota-route")
        self.assertEqual(code, 0, options)
        self.assertEqual(options["state"], "NATIVE_REQUIRED", options)
        self.assertEqual(options["reason"], "QUOTA_EXCEEDED", options)
        self.assertNotIn("catalog_id", options)
        code, status = self.external("status")
        self.assertEqual(code, 0, status)
        self.assertEqual(status["state"], "READY", status)
        self.assertNotIn("quota-route", {attempt["task_id"] for attempt in status["attempts"]})

        self.fake_config(models=[["different-account-model", "Another Model"]], quota_unavailable=True)
        code, options = self.external("discover", "quota-route")
        self.assertEqual(code, 0, options)
        self.assertEqual(options["state"], "MODEL_OPTIONS", options)
        self.assertNotEqual(options["quota_status"], "OK", options)
        self.assertNotEqual(options["catalog_id"], prior_catalog)
        self.assertEqual([model["id"] for model in options["models"]], ["different-account-model"])

    def test_reserved_attempt_uses_the_selected_model_from_its_catalog(self):
        self.make_ready()
        task = "selected-model"
        request = {"kind": "READ", "prompt": "Read the fixture.", "base": self._git("rev-parse", "HEAD"),
                   "file_choices": {"input.txt": "GIT"}, "edit_paths": []}
        code, failure = self.external("reserve", task, stdin=json.dumps(request), extra=("--request-stdin",))
        self.assertNotEqual(code, 0, failure)
        self.assertEqual(failure["reason"], "MODEL_SELECTION_REQUIRED", failure)

        code, options = self.external("discover", task)
        self.assertEqual(code, 0, options)
        self.assertEqual(options["state"], "MODEL_OPTIONS", options)
        request.update(catalog_id=options["catalog_id"], model_id="unknown-model")
        code, failure = self.external("reserve", task, stdin=json.dumps(request), extra=("--request-stdin",))
        self.assertNotEqual(code, 0, failure)
        self.assertEqual(failure["reason"], "MODEL_NOT_ELIGIBLE", failure)

        request["model_id"] = "gemini-fixture-low"
        code, reserved = self.external("reserve", task, stdin=json.dumps(request), extra=("--request-stdin",))
        self.assertEqual(code, 0, reserved)
        code, ack = self.external("start", task)
        self.assertEqual(code, 0, ack)
        completed = self.wait_terminal(task)
        self.assertEqual(completed["state"], "COMPLETED", completed)
        code, collected = self.external("collect", task)
        self.assertEqual(code, 0, collected)
        self.assertEqual(collected["result"]["requested_model_id"], "gemini-fixture-low")
        self.assertEqual(collected["result"]["reported_model_id"], "gemini-fixture-low")

    def test_catalog_changes_cannot_combine_quota_or_launch_the_old_selection(self):
        self.make_ready()
        task = "changed-catalog"
        replacement = [["gemini-fixture-low", "Fixture Gemini (Low)"], ["new-id", "New Model"]]
        self.fake_config(catalog_switch_marker=str(self.root / "usage-observed"),
                         models_after_usage=replacement)
        code, discovery = self.external("discover", task)
        self.assertNotEqual(code, 0, discovery)
        self.assertEqual(discovery["reason"], "CATALOG_REFRESH_REQUIRED", discovery)
        self.assertNotIn("catalog_id", discovery)

        self.fake_config(quota_auth_required=True)
        code, discovery = self.external("discover", task)
        self.assertNotEqual(code, 0, discovery)
        self.assertEqual(discovery["reason"], "AUTH_REQUIRED", discovery)
        self.assertNotIn("catalog_id", discovery)

        self.fake_config()
        marker = self.root / "should-not-dispatch"
        request = {"kind": "READ", "prompt": json.dumps({"spawn_marker": str(marker)}),
                   "base": self._git("rev-parse", "HEAD"), "file_choices": {"input.txt": "GIT"},
                   "edit_paths": []}
        code, reserved = self.reserve_selected(task, request)
        self.assertEqual(code, 0, reserved)
        self.fake_config(models=replacement)
        code, result = self.external("start", task)
        self.assertNotEqual(code, 0, result)
        self.assertEqual(result["reason"], "CATALOG_REFRESH_REQUIRED", result)
        self.assertEqual(self.wait_terminal(task)["state"], "CANCELED")
        self.assertFalse(marker.exists())

    def test_discovery_does_not_treat_reset_quota_as_exhausted(self):
        self.make_ready()
        self.fake_config(gemini_weekly_remaining=0, claude_five_hour_remaining=0,
                         catalog_switch_marker=str(self.root / "quota-recheck"), quota_reset_after_seconds=2,
                         post_usage_delay=3)
        code, options = self.external("discover", "quota-reset")
        self.assertEqual(code, 0, options)
        self.assertEqual(options["state"], "MODEL_OPTIONS", options)
        self.assertEqual(options["quota_status"], "QUOTA_UNAVAILABLE", options)

    def test_installed_package_first_use_is_off_and_control_updates_it(self):
        code, status = self.cli_call("command", "/coding-team antigravity status", "--platform", "codex")
        self.assertEqual(code, 0, status)
        self.assert_external_shape(status)
        for field in ("opt_in", "effective_policy", "attempts"):
            self.assertIn(field, status, status)
        self.assertIn(status.get("opt_in"), ("OFF", False), status)

        code, enabled = self.control("/coding-team antigravity ON")
        self.assertEqual(code, 0, enabled)
        self.assertIn(enabled.get("opt_in"), ("ON", True))
        code, status = self.control("/team-coding antigravity STATUS")
        self.assertEqual(code, 0, status)
        self.assertIn(status.get("opt_in"), ("ON", True))

    def test_control_off_and_on_are_idempotent_and_malformed_commands_fail(self):
        for spelling in ("/coding-team antigravity off", "/coding-team antigravity OFF",
                         "/team-coding antigravity off"):
            code, result = self.control(spelling)
            self.assertEqual(code, 0, result)
            self.assertIn(result.get("opt_in"), ("OFF", False), result)
        for spelling in ("/coding-team antigravity on", "/team-coding antigravity ON"):
            code, result = self.control(spelling)
            self.assertEqual(code, 0, result)
            self.assertIn(result.get("opt_in"), ("ON", True), result)
        for spelling in (
            "/coding-team antigravity status # comment",
            "/coding-team antigravity on then check",
            "/coding-team antigravity",
            "Please run /coding-team antigravity on",
        ):
            code, result = self.control(spelling)
            self.assertNotEqual(code, 0, (spelling, result))
            if spelling.lower().startswith(("/coding-team antigravity", "/team-coding antigravity")):
                self.assert_external_shape(result)
                self.assertFalse(result["ok"], (spelling, result))
            else:
                self.assertIn("error", result, result)

    def test_check_requires_opt_in_and_returns_attempt_ack(self):
        code, denied = self.control("/coding-team antigravity check")
        self.assertNotEqual(code, 0, denied)
        self.assert_external_shape(denied, has_attempt=True)
        self.assertIn(denied.get("reason"), ("OPT_IN_REQUIRED", "OFF", "POLICY_OFF"))

        code, _ = self.control("/coding-team antigravity on")
        self.assertEqual(code, 0)
        code, accepted = self.control("/coding-team antigravity check")
        self.assertEqual(code, 0, accepted)
        self.assert_external_shape(accepted, has_attempt=True)
        self.assertTrue(accepted.get("task_id"), accepted)
        self.assertIsInstance(accepted.get("attempt_no"), int)
        self.assertIn(accepted.get("state"), ("STARTING", "RUNNING", "ACK", "CHECKING", "COMPLETED"))
        completed = self.wait_terminal(accepted["task_id"], accepted["attempt_no"], timeout=45)
        self.assertEqual(completed["state"], "COMPLETED", completed)

    def test_check_preserves_auth_and_quota_diagnostics_with_zero_exit(self):
        code, enabled = self.control("/coding-team antigravity on")
        self.assertEqual(code, 0, enabled)
        for kind, expected_reason in (("auth", "AUTH_REQUIRED"), ("quota", "QUOTA_EXCEEDED")):
            with self.subTest(kind=kind):
                self.fake_config(diagnostic_probe="read", diagnostic_kind=kind)
                code, ack = self.control("/coding-team antigravity check")
                self.assertEqual(code, 0, ack)
                failed = self.wait_terminal(ack["task_id"], ack["attempt_no"], timeout=45)
                self.assertEqual(failed["state"], "FAILED", failed)
                code, collected = self.external("collect", ack["task_id"], ack["attempt_no"])
                self.assertEqual(code, 0, collected)
                self.assertEqual(collected["result"]["reason"], expected_reason, collected)
                self.assertEqual(collected["result"].get("exit_code"), 0, collected)

    def test_check_rejects_private_canary_leak_despite_matching_read_deny_marker(self):
        code, enabled = self.control("/coding-team antigravity on")
        self.assertEqual(code, 0, enabled)
        self.fake_config(diagnostic_probe="deny_private_read", diagnostic_kind="contradictory_private_read")
        code, ack = self.control("/coding-team antigravity check")
        self.assertEqual(code, 0, ack)
        failed = self.wait_terminal(ack["task_id"], ack["attempt_no"], timeout=90)
        self.assertEqual(failed["state"], "FAILED", failed)
        code, collected = self.external("collect", ack["task_id"], ack["attempt_no"])
        self.assertEqual(code, 0, collected)
        self.assertEqual(collected["result"]["reason"], "CHECK_UNPROVEN", collected)
        self.assertEqual(collected["result"].get("exit_code"), 0, collected)
        self.assertNotIn("coding-team-canary-", json.dumps(collected), "private canary leaked into durable result")

    def test_reserve_rejects_unapproved_paths_and_preserves_workspace(self):
        check = self.make_ready()
        self.assertEqual(check["attempt_no"], 1, check)
        code, check_record = self.external("collect", check["task_id"], check["attempt_no"])
        self.assertEqual(code, 0, check_record)
        self.assertEqual(check_record["result"]["state"], "COMPLETED", check_record)
        self.assertRegex(check_record["result"].get("binary_sha256") or "", r"^[0-9a-f]{64}$", check_record)
        mcp = next(probe for probe in check_record["result"]["probe_results"] if probe["probe"] == "deny_mcp")
        self.assertTrue(mcp["tool_absence"])
        self.assertFalse(mcp["runtime_denial"])
        code, before = self.control("/coding-team antigravity status")
        self.assertEqual(code, 0, before)
        self.assertEqual(before["state"], "READY", before)
        attempts_before = before["attempts"]
        before = (self.workspace / "input.txt").read_bytes()
        request = {"kind": "READ", "prompt": "read fixture", "base": self._git("rev-parse", "HEAD"),
                   "file_choices": {"../outside.txt": "GIT"}, "edit_paths": []}
        code, result = self.external("reserve", "unsafe", stdin=json.dumps(request),
                                     extra=("--request-stdin",))
        self.assertNotEqual(code, 0, result)
        self.assert_external_shape(result, has_attempt=True)
        self.assertFalse(result["ok"], result)
        self.assertEqual(result.get("reason"), "INVALID_PATH", result)
        self.assertEqual((result.get("task_id"), result.get("attempt_no")), ("unsafe", 1), result)
        code, after = self.control("/coding-team antigravity status")
        self.assertEqual(code, 0, after)
        self.assertEqual(after["state"], "READY", after)
        self.assertEqual(after["attempts"], attempts_before, "invalid traversal unexpectedly reserved an attempt")
        self.assertEqual((self.workspace / "input.txt").read_bytes(), before)

    def test_registered_counterpart_manifest_missing_blocks_admission(self):
        code, enabled = self.control("/coding-team antigravity on")
        self.assertEqual(code, 0, enabled)
        registry = next((Path(self.env["CODEX_HOME"]) / "coding-team").rglob("external-installations.json"))
        document = json.loads(registry.read_text(encoding="utf-8"))
        counterpart = (self.workspace / ".agents" / "skills" / "coding-team" / "install-manifest.json").resolve()
        self.assertFalse(counterpart.exists())
        document["installations"][counterpart.as_posix()] = "project"
        registry.write_text(json.dumps(document), encoding="utf-8")
        code, status = self.control("/coding-team antigravity status")
        self.assertEqual(code, 0, status)
        self.assertEqual(status.get("state"), "NOT_READY", status)
        self.assertEqual(status.get("reason"), "INSTALLATION_INCOMPATIBLE", status)

    def test_cancel_and_off_remove_prepared_projection_copies(self):
        self.make_ready()
        state_root = Path(self.env["CODEX_HOME"]) / "coding-team" / "external" / "v1"

        def reserve(task):
            request = {"kind": "READ", "prompt": "read fixture", "base": self._git("rev-parse", "HEAD"),
                       "file_choices": {"input.txt": "GIT"}, "edit_paths": []}
            code, view = self.reserve_selected(task, request)
            self.assertEqual(code, 0, view)
            attempt_root = state_root / "attempts" / ("task-" + task) / "attempt-1"
            workspace_projection = next(attempt_root.glob("workspace-*"))
            project = workspace_projection / "project"
            base = workspace_projection / "base"
            self.assertTrue(project.exists(), "expected prepared projection before cancellation")
            self.assertTrue(base.exists(), "expected prepared base copy before cancellation")
            return project, base

        canceled_project, canceled_base = reserve("cancel-prepared")
        code, canceled = self.external("cancel", "cancel-prepared")
        self.assertEqual(code, 0, canceled)
        self.assertEqual(canceled["state"], "CANCELED", canceled)
        self.assertFalse(canceled_project.exists(), "cancel left a prepared projection copy")
        self.assertFalse(canceled_base.exists(), "cancel left a prepared base copy")

        off_project, off_base = reserve("off-prepared")
        code, off = self.control("/coding-team antigravity off")
        self.assertEqual(code, 0, off)
        code, status = self.external("status", "off-prepared")
        self.assertEqual(code, 0, status)
        self.assertEqual(status["state"], "CANCELED", status)
        self.assertFalse(off_project.exists(), "OFF left a prepared projection copy")
        self.assertFalse(off_base.exists(), "OFF left a prepared base copy")

    def test_slow_metadata_preflight_does_not_cancel_running_attempt(self):
        self.make_ready()
        task = "slow-metadata-active"
        barrier, marker = self.root / "slow-metadata-barrier", self.root / "slow-metadata-spawn"
        request = {"kind": "READ", "prompt": json.dumps({"barrier": str(barrier),
                                                              "spawn_marker": str(marker),
                                                              "barrier_timeout": 30}),
                   "base": self._git("rev-parse", "HEAD"), "file_choices": {"input.txt": "GIT"},
                   "edit_paths": []}
        code, reserved = self.reserve_selected(task, request)
        self.assertEqual(code, 0, reserved)
        code, ack = self.external("start", task)
        self.assertEqual(code, 0, ack)
        try:
            import time
            deadline = time.monotonic() + 5
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(marker.exists(), "running backend did not reach its barrier")
            self.fake_config(metadata_delay_s=1.0)
            code, other = self.external("discover", "slow-probe", timeout=15)
            self.assertNotEqual(code, 0, other)
            self.assertIn(other.get("reason"), ("BUSY", "ACTIVE_ATTEMPT", "SLOT_BUSY"), other)
            code, still_active = self.external("status", task)
            self.assertEqual(code, 0, still_active)
            self.assertIn(still_active.get("state"), ("RUNNING", "STARTING"), still_active)
        finally:
            self.fake_config()
            barrier.touch()
        completed = self.wait_terminal(task, timeout=30)
        self.assertEqual(completed["state"], "COMPLETED", completed)

    def test_success_without_edit_diff_preserves_zero_exit_code(self):
        self.make_ready()
        task = "missing-diff"
        request = {"kind": "EDIT", "prompt": json.dumps({"response": "No edits are needed."}),
                   "base": self._git("rev-parse", "HEAD"), "file_choices": {"input.txt": "GIT"},
                   "edit_paths": ["input.txt"]}
        code, reserved = self.reserve_selected(task, request)
        self.assertEqual(code, 0, reserved)
        code, ack = self.external("start", task)
        self.assertEqual(code, 0, ack)
        result = self.wait_terminal(task)
        self.assertEqual(result["state"], "FAILED", result)
        code, collected = self.external("collect", task)
        self.assertEqual(code, 0, collected)
        self.assertEqual(collected["result"]["reason"], "DIFF_MISSING", collected)
        self.assertEqual(collected["result"].get("exit_code"), 0, collected)
        self.assertNotIn("artifact_sha", collected["result"], collected)

    def test_orphan_frozen_result_is_not_published_after_sqlite_abort(self):
        self.make_ready()
        task = "orphan-artifact"
        marker = self.root / "orphan-artifact-spawn-marker"
        request = {"kind": "READ", "prompt": json.dumps({"spawn_marker": str(marker)}),
                   "base": self._git("rev-parse", "HEAD"), "file_choices": {"input.txt": "GIT"},
                   "edit_paths": []}
        code, reserved = self.reserve_selected(task, request)
        self.assertEqual(code, 0, reserved)
        state_root = Path(self.env["CODEX_HOME"]) / "coding-team" / "external" / "v1"
        db_path = state_root / "state.sqlite3"
        connection = sqlite3.connect(db_path)
        try:
            connection.execute(
                "CREATE TRIGGER test_abort_result_publish BEFORE UPDATE OF result_id ON attempts "
                "WHEN NEW.task_id='orphan-artifact' AND NEW.result_id IS NOT NULL "
                "BEGIN SELECT RAISE(ABORT, 'injected result publication abort'); END"
            )
            connection.commit()
        finally:
            connection.close()
        try:
            code, ack = self.external("start", task)
            self.assertEqual(code, 0, ack)
            import time
            attempt_root = state_root / "attempts" / ("task-" + task) / "attempt-1"
            deadline = time.monotonic() + 20
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(marker.exists(), "fake CLI did not start")
            deadline = time.monotonic() + 20
            while not (attempt_root / "result").exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue((attempt_root / "result").exists(), "fake read artifact was not frozen")
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                code, interim = self.external("collect", task)
                self.assertEqual(code, 0, interim)
                if interim.get("result_id") is None and interim.get("result") is None:
                    break
                time.sleep(0.1)
            else:
                self.fail("injected publication abort did not leave an unpublished attempt: %r" % interim)
            self.assertIsNone(interim["result_id"], interim)
            self.assertIsNone(interim["result"], interim)
            code, active = self.external("status")
            self.assertEqual(code, 0, active)
            self.assertNotEqual(active.get("state"), "READY", "publication failure released its active slot")
        finally:
            connection = sqlite3.connect(db_path)
            try:
                connection.execute("DROP TRIGGER IF EXISTS test_abort_result_publish")
                connection.commit()
            finally:
                connection.close()

        code, recovered = self.external("reconcile", task, extra=("--target", "process"))
        self.assertEqual(code, 0, recovered)
        code, final = self.external("collect", task)
        self.assertEqual(code, 0, final)
        self.assertIn(final["state"], ("FAILED", "CANCELED", "RECONCILE_REQUIRED"), final)
        self.assertNotIn("artifact_sha", final.get("result") or {}, final)
        self.assertFalse((attempt_root / "result").exists(), "orphan frozen result remained after reconciliation")

    def test_installed_check_read_and_edit_use_detached_fake_runtime(self):
        self.make_ready()

        read_task = "barrier-read"
        barrier = self.root / "read-release-barrier"
        marker = self.root / "read-spawn-marker"
        read_request = {"kind": "READ", "prompt": json.dumps({"barrier": str(barrier),
                                                                  "spawn_marker": str(marker),
                                                                  "barrier_timeout": 15}),
                        "base": self._git("rev-parse", "HEAD"),
                        "file_choices": {"input.txt": "GIT"}, "edit_paths": []}
        code, reserved = self.reserve_selected(read_task, read_request)
        self.assertEqual(code, 0, reserved)
        code, ack = self.external("start", read_task, timeout=20)
        self.assertEqual(code, 0, ack)
        self.assertTrue(ack.get("acknowledged"), ack)
        self.assertIn(ack.get("state"), ("STARTING", "RUNNING"), ack)
        import time
        try:
            deadline = time.monotonic() + 5
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(marker.exists(), "detached fake backend never reached its barrier")
            self.assertFalse(barrier.exists(), "barrier was released before the start ACK")
        finally:
            # Always release the worker on an assertion failure so temp cleanup cannot race it.
            barrier.touch()
        completed = self.wait_terminal(read_task)
        self.assertEqual(completed["state"], "COMPLETED", completed)

        code, grant = self.external("grant-read", read_task)
        self.assertEqual(code, 0, grant)
        code, collected = self.external("collect", read_task, stdin=grant["read_grant"],
                                        extra=("--read-grant-stdin", "--wait-seconds", "1"))
        self.assertEqual(code, 0, collected)
        self.assertEqual(collected["state"], "COMPLETED", collected)
        self.assertNotIn(grant["read_grant"], json.dumps(collected), "read grant leaked into collected response")
        self.assertEqual(collected["result"]["summary"], "safe fixture input", collected)
        result_id = collected.get("result_id")
        self.assertRegex(result_id or "", r"^[0-9a-f]{64}$", collected)
        code, invalid_receipt = self.external(
            "collect", read_task, stdin=grant["read_grant"],
            extra=("--read-grant-stdin", "--wait-seconds", "1", "--record-receipt", result_id))
        self.assertNotEqual(code, 0, invalid_receipt)
        self.assertEqual(invalid_receipt.get("reason"), "INVALID_EXTERNAL_COMMAND", invalid_receipt)
        code, receipt = self.external("collect", read_task, extra=("--record-receipt", result_id))
        self.assertEqual(code, 0, receipt)
        self.assertIsNotNone(receipt["timestamps"]["received_at"], receipt)

        edit_task = "fake-edit"
        edit_paths = ["output.txt"]
        edit_request = {"kind": "EDIT", "prompt": json.dumps({"edit_paths": edit_paths,
                                                                   "write_files": {"output.txt": "fixture edit\n"}}),
                        "base": self._git("rev-parse", "HEAD"),
                        "file_choices": {"input.txt": "GIT"}, "edit_paths": edit_paths}
        code, reserved = self.reserve_selected(edit_task, edit_request)
        self.assertEqual(code, 0, reserved)
        code, ack = self.external("start", edit_task)
        self.assertEqual(code, 0, ack)
        completed = self.wait_terminal(edit_task)
        self.assertEqual(completed["state"], "COMPLETED", completed)
        self.assertEqual((self.workspace / "input.txt").read_text(encoding="utf-8"), "safe fixture input\n")
        self.assertFalse((self.workspace / "output.txt").exists(), "worker wrote into source worktree")

    def test_off_during_detached_work_stops_the_attempt(self):
        self.make_ready()
        task = "off-active"
        barrier, marker = self.root / "cancel-barrier", self.root / "cancel-spawn-marker"
        request = {"kind": "READ", "prompt": json.dumps({"barrier": str(barrier),
                                                              "spawn_marker": str(marker),
                                                              "barrier_timeout": 20}),
                   "base": self._git("rev-parse", "HEAD"), "file_choices": {"input.txt": "GIT"},
                   "edit_paths": []}
        code, reserved = self.reserve_selected(task, request)
        self.assertEqual(code, 0, reserved)
        code, ack = self.external("start", task)
        self.assertEqual(code, 0, ack)
        self.assertTrue(ack.get("acknowledged"), ack)
        try:
            import time
            end = time.monotonic() + 5
            while not marker.exists() and time.monotonic() < end:
                time.sleep(0.02)
            self.assertTrue(marker.exists(), "worker did not reach cancellation barrier")
            code, off = self.control("/coding-team antigravity off")
            self.assertEqual(code, 0, off)
            code, back_on = self.control("/coding-team antigravity on")
            self.assertEqual(code, 0, back_on)
            self.assertIn(back_on.get("opt_in"), ("ON", True), back_on)
        finally:
            barrier.touch()
        result = self.wait_terminal(task, timeout=20)
        self.assertEqual(result["state"], "CANCELED", result)
        self.assertRegex(result.get("result_id") or "", r"^[0-9a-f]{64}$", result)
        self.assertEqual(result["result"]["state"], "CANCELED", result)
        self.assertEqual(result["result"]["reason"], "OPT_IN_OFF", result)
        timestamps = result["timestamps"]
        for field in ("reserved_at", "started_at", "stopped_at", "finished_at"):
            self.assertIsNotNone(timestamps[field], result)
        self.assertIsNone(timestamps["received_at"], result)
        self.assertNotIn("summary", json.dumps(result))
        self.assertNotIn("artifact_sha", json.dumps(result))

    def test_changed_fake_binary_after_reserve_blocks_before_spawn(self):
        self.make_ready()
        task = "changed-binary"
        marker = self.root / "changed-binary-spawn-marker"
        request = {"kind": "READ", "prompt": json.dumps({"spawn_marker": str(marker)}),
                   "base": self._git("rev-parse", "HEAD"),
                   "file_choices": {"input.txt": "GIT"}, "edit_paths": []}
        code, reserved = self.reserve_selected(task, request)
        self.assertEqual(code, 0, reserved)
        fake_binary = self.root / "bundle" / "fake_agy.py"
        fake_binary.write_bytes(fake_binary.read_bytes() + b"\n# changed after CHECK and reserve\n")
        code, blocked = self.external("start", task)
        self.assertNotEqual(code, 0, blocked)
        self.assertEqual(blocked["reason"], "PROFILE_CHANGED", blocked)
        result = self.wait_terminal(task)
        self.assertEqual(result["state"], "CANCELED", result)
        self.assertEqual(result["reason"], "PROFILE_CHANGED", result)
        self.assertFalse(marker.exists(), "changed fake binary was spawned")

    def test_partial_apply_is_reconciled_before_edit_scope_is_released(self):
        self.make_ready()
        task = "partial-apply"
        edit_paths = ["00-first.bin", "10-next.bin", "20-next.bin", "30-next.bin", "40-next.bin"]
        write_sizes = {edit_paths[0]: 4 * 1024 * 1024,
                       edit_paths[1]: 64 * 1024, edit_paths[2]: 64 * 1024,
                       edit_paths[3]: 64 * 1024, edit_paths[4]: 64 * 1024}
        payload = {"edit_paths": edit_paths, "write_sizes": write_sizes, "write_byte": "z"}
        request = {"kind": "EDIT", "prompt": json.dumps(payload), "base": self._git("rev-parse", "HEAD"),
                   "file_choices": {}, "edit_paths": edit_paths}
        code, reserved = self.reserve_selected(task, request)
        self.assertEqual(code, 0, reserved)
        code, ack = self.external("start", task)
        self.assertEqual(code, 0, ack)
        completed = self.wait_terminal(task, timeout=35)
        self.assertEqual(completed["state"], "COMPLETED", completed)

        code, candidate = self.external("candidate", task)
        self.assertEqual(code, 0, candidate)
        candidate_sha = candidate["candidate_sha"]
        code, tested = self.external("mark-tested", task, stdin=json.dumps({"passed": True, "suite": "fixture"}),
                                     extra=("--candidate-sha", candidate_sha, "--report-stdin"))
        self.assertEqual(code, 0, tested)

        command = [sys.executable, str(self.cli), "external", "apply", "--platform", "codex",
                   "--workspace", str(self.workspace), "--task-id", task, "--attempt", "1",
                   "--candidate-sha", candidate_sha]
        process = subprocess.Popen(command, cwd=self.workspace, env=self.env, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True, encoding="utf-8")
        first = self.workspace / edit_paths[0]
        expected = b"z" * write_sizes[edit_paths[0]]
        interrupted = False
        import time
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and process.poll() is None:
            try:
                if first.is_file() and first.read_bytes() == expected and process.poll() is None:
                    process.kill()
                    interrupted = True
                    break
            except OSError:
                pass
            time.sleep(0.001)
        if process.poll() is None:
            process.kill()
        stdout, stderr = process.communicate(timeout=10)
        self.assertTrue(interrupted,
                        "could not interrupt the apply worker after its first full postimage (rc=%r, out=%r, err=%r)"
                        % (process.returncode, stdout, stderr))

        code, pending = self.external("status", task)
        self.assertEqual(code, 0, pending)
        self.assertEqual(pending.get("disposition"), "APPLYING", pending)
        code, recovered = self.external("reconcile", task, extra=("--target", "apply"), timeout=30)
        self.assertEqual(code, 0, recovered)
        self.assertEqual(recovered.get("disposition"), "NOT_APPLIED", recovered)
        self.assertEqual(recovered.get("apply_report", {}).get("reason"), "ROLLBACK_VERIFIED", recovered)
        self.assertTrue(all(not (self.workspace / path).exists() for path in edit_paths), recovered)

        code, blocked = self.reserve_selected("after-recovery", request)
        self.assertNotEqual(code, 0, blocked)
        self.assertEqual(blocked.get("reason"), "EDIT_SCOPE_BUSY", blocked)
        code, discarded = self.external("discard", task)
        self.assertEqual(code, 0, discarded)
        self.assertEqual(discarded.get("disposition"), "DISCARDED", discarded)

    def test_fake_runtime_has_success_deny_and_barrier_modes(self):
        """Fixture backend itself is deterministic and never contacts a provider."""
        fake = ROOT / "tests" / "fixtures" / "external" / "fake_agy.py"
        run_dir = self.root / "fake-run"
        run_dir.mkdir()
        (run_dir / "input.txt").write_text("marker-17\n", encoding="utf-8")
        success = subprocess.run([sys.executable, str(fake), "--workspace", str(run_dir)],
                                 cwd=run_dir, capture_output=True, text=True, encoding="utf-8", timeout=10)
        self.assertEqual(success.returncode, 0, success.stderr)
        self.assertEqual(json.loads(success.stdout)["response"], "marker-17")

        denied = subprocess.run([sys.executable, str(fake)], cwd=run_dir,
                                env=dict(self.env, FAKE_AGY_MODE="deny"), capture_output=True,
                                text=True, encoding="utf-8", timeout=10)
        self.assertEqual(denied.returncode, 0)
        self.assertIn("Tool run_command denied: requires approval in headless mode", denied.stderr)

        barrier = self.root / "release-barrier"
        marker = self.root / "spawn-marker"
        blocked = subprocess.Popen([sys.executable, str(fake)], cwd=run_dir,
                                  env=dict(self.env, FAKE_AGY_BARRIER=str(barrier),
                                           FAKE_AGY_MARKER=str(marker), FAKE_AGY_BARRIER_TIMEOUT="3"),
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                  encoding="utf-8")
        try:
            # Marker proves the fake process started; absent release keeps it pending.
            import time
            deadline = time.monotonic() + 2
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(marker.exists(), "fake process did not reach barrier")
            self.assertIsNone(blocked.poll(), "fake process completed before barrier release")
            barrier.touch()
            stdout, stderr = blocked.communicate(timeout=5)
            self.assertEqual(blocked.returncode, 0, stderr)
            self.assertEqual(json.loads(stdout)["response"], "marker-17")
        finally:
            if blocked.poll() is None:
                blocked.kill()
                blocked.communicate(timeout=5)


if __name__ == "__main__":
    unittest.main()
