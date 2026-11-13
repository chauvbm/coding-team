# Maintenance and evaluation

## Branch workflow

Work on a feature branch, never directly on `main`. Before publishing, run the stdlib suite and `git diff --check`, get the change reviewed, and only then push the branch and open a pull request. After pushing, confirm CI passed on the exact commit you pushed. Do not push unreviewed work to make CI run.

```text
python -m unittest discover -s tests -p "test_*.py" -v
git diff --check
```

CI runs the suite on Windows/Linux and Python 3.11/3.13, including historical decision replay and benchmark acceptance. A passing replay proves artifact/checker consistency, not that the current skill produced those decisions again. When available, also run the external skill-creator `scripts/quick_validate.py`.

Code is Python 3.11+ standard library only. Text files are UTF-8 without BOM, with LF newlines. Keep the guarantee rules intact: write `ENFORCED` only where a runtime mechanism exists, and never let a role with a shell, browser testing or any MCP tool report `no_file_write` or `no_delegate` as ENFORCED.

## Runtime smoke tests and behavioral evaluation (outside CI)

Neither runs in CI: they need a real, signed-in Claude Code, Codex or Antigravity runtime.

- **Platform smoke test:** follow [the smoke kit](docs/smoke/platform-smoke.md) with [the result template](docs/smoke/result-template.json). It installs into a throwaway project with `--scope project`, checks dispatch, model override, isolation, no nesting, runtime failures and the blind routing evaluation, and stores results in `evaluations/<YYYY-MM-DD>-platform-smoke/<platform>.json`. Record `NOT_RUN` with a reason instead of guessing. The README's "Verified platforms" table is updated only from a published `release.json` in that directory.
- **Behavioral evaluation:** for a substantial instruction change, use fresh independent evaluators with realistic requests. Export only inputs and operational resources with `scripts/export_evaluation_inputs.py --cases <fixture>` (it copies `SKILL.md`, `references`, `scripts`, `agents` and `platforms`, never tests, checks or results); keep the oracle and earlier verdicts coordinator-only; score with `tests/check_behavioral_results.py --cases <fixture> <responses...>`. Freeze acceptance checks before dispatch. Preserve failures and protocol corrections rather than changing expectations to obtain a pass. Review rationales and actual artifacts as well as structured scores. Instruction-scoped isolation is not a filesystem sandbox.

Do not rewrite published results. `tests/realistic_cases_v3.json` is a protocol separate from the frozen v2 evaluation; the [v3 evaluation](evaluations/2026-11-01-protocol-v3/README.md) publishes raw responses and scores (14/16 with skill, 13/16 control). No v2 scores are rewritten or counted as v3 results. The [cross-module experiment](benchmarks/cross-module/README.md) preserves a frozen contract, pinned upstream source and five fresh attempts per condition; replaying its snapshots with the scorer is not another live run, and its 28/28 tie and unmatched-compute/timing limitations stay as recorded. These records predate the multi-platform version.

Measure context with an actual tokenizer or host telemetry when available. Word/character counts are size indicators, not measured tokens. Report unavailable token/cost data as null; account-wide quota deltas cannot attribute usage to a particular worker.

## Portable archives

Distribute a named committed revision rather than zipping the working directory:

```text
git archive --format=zip --output=../coding-team.zip HEAD
```

Use a new output filename if an archive already exists. Git archive includes only committed files, so local graphs, caches, private state and untracked artifacts stay out. `.gitattributes` makes text checkouts LF; historical published SHA256 values refer to UTF-8 text with LF newlines. Do not replace historical evidence merely to make it appear current. This full archive includes benchmark and evaluation artifacts for expert review; it is not the smaller runtime package below.

Project-specific tools such as graph indexers are optional development aids, not skill prerequisites. Keep their generated files out of release archives and runtime instructions.

## Runtime-only installation package

```text
python scripts/build_runtime_package.py --output ../coding-team.zip
python scripts/build_runtime_package.py --platform claude-code --output ../coding-team-claude-code.zip
```

Run from a stable, committed checkout. Omit `--output` for a new temporary destination (`coding-team.zip` in a fresh temporary directory); existing destinations and destinations inside the source tree are refused. The package contains `LICENSE`, `SKILL.md`, `agents` (including `agents/roles`), `platforms`, `references` and `scripts` under `coding-team/`, plus `runtime-package.json` with canonical UTF-8/LF hashes and sizes. `--platform P` keeps only `platforms/P.json` (an unknown platform is rejected before any output is created) and records `"platform": "P"` in the manifest. It excludes tests, benchmarks, evaluations, vendored benchmark code, Git metadata, graphs and caches. The source Git HEAD in the manifest is informational: the package captures actual working-tree bytes.

To install from it, extract into a staging directory, inspect the manifest, then run `coding-team/scripts/coding_team.py install --platform P --dry-run` and `install --platform P` from the extracted copy (see the README). The installer copies `scripts/*.py` except `export_evaluation_inputs.py` and `build_runtime_package.py`, which are maintainer tools. The builder itself does not install, change policy or touch global instructions. It rejects linked resources before creating output and requires a filesystem supporting atomic hard-link publication.
