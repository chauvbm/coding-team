# coding-team đa nền tảng và phân luồng Superpowers — Kế hoạch triển khai (bản 2)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Mục tiêu:** Từ một mã nguồn, cài được coding-team cho Claude Code, Codex và Antigravity. Bộ cài sinh agent tĩnh cho 6 vai trò, đi kèm bộ lệnh `/coding-team` mới và chính sách phân luồng một-bộ-điều-phối với Superpowers.

**Kiến trúc:**
- Lõi dùng chung: `SKILL.md`, `references/`, các module Python trong `scripts/`.
- Mỗi nền tảng có một file dữ liệu adapter: `platforms/<tên>.json`.
- Một CLI duy nhất `scripts/coding_team.py` điều phối các module:
  - `ct_storage`: khoá, ghi nguyên tử, chặn liên kết.
  - `ct_platform`: cấu hình nền tảng, đường dẫn.
  - `ct_roles`: vai trò, năng lực, sinh agent.
  - `ct_policy`: state và lệnh bật/tắt.
  - `ct_rules`: khối chỉ dẫn toàn cục.
  - `ct_install`: cài và gỡ theo giao dịch, có journal phục hồi.
  - `ct_orchestration`: ownership theo repo, phân luồng tất định, bàn giao.

**Tech Stack:** Python 3.11 trở lên, chỉ thư viện chuẩn (`json`, `tomllib`, `hashlib`, `argparse`, `subprocess`, `fnmatch`, `unittest`). Markdown, JSON. CLI `git` (dùng để liệt kê worktree).

**Spec:** `docs/superpowers/specs/2026-11-13-multi-platform-and-orchestration-design.md` (bản 2, đã duyệt). Người thực thi phải đọc cả spec lẫn kế hoạch này.

## Quy trình thực thi

- **Nhánh làm việc:** `feat/multi-platform`, tạo từ `main`. Controller tạo nhánh trước Task 1. Mỗi lần implementer kết thúc một task, `HEAD` phải ở trên nhánh này.
- **Implementer:** làm TDD (RED → GREEN → refactor), **chỉ commit cục bộ**, không bao giờ push.
- **Controller:**
  - Chỉ push (`git push origin feat/multi-platform`) khi task review đã đạt.
  - Với các task checkpoint (Task 4, 8, 11), chỉ push sau khi bộ test tích hợp của checkpoint đạt.
  - Task chưa được push thì chưa được tính là xong.
- **Không chạy song song nhiều implementer.** Thứ tự task là bắt buộc, vì các task sau dùng interface của task trước.
- **Merge vào `main` chỉ ở Task 15**, sau khi đủ ba điều kiện:
  1. final review toàn nhánh đạt;
  2. qua release gate (smoke test từng nền tảng, hoặc người dùng chấp nhận rõ ràng mục `NOT_RUN`);
  3. người dùng xác nhận merge.
- **Isolation khi thực thi:** Leader quyết định theo mục đích chứ không theo tên vai trò.
  - Kiểm chứng working tree hiện tại (kể cả thay đổi chưa commit): dùng workspace hiện tại.
  - Kiểm chứng một snapshot đã commit: có thể dùng worktree riêng.
  - Nhiều Developer: dùng ma trận phân chia file, hoặc worktree riêng cho từng người.

  Quy tắc này được ghi vào tài liệu ở Task 12. File agent tĩnh không cố định `isolation`.

## Global Constraints

- Python 3.11 trở lên, chỉ thư viện chuẩn. CI giữ ma trận Ubuntu/Windows × Python 3.11/3.13. Lệnh test: `python -m unittest discover -s tests -p "test_*.py" -v`.
- File `.md` sinh ra hoặc ghi ra phải là UTF-8 **không BOM**, dùng LF.
- Không đổi định dạng ledger. Vai trò cổng duyệt logic vẫn là `Architect`, `Developer`, `QA`.
- **Không sửa:**
  - `benchmarks/` và `evaluations/` (trừ việc thêm thư mục kết quả smoke mới ở Task 15).
  - `tests/behavioral_cases.json`, `tests/sequential_cases.json`, `tests/reviewer_transfer_cases.json`, `tests/mode_cases.json`, `tests/realistic_cases_v2.json`, `tests/realistic_cases_v3.json`, `tests/check_behavioral_results.py`.
- Test cũ bị thay thế phải có test mới bao phủ tương đương, theo **Bảng chuyển test** bên dưới. Không được xoá test cũ khi chưa có test thay thế.
- Không thêm hook vào settings của người dùng. Không ghi ra ngoài marker `<!-- coding-team-policy:start -->` / `<!-- coding-team-policy:end -->` trong file chỉ dẫn toàn cục.
- Mọi lần ghi ra ngoài repo đều phải sao lưu trước. `--dry-run` **không được** tạo file, thư mục, lock, staging hay backup.
- Không ghi qua symlink, junction hay reparse point, ở cả đích lẫn mọi thư mục tổ tiên nằm dưới root được quản lý.
- **Mức bảo đảm:**
  - Chỉ ghi `ENFORCED` khi có cơ chế runtime tương ứng.
  - Vai trò có shell, `browser_testing`, hoặc MCP không nằm trong danh sách MCP chỉ-đọc: `no_file_write` và `no_delegate` đều không bao giờ là `ENFORCED`.
  - `write:assigned` và `write:tests` luôn là `ADVISORY`.
- ID vai trò: `ct-architect`, `ct-researcher`, `ct-developer`, `ct-test-engineer`, `ct-reviewer`, `ct-security`. Biến thể thêm: `ct-reviewer-verify`.
- Tầng model: `inherit`, `strong`, `standard`, `light`. Nền tảng: `claude-code`, `codex`, `antigravity`.
- Cú pháp lệnh duy nhất: `/coding-team status|on|off|on --global|off --global|reset|help`. Trên Codex chấp nhận thêm tiền tố `$coding-team`. Cú pháp cũ (`/coding team …`, `on all`, `off all`, `inherit`) bị từ chối, kèm gợi ý cú pháp mới.
- State bật/tắt luôn ở cấp người dùng. Không có nguồn mã chat tin cậy thì chat là `UNKNOWN`. Không bao giờ đoán mã chat.
- Commit message kết thúc bằng dòng `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Quyết định của kế hoạch (bổ sung cho spec)

1. **Một nguồn cho tầng model:** tầng mặc định chỉ nằm trong `platforms/*.json` (`roles`). `agents/roles/roles.json` chỉ chứa năng lực.
2. **Không cố định `isolation` trong file agent:** quyết định theo mục đích, như mục "Quy trình thực thi".
3. **Vị trí file phụ trợ:** `backups/`, `install.lock` và `install-journal.json` nằm trong `state_file.parent`.
4. **`--scope project`** không được kết hợp với `--default` hay `--global-rules`.
5. **Ghi đè file:**
   - File do coding-team quản lý mà người dùng đã sửa (hash khác manifest): **mặc định từ chối** và báo xung đột. Chỉ ghi đè khi có `--overwrite-modified`, và vẫn sao lưu trước.
   - File không do coding-team quản lý: cần `--replace-unmanaged`.
6. **Giao dịch nhiều file:**
   - `atomic_write` chỉ bảo vệ từng file riêng lẻ.
   - Trước lần thay file đầu tiên, ghi `install-journal.json`. Nếu tiến trình bị tắt giữa chừng, journal còn lại; lần `install`/`uninstall` sau sẽ từ chối và yêu cầu chạy `install --recover`.
   - Lỗi Python thông thường thì tự rollback.
7. **Ownership điều phối theo repository, không theo worktree:**
   - File ở `<worktree chính>/.coding-team/orchestration.json`, trong đó worktree chính là mục đầu tiên của `git worktree list --porcelain`.
   - **Không đặt trong `.git/`**, vì Claude Code chặn agent ghi vào `.git/`.
   - Ngoài git thì dùng `<workspace>/.coding-team/`.
   - Thư mục này tự tạo `.gitignore` với nội dung `*`.
8. **Workspace SDD:**
   - Khi tồn tại `.superpowers/sdd/*/progress.md` ở bất kỳ worktree nào, gọi là **`unresolved_sdd_workspaces`**: tiến độ chưa được giải quyết.
   - Đây **không phải** bằng chứng có tiến trình SDD đang chạy.
   - Kết quả: HOLD để người dùng xác định tiếp tục hay bàn giao.
9. **Bàn giao:**
   - `from` phải khớp owner hiện tại của task, nếu đã có record.
   - Bằng chứng worker đã dừng là **do caller khai báo**: kết quả ghi `termination_evidence: "caller-asserted"`, và tài liệu yêu cầu Leader kiểm tra bằng công cụ runtime trước khi ghi.
10. **Phân luồng tất định:**
    - `ct_orchestration.route(facts)` thực thi bảng §6.2 trên các fact đã phân loại. Model vẫn là bên phân loại fact, nên phân luồng nói chung vẫn ADVISORY.
    - Trường hợp không khớp điều kiện nào: L2 → `coding-team`, L0/L1 → `leader-direct`.
11. **`docs_lookup`:**
    - Claude Code: dùng `WebFetch`, `WebSearch`, cộng các công cụ MCP do người dùng truyền qua `install --docs-mcp NAME` (lặp lại được).
    - MCP khớp `read_only_mcp_patterns` (Context7) thì giữ được `ENFORCED`; MCP khác hạ xuống `ADVISORY`.
    - Antigravity `ct-researcher` để `inheritMcp: false` (tra cứu qua `search_web`/`read_url_content`). Chỉ `ct-test-engineer` có `inheritMcp: true`; vai trò này vốn đã ADVISORY vì có shell.
    - Codex: không áp dụng (mọi mức là ADVISORY).

## Bảng chuyển test

| Test cũ | Test thay thế (Task) |
|---|---|
| `test_policy_helper.test_initialize_defaults_and_preserves_existing_bytes` | `test_policy.test_initialize_default` (5) |
| `…test_all_aliases_and_normalization` | `test_policy.test_new_grammar` (5) |
| `…test_only_complete_unquoted_commands_are_accepted` | `test_policy.test_embedded_or_quoted_commands_rejected` (5) |
| `…test_chat_precedence_global_preservation_and_inherit` | `test_policy.test_chat_precedence_and_reset` (5) |
| `…test_status_unknown_identity_and_noop_do_not_write` | `test_policy.test_status_unknown_and_noop_do_not_write` (5) |
| `…test_missing_and_corrupt_state_fail_closed_without_overwrite` | `test_policy.test_missing_and_corrupt_state_fail_closed` (5) |
| `…test_cli_chat_identity_and_alias` | `test_cli.test_install_status_command_roundtrip` (10) |
| `…test_bootstrap_idempotent_preserves_unrelated_bytes_and_custom_paths` | `test_rules.test_upsert_idempotent_preserves_unrelated_bytes` (6) |
| `…test_shell_argument_quotes_metacharacters_for_both_shells` | `test_storage.test_shell_argument_quotes_metacharacters_for_both_shells` (1) |
| `…test_generated_bootstrap_commands_execute_with_literal_special_paths` | `test_rules.test_generated_commands_execute_with_special_paths` (6, chạy thật từ Task 10) |
| `…test_bootstrap_append_to_missing_and_unmarked_files` | `test_rules.test_upsert_append_to_missing_and_unmarked` (6) |
| `…test_bootstrap_rejects_malformed_duplicate_and_reversed_markers` | `test_rules.test_upsert_rejects_malformed_markers` (6) |
| `…test_atomic_replace_failure_preserves_original_and_cleans_temp`, `…test_atomic_fsync_failure_…` | `test_storage` cùng tên (1) |
| `…test_os_lock_timeout_and_process_exit_release`, `…test_os_lock_released_when_body_raises` | `test_storage` cùng tên (1) |
| `…test_concurrent_subprocess_updates_do_not_lose_chat_overrides` | `test_policy.test_concurrent_updates_do_not_lose_chat_overrides` (5) |
| `test_model_profiles.test_shipped_profiles_validate_without_claiming_availability` | `test_platform_config.test_shipped_platforms_validate` (2) |
| `…test_custom_model_names_and_null_inheritance_are_valid_structure` | `test_platform_config.test_custom_model_and_inherit_valid` (2) |
| `…test_top_level_shape_schema_and_profile_selection_are_checked`, `…test_missing_unknown_roles_and_selection_fields_are_rejected`, `…test_model_and_effort_values_are_not_silently_inherited` | `test_platform_config.test_schema_rejects_bad_shapes` (2) |
| `…test_duplicate_json_keys_are_rejected_at_every_object_depth` | `test_platform_config.test_duplicate_keys_rejected` (2) |
| `…test_cli_reports_valid_invalid_and_missing_inputs_without_writes` | `test_cli.test_check_config` (10) |

## Review Focus

1. **Đường dẫn home hoặc cấu hình có dấu cách, dấu nháy đơn, ký tự tiếng Việt** (ví dụ `C:\Users\Nguyễn Văn A`): cài đặt thành công, lệnh nhúng trong khối chỉ dẫn chạy được nguyên văn. Test ở Task 6 và Task 7.
2. **Biến môi trường được đặt nhưng rỗng hoặc chỉ có khoảng trắng** (`CLAUDE_CONFIG_DIR=""`, `CLAUDE_CODE_SESSION_ID=" "`): coi như chưa đặt. Test ở Task 2.
3. **Hai biến nhận diện nền tảng cùng xuất hiện** (`CLAUDECODE=1` và `CODEX_THREAD_ID`): `detect_platform` trả `None`, CLI báo lỗi yêu cầu `--platform`. Test ở Task 2 và Task 10.
4. **File chỉ dẫn toàn cục có sẵn BOM, CRLF hoặc byte không phải UTF-8**: byte ngoài marker giữ nguyên. Test ở Task 6.
5. **Đích cài đặt (hoặc thư mục tổ tiên) là symlink hoặc junction trỏ ra ngoài**: từ chối, không ghi gì. Test ở Task 7.

---

### Task 1: Module lưu trữ dùng chung `ct_storage`

**Files:**
- Create: `scripts/ct_storage.py`
- Modify: `scripts/task_ledger.py:14-21` (đổi import)
- Test: `tests/test_storage.py`

**Interfaces:**
- Produces:
  - `locked(path: Path, timeout: float = 5.0)`: context manager, khoá OS trên file `<path>.lock`.
  - `atomic_write(path: Path, payload: bytes) -> None`.
  - `shell_argument(value: str) -> str`.
  - Ba hàm trên được chuyển nguyên văn từ `scripts/coding_team_policy.py`.
  - `class LinkedPathError(ValueError)`.
  - `reject_links(path: Path, stop_at: Path) -> None`: kiểm tra `path` và mọi thư mục tổ tiên đã tồn tại, cho tới (và gồm cả) `stop_at`. Raise `LinkedPathError` nếu gặp symlink, junction hoặc reparse point. Logic lấy từ `reject_link` trong `build_runtime_package.py` (`lstat`, `FILE_ATTRIBUTE_REPARSE_POINT`, `is_junction`).

- [ ] **Step 1: Viết test.** Chuyển 5 test theo Bảng chuyển test sang `tests/test_storage.py`. Đầu file thêm `sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))` rồi `import ct_storage`. Thêm hai test:
  - `test_reject_links_symlink_ancestor`: tạo `root/real` và `root/link -> root/real`; `reject_links(root/'link'/'file.txt', root)` raise `LinkedPathError`. Nếu hệ điều hành không cho tạo symlink thì `skipTest`.
  - `test_reject_links_accepts_plain_and_missing_paths`: đường dẫn thường, hoặc chưa tồn tại, không raise.
- [ ] **Step 2: Chạy** `python -m unittest tests.test_storage -v`. Kỳ vọng FAIL với `ModuleNotFoundError`.
- [ ] **Step 3: Tạo `scripts/ct_storage.py`.** Trong `task_ledger.py`, đổi import sang `ct_storage`, giữ nhánh dự phòng `importlib` trỏ tới `ct_storage.py`.
- [ ] **Step 4: Chạy** `python -m unittest tests.test_storage tests.test_task_ledger -v`. Kỳ vọng PASS.
- [ ] **Step 5: Commit cục bộ** với message `refactor: extract shared storage primitives and link guard`.

### Task 2: Cấu hình nền tảng `platforms/*.json` và `ct_platform`

**Files:**
- Create: `platforms/claude-code.json`, `platforms/codex.json`, `platforms/antigravity.json`, `scripts/ct_platform.py`
- Test: `tests/test_platform_config.py`

**Interfaces:**
- Produces:
  - Hằng số `PLATFORMS`, `ROLES` (đúng 6 ID, không có biến thể), `TIERS`, `REPO_ROOT: Path`.
  - `read_json_unique(path: Path) -> object`: từ chối khoá trùng ở mọi cấp, đọc `utf-8-sig`.
  - `validate_platform(data: dict) -> dict`: trả `{'valid': True, 'platform': str, 'availability_verified': False}`, raise `ValueError` khi sai.
  - `load_platform(name: str, source_root: Path = REPO_ROOT, input_path: Path | None = None) -> dict`.
  - `resolve_config_root(cfg, env: Mapping[str, str], home: Path) -> Path`.
  - `target_paths(cfg, scope: str, env, home: Path, project_dir: Path | None) -> dict[str, Path | None]`. Các khoá: `skill_dir`, `agents_dir`, `global_rules_file` (`None` khi scope là project), `state_file`, `backup_root`, `lock_file`, `journal_file`, `managed_roots` (danh sách root dùng làm `stop_at` cho `reject_links`).
  - `detect_platform(env) -> str | None`.
  - `resolve_tier(cfg, role: str, tier: str | None = None) -> dict`: trả `{'tier', 'model', 'effort'}`.
  - `chat_id_from_env(cfg, env) -> str | None`.

**Lược đồ v2** (khoá bắt buộc, đúng tập này):
- `schema_version` = 2, `platform`, `verified_on` (`YYYY-MM-DD`).
- `config_root` = `{env: str|null, default: str}`.
- `tiers`: đúng 4 khoá; `inherit` = `{null, null}`.
- `roles`: đúng 6 khoá, mỗi khoá `{tier}`.
- `effort_vocabulary`; `static_agent_model` ∈ `{default, omit}`; `tool_allowlist_enforced` (bool).
- `tools`: các khoá `read`, `docs_lookup`, `write`, `shell`, `browser_testing`, `delegate`.
- `read_only_mcp_patterns`: danh sách pattern fnmatch.
- `agent_format` ∈ `{claude-md, codex-toml, antigravity-md}`.
- `paths`: `skill_dir`, `agents_dir`, `global_rules_file`, `state_file`; chỉ dùng placeholder `{config_root}` và `{home}`.
- `project_paths`: `skill_dir`, `agents_dir`; đường dẫn tương đối, không có `..`.
- `chat_id_env`, `detect_env`, `force_model_env`, `invocation`.

**Giá trị cụ thể** (`"verified_on": "2026-11-13"`; các mục spec ghi "cần xác minh" phải được kiểm tra lại trước khi ghi):

| Khoá | claude-code | codex | antigravity |
|---|---|---|---|
| config_root | env `CLAUDE_CONFIG_DIR`, default `{home}/.claude` | env `CODEX_HOME`, default `{home}/.codex` | env null, default `{home}/.gemini` |
| tiers strong / standard / light | `opus`/`high`, `sonnet`/`medium`, `haiku`/`low` | `gpt-6.1-sol`/`high`, `gpt-6-luna`/`medium`, `gpt-6-luna`/`low` | `pro`, `flash`, `flash_lite` (effort null) |
| roles | architect strong, researcher light, developer standard, test-engineer standard, reviewer strong, security strong | như cột trái | như cột trái |
| effort_vocabulary | `low, medium, high, xhigh, max` | `low, medium, high, xhigh, max, ultra` | `[]` |
| static_agent_model | `default` | `omit` | `omit` |
| tool_allowlist_enforced | `true` | `false` | `true` |
| tools.read | `Read, Grep, Glob` | `[]` | chép đúng danh sách tools của `agents/antigravity/ct-architect.md` trong repo agy |
| tools.docs_lookup | `WebFetch, WebSearch` | `[]` | `[]` |
| tools.write | `Edit, Write, NotebookEdit` | `[]` | `write_to_file, replace_file_content, multi_replace_file_content, notebook_edit` |
| tools.shell | `Bash, PowerShell` | `[]` | `run_command, manage_task` |
| tools.browser_testing | `Bash, PowerShell` | `[]` | `run_command` |
| tools.delegate | `Agent` | `[]` | `invoke_subagent, define_subagent` |
| read_only_mcp_patterns | `mcp__*context7*__resolve-library-id`, `mcp__*context7*__query-docs`, `mcp__*context7*__get-library-docs` | `[]` | `[]` |
| agent_format | `claude-md` | `codex-toml` | `antigravity-md` |
| paths.skill_dir | `{config_root}/skills/coding-team` | `{home}/.agents/skills/coding-team` | `{config_root}/config/skills/coding-team` |
| paths.agents_dir | `{config_root}/agents` | `{config_root}/agents` | `{config_root}/config/agents` |
| paths.global_rules_file | `{config_root}/CLAUDE.md` | `{config_root}/AGENTS.md` | `{config_root}/config/rules/coding-team-policy.md` |
| paths.state_file | `{config_root}/coding-team/state.json` | `{config_root}/coding-team/state.json` | `{config_root}/antigravity/coding-team/state.json` |
| project_paths | `.claude/skills/coding-team`, `.claude/agents` | `.agents/skills/coding-team`, `.codex/agents` | `.agents/skills/coding-team`, `.agents/agents` |
| chat_id_env | `CLAUDE_CODE_SESSION_ID` | `CODEX_THREAD_ID` | `ANTIGRAVITY_CONVERSATION_ID, ANTIGRAVITY_CHAT_ID` |
| detect_env | `CLAUDECODE` | `CODEX_THREAD_ID, CODEX_HOME` | `ANTIGRAVITY_CONVERSATION_ID, ANTIGRAVITY_CHAT_ID` |
| force_model_env | `CLAUDE_CODE_SUBAGENT_MODEL_FORCE` | `[]` | `[]` |
| invocation | `/coding-team` | `$coding-team` | `/coding-team` |

- [ ] **Step 1: Viết test** trong `tests/test_platform_config.py`:
  - `test_shipped_platforms_validate`: cả 3 file cho `valid is True`, `availability_verified is False`, `platform` khớp tên file.
  - `test_custom_model_and_inherit_valid`: model tuỳ ý (`organization-private-model`) với effort hợp lệ thì vẫn hợp lệ.
  - `test_schema_rejects_bad_shapes`: các biến thể sau đều raise `ValueError`:
    - thiếu tầng, hoặc có tầng lạ;
    - `inherit` khác null;
    - thiếu vai trò, thừa vai trò, hoặc dùng tên cũ `QA / Reviewer`;
    - model là `''`, `'  '`, `True`, `12`;
    - effort là `'maximum'`, `'HIGH'`, `[]`, hoặc nằm ngoài từ vựng;
    - `static_agent_model: "always"`;
    - `project_paths` tuyệt đối hoặc có `..`;
    - placeholder `{user}`;
    - `schema_version: 1`;
    - `read_only_mcp_patterns` không phải danh sách.
  - `test_duplicate_keys_rejected`: khoá trùng ở cấp gốc và cấp lồng.
  - `test_config_root_env_and_empty_env`: `CLAUDE_CONFIG_DIR=<tmp>/cfg` cho `<tmp>/cfg/skills/coding-team`; giá trị `""` và `"  "` cho `<home>/.claude/...`.
  - `test_project_scope_paths`: `skill_dir == project_dir/'.claude/skills/coding-team'`; `global_rules_file is None`; `state_file` vẫn ở cấp người dùng.
  - `test_aux_files_next_to_state`: `backup_root`, `lock_file`, `journal_file` có cha là `state_file.parent`, với tên lần lượt `backups`, `install.lock`, `install-journal.json`.
  - `test_detect_platform`: `{'CLAUDECODE': '1'}` cho `'claude-code'`; `{'CODEX_THREAD_ID': 't'}` cho `'codex'`; `{}` cho `None`; có cả `CLAUDECODE` và `CODEX_THREAD_ID` cho `None`; `{'CLAUDECODE': ''}` cho `None`.
  - `test_resolve_tier`: Claude Code `ct-architect` cho `('strong', 'opus', 'high')`; `tier='light'` cho `haiku`; `inherit` cho model và effort đều `None`; tầng lạ raise `ValueError`.
  - `test_chat_id_from_env`: `' abc '` cho `'abc'`; rỗng hoặc chỉ khoảng trắng cho `None`; Antigravity ưu tiên `ANTIGRAVITY_CONVERSATION_ID`.
- [ ] **Step 2: Chạy test.** Kỳ vọng FAIL.
- [ ] **Step 3: Tạo 3 file JSON và `scripts/ct_platform.py`.** `scope` khác `user` hoặc `project` thì raise `ValueError`.
- [ ] **Step 4: Chạy test.** Kỳ vọng PASS.
- [ ] **Step 5: Commit cục bộ** với message `feat: add per-platform adapter configs`.

### Task 3: Danh mục vai trò, ma trận năng lực và trình sinh agent `ct_roles`

**Files:**
- Create:
  - `agents/roles/roles.json`
  - `agents/roles/ct-architect.md`, `ct-researcher.md`, `ct-developer.md`, `ct-test-engineer.md`, `ct-reviewer.md`, `ct-security.md`
  - `scripts/ct_roles.py`
- Test: `tests/test_roles.py`

**Interfaces:**
- Consumes: `ct_platform.load_platform`, `ct_platform.resolve_tier`, `ct_platform.ROLES`.
- Produces:
  - `CAPABILITIES = ('read', 'docs_lookup', 'write:assigned', 'write:tests', 'shell', 'shell:checks', 'browser_testing')`.
  - `load_roles(directory: Path) -> dict[str, dict]`: mỗi phần tử là `{'description', 'ledger_role', 'variants': {variant_id: [cap, ...]}, 'body'}`.
  - `extra_mcp_tools(capabilities, mcp_tools: dict[str, list[str]] | None) -> list[str]`: các công cụ MCP được cấp cho vai trò, lấy theo những capability mà vai trò có.
  - `guarantees(capabilities, cfg, mcp_tools=None) -> dict[str, str]`: khoá `no_file_write`, `write_scope`, `command_limits`, `no_delegate`; giá trị `ENFORCED`, `ADVISORY` hoặc `N/A`.
  - `tools_for(capabilities, cfg, mcp_tools=None) -> list[str]`: không trùng, giữ thứ tự, không bao giờ chứa phần tử nào của `cfg['tools']['delegate']`.
  - `inherits_mcp(capabilities, cfg) -> bool`: `True` chỉ khi `agent_format == 'antigravity-md'` và có `browser_testing`.
  - `render_agent(role_id, variant_id, roles, cfg, mcp_tools=None) -> tuple[str, bytes]`.
  - `render_all(roles, cfg, mcp_tools=None) -> list[tuple[str, bytes]]`.

**`roles.json`** (schema_version 1):

| Vai trò | ledger_role | Biến thể và năng lực |
|---|---|---|
| ct-architect | `Architect` | `ct-architect`: `[read]` |
| ct-researcher | null | `ct-researcher`: `[read, docs_lookup]` |
| ct-developer | `Developer` | `ct-developer`: `[read, write:assigned, shell]` |
| ct-test-engineer | null | `ct-test-engineer`: `[read, write:tests, shell, browser_testing]` |
| ct-reviewer | `QA` | `ct-reviewer`: `[read]`; `ct-reviewer-verify`: `[read, shell:checks]` |
| ct-security | null | `ct-security`: `[read]` |

**Quy tắc `guarantees`.** Ký hiệu:
- `E = cfg['tool_allowlist_enforced']`;
- `shell_like` = có `shell`, `shell:checks` hoặc `browser_testing`;
- `unsafe_mcp` = có công cụ trong `extra_mcp_tools` không khớp `read_only_mcp_patterns`, **hoặc** `inherits_mcp(...)`.

Quy tắc:
- `no_file_write`:
  - có `write:*` → `N/A`;
  - nếu không, khi `shell_like` hoặc `unsafe_mcp` → `ADVISORY`;
  - còn lại: `ENFORCED` nếu `E`, ngược lại `ADVISORY`.
- `no_delegate`: `ENFORCED` chỉ khi có `E`, không `shell_like` và không `unsafe_mcp`; ngược lại `ADVISORY`. Lý do: shell có thể khởi chạy một AI CLI khác.
- `write_scope`: có `write:*` → `ADVISORY`, ngược lại `N/A`.
- `command_limits`: có `shell:checks` → `ADVISORY`, ngược lại `N/A`.

**Phần thân các file `ct-*.md`:**
- Chuyển contract riêng từng vai trò từ `references/agent-contracts.md`:
  - Architect → ct-architect;
  - Investigator → ct-researcher;
  - Developer → ct-developer (thêm mục `focus`);
  - Test Engineer → ct-test-engineer (thêm mục `browser_testing` theo spec §5.1);
  - QA / Reviewer → ct-reviewer (thêm mục "Chế độ verify" và quy tắc isolation theo mục đích);
  - Specialist → ct-security (viết lại cho bảo mật, hiệu năng, xử lý đồng thời, kèm điều kiện L3 §6.7).

**`render_agent`:**
- Đầu phần thân luôn có `WORKER_CLAUSE`, nguyên văn:
  > You are a coding-team worker, not the Leader. Leader-only rules, routing policy and orchestrator skills (coding-team, subagent-driven-development, dispatching-parallel-agents, executing-plans, requesting-code-review) do not apply to you and must not be invoked. Never dispatch, spawn or define agents, and never start another AI agent or CLI session through a shell. Use technique skills (test-driven-development, systematic-debugging, verification-before-completion, receiving-code-review) only when your handoff names them.
- Tiếp theo là mục `## Runtime guarantees (<platform>)`, mỗi guarantee một dòng `- <key>: <level>`.
- Frontmatter theo `agent_format`:
  - **`claude-md`**, file `<variant>.md`:
    - `name`, `description` (chuỗi trong nháy kép JSON);
    - `tools:` là kết quả `tools_for` nối bằng `", "`;
    - khi `static_agent_model == 'default'` và model khác null thì thêm `model:` và `effort:`.
  - **`codex-toml`**, file `<variant>.toml`:
    - `name`, `description`, `developer_instructions` (dùng chuỗi literal `'''…'''`; nếu thân chứa `'''` thì chuyển sang chuỗi basic `"""…"""` có escape);
    - không có `model`, `model_reasoning_effort`, `sandbox_mode`.
  - **`antigravity-md`**, file `<variant>/agent.md`:
    - `name`, `description`, `mainAgent: false`, `subagent: true`, `hidden: false`, `inheritCustomizations: true`;
    - `inheritMcp`: theo `inherits_mcp`;
    - `commandExecutionPolicy`: `auto` khi `shell_like`, ngược lại `off`;
    - `tools:` dạng danh sách YAML;
    - không có `model`.

- [ ] **Step 1: Viết test** trong `tests/test_roles.py`:
  - `test_catalog_matches_platform_roles`: tập vai trò khớp `ct_platform.ROLES`; các `ledger_role` khác null đúng là `{'Architect', 'Developer', 'QA'}`.
  - `test_guarantee_matrix` (trên Claude Code):
    - `ct-architect`: `no_file_write` và `no_delegate` đều `ENFORCED`;
    - `ct-developer`: `no_delegate == 'ADVISORY'`, `write_scope == 'ADVISORY'`, `no_file_write == 'N/A'`;
    - `ct-reviewer-verify`: `no_file_write` và `no_delegate` đều `ADVISORY`, `command_limits == 'ADVISORY'`;
    - `ct-test-engineer`: `no_delegate == 'ADVISORY'`.
    - Trên Codex, mọi guarantee khác `N/A` đều là `ADVISORY`.
    - Trên Antigravity, `ct-researcher` có `no_file_write == 'ENFORCED'` và `inheritMcp: false`.
  - `test_docs_mcp_read_only_vs_unsafe`:
    - `mcp_tools={'docs_lookup': ['mcp__plugin_context7_context7__query-docs']}`: tools của `ct-researcher` chứa công cụ đó, và `no_file_write`, `no_delegate` vẫn `ENFORCED`;
    - với `['mcp__x__run_command']`: cả hai thành `ADVISORY`;
    - `ct-architect` (không có `docs_lookup`) không nhận công cụ MCP nào.
  - `test_read_only_never_gets_shell_or_delegate`: tools của Claude Code `ct-architect` đúng là `Read, Grep, Glob`; không biến thể nào có `Agent`; trên Antigravity không biến thể nào có `invoke_subagent` hay `define_subagent`.
  - `test_claude_frontmatter`: có `model: opus` và `effort: high`; không có `isolation:`; bắt đầu bằng `---\n`; không có BOM.
  - `test_codex_toml_parses_without_model_or_sandbox`: parse bằng `tomllib`; có đủ 3 khoá bắt buộc; không có `model`, `model_reasoning_effort`, `sandbox_mode`. Thân chứa `'''` và `\` vẫn được giữ nguyên văn.
  - `test_antigravity_flags`: mọi file có `mainAgent: false`, `subagent: true`, `inheritCustomizations: true`; architect có `commandExecutionPolicy: off`, developer có `auto`; không file nào có `model:`.
  - `test_worker_clause_and_guarantees_present`.
  - `test_render_all_count`: mỗi nền tảng sinh đúng 7 file.
- [ ] **Step 2: Chạy test.** Kỳ vọng FAIL.
- [ ] **Step 3: Tạo `roles.json`, 6 file thân và `scripts/ct_roles.py`.**
- [ ] **Step 4: Chạy test.** Kỳ vọng PASS.
- [ ] **Step 5: Commit cục bộ** với message `feat: add role catalog, capability matrix and agent renderers`.

### Task 4: Checkpoint A — tích hợp nền tảng (sau Task 1–3)

**Files:**
- Create: `tests/test_integration_foundation.py`

**Interfaces:**
- Consumes: `ct_storage`, `ct_platform`, `ct_roles`.
- Produces: không có API mới. Đây là cổng tích hợp; Controller chỉ push Task 1–4 sau khi checkpoint này đạt.

- [ ] **Step 1: Viết test** cho các bất biến xuyên module, chạy trên cả 3 nền tảng × mọi biến thể:
  - `test_guarantees_consistent_with_rendered_tools`: nếu tools đã render chứa bất kỳ phần tử nào của `tools.shell`, `tools.write` hoặc `tools.browser_testing`, thì `no_file_write != 'ENFORCED'` và `no_delegate != 'ENFORCED'`.
  - `test_rendered_tier_matches_platform_config`: với `claude-md`, giá trị `model` và `effort` trong frontmatter bằng `resolve_tier(cfg, role)`. Với `codex-toml` và `antigravity-md`, file không có model.
  - `test_every_role_has_tier_and_body`: mọi phần tử của `ROLES` có tier trong mỗi platform JSON, và có thân không rỗng.
  - `test_rendered_paths_are_relative_and_safe`: không đường dẫn nào tuyệt đối hay chứa `..`.
- [ ] **Step 2: Chạy** `python -m unittest tests.test_integration_foundation -v` cùng toàn bộ test. Kỳ vọng PASS (test FAIL thì sửa task gốc, không nới test).
- [ ] **Step 3: Commit cục bộ** (`test: foundation integration checkpoint`). Sau khi review đạt, Controller push nhánh.

### Task 5: State bật/tắt và lệnh mới `ct_policy`

**Files:**
- Create: `scripts/ct_policy.py`
- Test: `tests/test_policy.py`

**Interfaces:**
- Consumes: `ct_storage.locked`, `ct_storage.atomic_write`.
- Produces:
  - `class LegacyCommandError(ValueError)`, có thuộc tính `.hint: str`.
  - `validate_state(state) -> None` (schema v1 giữ nguyên).
  - `load_state(path) -> dict`, `save_state(path, state) -> None`.
  - `initialize_state(path, default: str | None = None) -> tuple[dict, bool]`:
    - state chưa có: tạo với `default or 'ON'`;
    - state đã có và có `default`: chỉ đổi `global_policy`;
    - state đã có và không có `default`: giữ nguyên;
    - `default` không phân biệt hoa thường.
  - `describe(state, chat_id) -> dict`: khoá `global_policy`, `chat_id`, `chat_policy` (`ON`/`OFF`/`INHERIT`/`UNKNOWN`), `effective_policy` (`ON`/`OFF`/`None`).
  - `parse_command(text) -> tuple[str, str | None]`: một trong `('status', None)`, `('help', None)`, `('chat', 'ON'|'OFF'|'RESET')`, `('global', 'ON'|'OFF')`.
  - `apply_command(path, text, chat_id) -> dict`:
    - `help` trả `{'help': HELP_LINES}` và không đọc state;
    - lệnh chat khi thiếu mã chat raise `ValueError('chat command requires a trusted chat id; use --global or pass --chat-id')`;
    - không có gì thay đổi thì không ghi.
  - `HELP_LINES` có đúng 7 dòng.

- [ ] **Step 1: Viết test.** Gồm các test thay thế trong Bảng chuyển test, và:
  - `test_new_grammar`: chuẩn hoá khoảng trắng và hoa thường, với cả tiền tố `/coding-team` và `$coding-team`, cho đủ 7 lệnh.
  - `test_legacy_syntax_rejected_with_hint`: các đầu vào `/coding team on`, `/coding-team on all`, `/coding-team off all`, `/coding-team inherit` raise `LegacyCommandError`. `hint` lần lượt chứa `/coding-team on`, `/coding-team on --global`, `/coding-team off --global`, `/coding-team reset`.
  - `test_embedded_or_quoted_commands_rejected`: tập đầu vào cũ, chuyển sang cú pháp mới, đều raise `ValueError`.
  - `test_chat_precedence_and_reset`: `a` OFF, `b` OFF; `reset` với `a` cho `{'b': 'OFF'}`; `on --global` giữ nguyên các thiết lập riêng của chat.
  - `test_initialize_default`: tạo mới với `'off'` cho `OFF`. Có chat override: `default='on'` chỉ đổi global; `default=None` giữ nguyên byte.
  - `test_help_needs_no_state`: không tạo file, `len == 7`.
- [ ] **Step 2: Chạy test.** Kỳ vọng FAIL.
- [ ] **Step 3: Viết `scripts/ct_policy.py`.**
- [ ] **Step 4: Chạy test.** Kỳ vọng PASS.
- [ ] **Step 5: Commit cục bộ** với message `feat: new /coding-team command grammar and policy state`.

### Task 6: Khối chỉ dẫn toàn cục `ct_rules`

**Files:**
- Create: `scripts/ct_rules.py`
- Test: `tests/test_rules.py`

**Interfaces:**
- Consumes: `ct_storage.locked`, `ct_storage.atomic_write`, `ct_storage.shell_argument`.
- Produces:
  - Marker giữ nguyên: `START = '<!-- coding-team-policy:start -->'`, `END = '<!-- coding-team-policy:end -->'`.
  - `render_rules_block(*, platform: str, python: str, cli: Path, state: Path, skill_dir: Path) -> str`.
  - `upsert_block(path: Path, block: str) -> bool`: marker hỏng, trùng hoặc đảo ngược thì raise `ValueError`.
  - `remove_block(path: Path) -> bool`.

Khối chỉ dẫn (tiếng Anh) phải chứa:
1. **Lệnh status:** `{python} {cli} status --platform {platform} --state {state}`, mọi phần đi qua `shell_argument`; trên Windows có tiền tố `& `.
2. **Mẫu lệnh** `{python} {cli} command '<exact command>' --platform {platform} --state {state}`, trong đó `<exact command>` cũng đi qua `shell_argument`.
3. **7 lệnh mới.**
4. **Hợp đồng mã chat:** `--chat-id`, rồi biến runtime tin cậy, còn lại là `UNKNOWN`; không bao giờ đoán.
5. **Bảng phân luồng rút gọn** theo đúng thứ tự §6.2, có câu `file count alone is never the criterion`, kèm đường dẫn `{skill_dir}/references/orchestration.md`.
6. **Câu giới hạn:** `Routing is ADVISORY: these instructions guide the Leader; they do not enforce a single orchestrator.`
7. **Câu cho worker:** `Subagents/workers: these rules are Leader-only.`

- [ ] **Step 1: Viết test.** Gồm 4 test thay thế trong Bảng chuyển test, và:
  - `test_generated_commands_execute_with_special_paths`:
    - đường dẫn chứa `spaces apostrophe' $(echo SUBSTITUTED) Nguyễn`;
    - chép `scripts/` của repo, `platforms/claude-code.json` và `agents/roles/` vào một skill dir tạm;
    - tạo state bằng `ct_policy.initialize_state`;
    - chạy lệnh status lấy từ khối chỉ dẫn qua PowerShell hoặc `/bin/sh -c`, với `CLAUDE_CODE_SESSION_ID=fixture-shell`, kỳ vọng `ON`;
    - chạy mẫu lệnh với `'/coding-team off'`, kỳ vọng `OFF`;
    - gắn `@unittest.skipUnless((SCRIPTS/'coding_team.py').exists(), 'CLI added in Task 10')`.
  - `test_block_contents`: có `/coding-team on --global`, `/coding-team reset`, `Routing is ADVISORY`, `Leader-only`, `references/orchestration.md`; không có `on all` hay `inherit`.
  - `test_remove_block_preserves_rest`.
- [ ] **Step 2: Chạy test.** Kỳ vọng FAIL.
- [ ] **Step 3: Viết `scripts/ct_rules.py`.** Logic upsert chép từ `install_global_instructions` cũ.
- [ ] **Step 4: Chạy test.** Kỳ vọng PASS; test chạy lệnh được báo SKIP.
- [ ] **Step 5: Commit cục bộ** với message `feat: platform-aware global rules block with routing policy`.

### Task 7: Cài đặt và gỡ cài đặt theo giao dịch `ct_install`

**Files:**
- Create: `scripts/ct_install.py`
- Test: `tests/test_install.py`

**Interfaces:**
- Consumes: `ct_platform.*`, `ct_roles.load_roles`, `ct_roles.render_all`, `ct_rules.*`, `ct_policy.initialize_state`, `ct_storage.*`.
- Produces:
  - Các lớp lỗi (đều là `ValueError` và đều có `.paths: list[str]`): `UnmanagedTargetError`, `ModifiedTargetError`, `IncompleteTransactionError`. Ngoài ra `class InstallRolledBack(RuntimeError)`.
  - `@dataclass InstallPlan`:
    - `platform`, `scope`, `paths: dict`;
    - `files: list[tuple[str, str, bytes]]` (root label `'skill_dir'` hoặc `'agents_dir'`, đường dẫn tương đối POSIX, nội dung);
    - `rules_block: str | None`, `state_default: str | None`, `options: dict` (gồm `docs_mcp`).
  - `build_plan(source_root, cfg, *, scope, env, home, project_dir, default, global_rules, python, docs_mcp: list[str] = ()) -> InstallPlan`:
    - `scope == 'project'` kết hợp `default` hoặc `global_rules` thì raise `ValueError`;
    - `docs_mcp` khác rỗng trên nền tảng không phải `claude-code` thì raise `ValueError`;
    - các file của skill: `SKILL.md`, `LICENSE`, `references/**`, `agents/roles/**`, `scripts/*.py` (trừ `export_evaluation_inputs.py` và `build_runtime_package.py`), `platforms/<platform>.json`; riêng Codex thêm `agents/openai.yaml`.
  - `preview(plan) -> dict`: **chỉ đọc**; trả `would_write`, `would_backup`, `would_remove`, `conflicts: {'unmanaged': [...], 'modified': [...], 'linked': [...]}`, `journal_present: bool`.
  - `apply_plan(plan, *, dry_run: bool, replace_unmanaged: bool, overwrite_modified: bool) -> dict`. Kết quả gồm `dry_run`, `written`, `unchanged`, `removed_stale`, `kept_modified_stale`, `modified_overwritten`, `backup_dir`, `manifest`, `rules_file`, `state`.
  - `recover(cfg, *, scope, env, home, project_dir) -> dict`: trả `restored`, `deleted_new`, `journal_removed`.
  - `uninstall(cfg, *, scope, env, home, project_dir, dry_run, remove_modified) -> dict`: trả `removed`, `kept_modified`, `missing`, `rules_removed`, `backup_dir`.
  - Hằng số `MANIFEST_NAME = 'install-manifest.json'`.

**Manifest** (ở `<skill_dir>/install-manifest.json`, schema_version 1):
- `platform`, `scope`, `installed_at`, `options`;
- `files: {"skill_dir": {rel: sha256}, "agents_dir": {rel: sha256}}`;
- `rules_file: str | null`.

**Chính sách xử lý từng đích:**

| Tình trạng đích | Hành động |
|---|---|
| Chưa tồn tại | Tạo mới |
| Do coding-team quản lý, hash hiện tại bằng manifest, nội dung mới giống | Bỏ qua (`unchanged`) |
| Do coding-team quản lý, hash hiện tại bằng manifest, nội dung mới khác | Sao lưu rồi nâng cấp |
| Do coding-team quản lý, hash hiện tại **khác** manifest (người dùng đã sửa) | Raise `ModifiedTargetError`, trừ khi có `overwrite_modified` (khi đó sao lưu, thay, ghi vào `modified_overwritten`) |
| Tồn tại nhưng không do coding-team quản lý (hoặc `skill_dir` không có manifest) | Raise `UnmanagedTargetError`, trừ khi có `replace_unmanaged` (khi đó sao lưu rồi thay) |
| File cũ trong manifest nhưng không còn trong plan | Chưa sửa thì sao lưu rồi xoá; đã sửa thì giữ lại và ghi vào `kept_modified_stale` |
| Đích hoặc thư mục tổ tiên là liên kết | Raise `LinkedPathError` (không có cờ nào bỏ qua được) |

**Các bước của `apply_plan`:**
1. Nếu `dry_run`: trả `preview(plan)` kèm `dry_run: True`. **Dừng ở đây**, trước khi lấy khoá, tạo staging, backup hay journal.
2. Gọi `reject_links` cho mọi đích. Giữ `locked(lock_file, timeout=30)` trong suốt giao dịch.
3. Nếu `journal_file` tồn tại, raise `IncompleteTransactionError` kèm gợi ý `coding_team.py install --recover`.
4. Đánh giá xung đột theo bảng trên. Có xung đột chưa được cho phép thì raise; tới lúc này chưa ghi gì.
5. Dựng staging trong `backup_root/.staging-<ts>`. Sao lưu mọi đích sẽ bị thay hoặc xoá (kể cả file chỉ dẫn toàn cục) vào `backup_root/<ts>/`.
6. Ghi journal bằng `atomic_write`, gồm `started_at`, `backup_dir`, và `targets: [{path, existed, backup}]`.
7. Thay từng file bằng `atomic_write`, xoá các file cũ thuộc diện bị xoá, rồi `upsert_block` và `initialize_state`.
8. Ghi manifest (`installed_at` chỉ đổi khi có thay đổi), xoá journal, xoá staging.
9. Bất kỳ `Exception` nào ở bước 5–8: khôi phục theo journal (chép lại bản sao lưu, xoá file mới tạo), xoá journal, rồi raise `InstallRolledBack`.

Không có gì cần sao lưu thì không tạo thư mục backup (`backup_dir` là `None`).

**`uninstall`** (dùng chung bảng liên kết, journal và khoá):
- không có manifest thì raise `ValueError('not a coding-team managed installation: …')`;
- file có hash khớp thì xoá; file không khớp thì giữ lại, trừ khi có `remove_modified` (sao lưu trước);
- `remove_block` nếu manifest có `rules_file`;
- **không xoá state**;
- xoá thư mục `skill_dir` nếu đã trống.

- [ ] **Step 1: Viết test** trong `tests/test_install.py`. Mọi test dùng `home` và `project_dir` tạm, env tự dựng, `source_root` là repo.
  - `test_dry_run_changes_nothing`: chụp trạng thái cây (mọi file, kể cả file ẩn, và mọi thư mục, kèm hash) trước và sau. Chạy dry-run trên 3 nền tảng, ở hai tình huống: home trống; và home đã cài, có một file bị sửa (khi đó xung đột phải xuất hiện trong `conflicts`). Kỳ vọng cây giống hệt, không có `install.lock`, `.staging-*`, `backups` hay journal.
  - `test_install_layout_per_platform`:
    - Claude Code có `skills/coding-team/SKILL.md`, `agents/ct-architect.md`, `agents/ct-reviewer-verify.md`;
    - Codex có `agents/ct-architect.toml` và `.agents/skills/coding-team/agents/openai.yaml`;
    - Antigravity có `config/agents/ct-architect/agent.md`;
    - skill dir chỉ có một file trong `platforms/`, và có `agents/roles/roles.json`.
  - `test_config_root_env_respected`: `CLAUDE_CONFIG_DIR=<tmp>/alt dir`.
  - `test_second_install_is_noop`: `written == []`, manifest giống hệt từng byte, `backup_dir is None`.
  - `test_upgrade_unmodified_file_backs_up`: cài; đổi thân `ct-architect.md` trong bản sao `source_root`; cài lại thì file được nâng cấp và bản cũ nằm trong backup.
  - `test_modified_file_conflict_by_default`: cài; người dùng sửa `agents/ct-architect.md`; cài lại thì raise `ModifiedTargetError` có đường dẫn đó, và **không file nào thay đổi**. Có `overwrite_modified=True` thì thành công, bản sửa nằm trong backup, đường dẫn nằm trong `modified_overwritten`.
  - `test_unmanaged_target_requires_flag`: có sẵn `skill_dir/SKILL.md` thì raise `UnmanagedTargetError`; có `replace_unmanaged=True` thì thành công, bản cũ nằm trong backup.
  - `test_linked_target_refused` (Review Focus 5): `agents_dir` là symlink hoặc junction trỏ ra một thư mục ngoài thì raise `LinkedPathError`, thư mục ngoài không bị đổi. Không tạo được liên kết thì `skipTest`.
  - `test_rollback_on_exception`: lần cài thứ hai dùng nội dung khác. Patch `ct_install.atomic_write` để raise `OSError` ở **lần ghi thứ 2 vào một file đích của plan** (không tính journal, backup hay manifest). Kỳ vọng `InstallRolledBack`; cây giống hệt sau lần cài đầu; không còn journal hay staging.
  - `test_killed_process_leaves_journal_then_recover`: chạy cài trong subprocess với một script nhỏ patch `ct_install.atomic_write` để gọi `os._exit(1)` ở lần ghi thứ 2 vào một file đích của plan, tức là sau khi journal đã được ghi. Sau đó: journal còn; `apply_plan` lần sau raise `IncompleteTransactionError`; `recover(...)` đưa cây về trạng thái trước lần cài bị tắt và xoá journal; cài lại thì thành công.
  - `test_concurrent_install_waits_for_lock`: tiến trình con giữ `locked(lock_file)` 2 giây; `apply_plan` hoàn tất sau khi khoá được nhả.
  - `test_scope_rules`: project kết hợp `default` raise; project kết hợp `global_rules` raise; cài theo project không đụng `state_file` hay `global_rules_file`.
  - `test_default_and_state_preservation`.
  - `test_global_rules_migration`: `AGENTS.md` có khối marker cũ chứa `/coding team on all`; sau khi cài có `global_rules`, khối được thay đúng một lần, byte ngoài marker giữ nguyên.
  - `test_docs_mcp_only_claude`: `docs_mcp=['mcp__plugin_context7_context7__query-docs']` với Claude Code thì `ct-researcher.md` chứa công cụ đó và manifest `options` ghi lại; với Codex thì raise `ValueError`.
  - `test_uninstall_keeps_modified`, `test_uninstall_unmanaged_refused`.
  - `test_home_with_special_characters` (Review Focus 1).
- [ ] **Step 2: Chạy test.** Kỳ vọng FAIL.
- [ ] **Step 3: Viết `scripts/ct_install.py`.** Import `from ct_storage import atomic_write, locked, reject_links` vào namespace của module, để test patch được.
- [ ] **Step 4: Chạy** `python -m unittest tests.test_install -v` cùng toàn bộ test. Kỳ vọng PASS.
- [ ] **Step 5: Commit cục bộ** với message `feat: transactional installer with conflict policy, journal and recovery`.

### Task 8: Checkpoint B — tích hợp chính sách và trình cài đặt, kèm review an toàn ghi file (sau Task 5–7)

**Files:**
- Create: `tests/test_integration_install.py`

**Interfaces:**
- Consumes: `ct_policy`, `ct_rules`, `ct_install`, `ct_roles`, `ct_platform`.

- [ ] **Step 1: Viết test:**
  - `test_full_cycle_each_platform`: với từng nền tảng: chụp trạng thái cây, rồi `install` (user scope, `global_rules`, `default='off'`). Kiểm tra:
    - file agent trên đĩa giống hệt từng byte với `render_all`;
    - state có global là `OFF`;
    - khối chỉ dẫn có đúng một khối.

    Sau đó `uninstall`; cây trở về trạng thái ban đầu, chỉ còn khác ở `state_file` và thư mục `backups/`.
  - `test_rules_block_points_to_installed_cli`: đường dẫn CLI trong khối chỉ dẫn là `<skill_dir>/scripts/coding_team.py`, và state trong khối khớp `target_paths()['state_file']`.
  - `test_project_and_user_installs_coexist`: cài user scope rồi cài project scope cho cùng nền tảng; hai manifest độc lập; gỡ bản project không ảnh hưởng bản user.
- [ ] **Step 2: Chạy toàn bộ test.** Kỳ vọng PASS.
- [ ] **Step 3: Review an toàn ghi file.** Controller dispatch một reviewer riêng (tầng `strong`) đọc `ct_install.py`, `ct_rules.py`, `ct_storage.py` với trọng tâm:
  - mọi đường ghi có đi qua `reject_links` không;
  - dry-run có hoàn toàn chỉ đọc không;
  - rollback và journal có đúng trên Windows không (file đang mở, `os.replace` qua ổ đĩa);
  - có bỏ sót việc sao lưu trước khi xoá hay ghi không;
  - xử lý `PermissionError`;
  - hành vi khi `backup_root` nằm trên ổ khác.

  Mọi phát hiện Critical hoặc Important phải được sửa trong task gốc trước khi push.
- [ ] **Step 4: Commit cục bộ** (`test: policy and installer integration checkpoint`). Sau khi review đạt, Controller push nhánh.

### Task 9: Ownership điều phối, phân luồng tất định và bàn giao `ct_orchestration`

**Files:**
- Create: `scripts/ct_orchestration.py`
- Test: `tests/test_orchestration.py`

**Interfaces:**
- Consumes: `ct_storage.locked`, `ct_storage.atomic_write`.
- Produces:
  - `ORCHESTRATORS = ('leader-direct', 'coding-team', 'superpowers-sdd', 'superpowers-dpa', 'superpowers-executing-plans')`.
  - `class OrchestrationConflict(ValueError)`, có `.blockers: list[str]`.
  - `orchestration_file(workspace: Path) -> Path`: `<worktree chính>/.coding-team/orchestration.json`. Worktree chính lấy từ mục `worktree` đầu tiên của `git -C <workspace> worktree list --porcelain`. Nếu không phải repo git thì dùng `<workspace>/.coding-team/orchestration.json`.
  - `unresolved_sdd_workspaces(workspace: Path) -> list[str]`: các đường dẫn tuyệt đối dạng POSIX của `.superpowers/sdd/*/progress.md`, quét **mọi** worktree.
  - `status(workspace, task_id: str | None = None) -> dict`: trả `tasks` (mọi record), `unresolved_sdd` (đã trừ các mục được record của `task_id` acknowledge), `can_claim: bool`, `blockers`, và `note` với nội dung cố định `"unresolved SDD workspaces indicate unfinished progress, not a running process"`.
  - `claim(workspace, task_id, mode, phase) -> dict`: raise `OrchestrationConflict` khi task đã có owner khác `coding-team`, hoặc còn workspace SDD chưa được acknowledge. Claim lại một task mà `coding-team` đang giữ thì chỉ cập nhật `mode` và `phase`.
  - `validate_handoff(record: dict, current_owner: str | None, unresolved: list[str]) -> None`.
  - `handoff(workspace, record) -> dict`: trả về record kèm `termination_evidence: 'caller-asserted'`, `from_verified: bool`.
  - `release(workspace, task_id) -> dict`.
  - `route(facts: dict) -> dict`: trả `{'orchestrator', 'rule': int, 'reason': str}`.

**File state:** `{"schema_version": 1, "tasks": {task_id: {"orchestrator", "mode", "phase", "handoff_from", "handoff_record", "acknowledged_sdd": []}}}`. Mọi lần ghi giữ `locked(orchestration_file)`. Lần đầu tạo `.coding-team/.gitignore` với nội dung `*\n`.

**Bàn giao:**
- Các trường bắt buộc giữ như spec §6.5:
  - `task_id`, `reason`, `from`, `to` (thuộc `ORCHESTRATORS`, và `from` khác `to`);
  - `workers[{id, terminated is True, evidence không rỗng}]`;
  - `snapshot{head, uncommitted, untracked}`;
  - `tests{commands, result, snapshot}`;
  - `findings`, `previous_ledger`, `acknowledged_sdd`.
- Kiểm tra thêm:
  - nếu task đã có record thì `from` phải bằng owner của record, ngược lại raise; khi đó `from_verified = True`;
  - nếu task chưa có record thì `from_verified = False`;
  - mọi mục của `acknowledged_sdd` phải nằm trong `unresolved`.

**Fact của `route`** (bắt buộc đủ):
- `level` ∈ `L0..L3`;
- `user_choice` (thuộc `ORCHESTRATORS` hoặc null), `safety_forbids_user_choice` (bool);
- `architecture_dispute`, `multi_contract_change`, `has_plan`, `sequential_review_sufficient`, `superpowers_available` (bool);
- `independent_issues` (int ≥ 0).

**Thứ tự quyết định của `route`:**
1. `user_choice` có giá trị và không bị chính sách an toàn cấm → `user_choice` (rule 0).
2. `L3` → `coding-team`.
3. `architecture_dispute` → `coding-team`.
4. `multi_contract_change` → `coding-team`.
5. `independent_issues >= 2` → `superpowers-dpa` nếu có Superpowers, ngược lại `coding-team`.
6. `has_plan` và `sequential_review_sufficient` → `superpowers-sdd` nếu có Superpowers, ngược lại `coding-team`.
7. `L0` hoặc `L1` → `leader-direct`.
8. Còn lại → `coding-team`.

- [ ] **Step 1: Viết test** trong `tests/test_orchestration.py`:
  - `test_unresolved_sdd_blocks_claim_and_is_not_called_running`: có `progress.md` thì `can_claim` là `False` và có `note`; `claim` raise.
  - `test_ownership_shared_across_worktrees`:
    - tạo repo git tạm, `git worktree add ../wt-b`;
    - claim `task-1` ở worktree chính, rồi `status(wt-b, 'task-1')` thấy owner;
    - tạo SDD trong `wt-b`, rồi `claim` `task-2` ở worktree chính bị chặn;
    - file nằm ở `<main>/.coding-team/orchestration.json`, **không nằm trong `.git/`**;
    - máy không có `git` thì `skipTest`.
  - `test_non_git_workspace_uses_local_dir`.
  - `test_handoff_validation`:
    - thiếu `evidence`, `terminated: False`, hoặc thiếu `snapshot.untracked` đều raise, và thông báo có tên trường;
    - `from` không khớp owner hiện tại thì raise;
    - acknowledge một đường dẫn không nằm trong `unresolved` thì raise.
  - `test_valid_handoff_then_claim`: bàn giao từ SDD sang coding-team kèm acknowledge, sau đó `claim` thành công; kết quả có `termination_evidence == 'caller-asserted'` và `from_verified is False`.
  - `test_route_order`: mỗi rule 0–8 có một ca; thêm ca "đổi tên trên 10 file" (`L1`, mọi bool là `False`) cho `leader-direct`, và ca "xác thực 2 file" (`L3`) cho `coding-team`; `superpowers_available=False` thì các rule 5 và 6 trả `coding-team`; fact thiếu khoá thì raise `ValueError`.
  - `test_gitignore_and_release`.
- [ ] **Step 2: Chạy test.** Kỳ vọng FAIL.
- [ ] **Step 3: Viết `scripts/ct_orchestration.py`.** Gọi `git` qua `subprocess.run([...], capture_output=True, text=True, timeout=10)`. Lệnh trả mã khác 0 thì coi như không phải repo git.
- [ ] **Step 4: Chạy test.** Kỳ vọng PASS.
- [ ] **Step 5: Commit cục bộ** với message `feat: repo-scoped orchestration ownership, routing and handoff`.

### Task 10: CLI `coding_team.py`, `doctor`, và gỡ các script cũ

**Files:**
- Create: `scripts/coding_team.py`, `tests/test_cli.py`
- Delete (chỉ sau Step 4): `scripts/coding_team_policy.py`, `scripts/check_model_profiles.py`, `references/model-profiles.json`, `tests/test_policy_helper.py`, `tests/test_model_profiles.py`

**Interfaces:**
- Consumes: toàn bộ các module ở Task 1–9.
- Produces:
  - `main(argv=None) -> int`. Luôn in đúng một object JSON. Exit code: `0` thành công hoặc khoẻ; `1` hợp lệ nhưng kết quả âm (doctor có vấn đề, `orchestrator status` không claim được); `2` lỗi (kể cả các lỗi xung đột của Task 7).
  - Các subcommand:
    - `install --platform P [--scope user|project] [--project-dir D] [--default on|off] [--global-rules] [--docs-mcp NAME]... [--dry-run] [--replace-unmanaged] [--overwrite-modified] [--recover]`. Khi có `--recover` thì gọi `recover` và bỏ qua các cờ khác, trừ platform, scope và project-dir.
    - `uninstall --platform P [--scope …] [--project-dir D] [--dry-run] [--remove-modified]`
    - `status [--platform P] [--chat-id ID] [--state F]`
    - `command TEXT [--platform P] [--chat-id ID] [--state F]`. Lỗi cú pháp cũ trả JSON có `hint`.
    - `doctor [--platform P] [--scope …] [--project-dir D]`
    - `check-config [--platform P | --input F]`
    - `orchestrator {status|claim|handoff|release|route} [--workspace W] [--task-id T] [--mode M] [--phase PH] [--input F]`
  - `resolve_platform(arg, env) -> str`. Thứ tự: tham số; `install-manifest.json` ở `Path(__file__).parents[1]`; `detect_platform(env)`; không xác định được thì raise `ValueError('cannot determine platform; pass --platform')`.
  - `doctor(cfg, *, scope, env, home, project_dir) -> dict`. Khoá:
    - `manifest`, `modified_files`, `missing_files`, `journal_present`;
    - `agents: {variant: {present, requested_tier, model_in_file, guarantees}}` (guarantees tính kèm `options.docs_mcp` trong manifest);
    - `force_model_env_active`;
    - `state`, `rules_block`;
    - `superpowers: {installed: 'found'|'not_found', enabled: true|false|null, available_in_session: 'determined by Leader at runtime'}`;
    - `healthy`.
  - Dò Superpowers: tìm `**/skills/subagent-driven-development/SKILL.md` dưới `config_root` và `{home}/.agents/skills`. Với Claude Code, `enabled` lấy từ `settings.json` → `enabledPlugins`: khoá có chứa `superpowers` cho `true`/`false`, không có thì `null`.

- [ ] **Step 1: Viết test** trong `tests/test_cli.py`. Gọi bằng subprocess; env có `HOME` và `USERPROFILE` trỏ vào thư mục tạm, đồng thời xoá các biến nhận diện nền tảng.
  - `test_install_status_command_roundtrip`: `install --default on`; `status` với `CLAUDE_CODE_SESSION_ID=s1` cho `ON`; `command '/coding-team off'` cho `OFF`; `command '/coding-team on all'` cho exit 2 và có `hint`.
  - `test_ambiguous_platform_detection` (Review Focus 3).
  - `test_installed_copy_infers_platform_and_doctor_runs`: chạy `<skill_dir>/scripts/coding_team.py status`, rồi `doctor` không kèm `--platform`; exit 0, và `platform == 'claude-code'`.
  - `test_chat_unknown_status_and_global_still_works`.
  - `test_doctor_reports_guarantees_force_env_and_journal`:
    - `no_file_write` của `ct-architect` là `ENFORCED`, `no_delegate` của `ct-developer` là `ADVISORY`;
    - có biến FORCE thì biến đó xuất hiện trong `force_model_env_active`;
    - xoá một file agent thì exit 1;
    - có journal còn sót thì `journal_present: true` và exit 1.
  - `test_install_conflict_exit_codes`: file bị sửa cho exit 2 và JSON liệt kê đường dẫn; thêm `--overwrite-modified` thì exit 0; `--dry-run` cho exit 0 kèm `conflicts`.
  - `test_check_config`: hợp lệ cho exit 0; tầng lạ cho exit 2; file thiếu cho exit 2 và không tạo file nào.
  - `test_orchestrator_cli`: `status` khi có SDD dở cho exit 1; `handoff --input` cho exit 0; `claim` cho exit 0; `route --input facts.json` trả `orchestrator`.
- [ ] **Step 2: Chạy test.** Kỳ vọng FAIL.
- [ ] **Step 3: Viết `scripts/coding_team.py`.** Ngay đầu file, chèn thư mục của chính nó vào `sys.path`.
- [ ] **Step 4: Đối chiếu Bảng chuyển test.** Với mỗi test thay thế trong bảng, chạy `python -m unittest tests.<module>.<Class>.<test> -v` và xác nhận PASS.
- [ ] **Step 5: Xoá 5 file cũ.** Chạy toàn bộ test; kỳ vọng PASS, và test chạy lệnh ở Task 6 giờ chạy thật.
- [ ] **Step 6: Commit cục bộ** với message `feat: unified coding_team CLI with doctor; remove Codex-only helpers`.

### Task 11: Checkpoint C — tích hợp đầu-cuối của CLI (sau Task 9–10)

**Files:**
- Create: `tests/test_integration_cli.py`

- [ ] **Step 1: Viết test** (qua subprocess CLI):
  - `test_two_sessions_concurrently`: 10 tiến trình song song đổi xen kẽ hai mã chat `s1` và `s2`, kèm một lệnh `--global`. Cuối cùng cả hai chat override và global đều đúng với lệnh cuối cùng của từng nguồn, không mất cập nhật nào.
  - `test_orchestration_across_worktrees_via_cli`: dùng repo git tạm có 2 worktree; `orchestrator claim` ở A, rồi `orchestrator status --task-id` ở B thấy owner, và `claim` cùng `task_id` với owner khác bị từ chối (exit 2).
  - `test_corrupt_files_fail_closed`: `state.json` hỏng thì `status` exit 2 và byte của file không đổi; `orchestration.json` hỏng thì `orchestrator status` exit 2 và byte không đổi; `install-manifest.json` hỏng thì `doctor` exit 1 với `manifest: 'invalid'`.
  - `test_install_from_extracted_runtime_package`: `build_runtime_package` (tạm thời gọi trực tiếp module) → giải nén → chạy `install` từ thư mục giải nén thành công. Nếu chưa có thư mục `platforms` trong gói thì `expectedFailure`; Task 14 sẽ bỏ đánh dấu này.
- [ ] **Step 2: Chạy toàn bộ test.** Kỳ vọng PASS (trừ `expectedFailure` đã nêu).
- [ ] **Step 3: Commit cục bộ** (`test: CLI end-to-end integration checkpoint`). Sau khi review đạt, Controller push nhánh.

### Task 12: Viết lại SKILL.md và references

**Files:**
- Modify: `SKILL.md`, `references/agent-contracts.md`, `references/model-routing.md`, `references/control-policy.md`, `references/workflow.md`, `references/debate-governance.md`, `references/quality-safety.md`, `references/task-ledger.md`
- Create: `references/orchestration.md`
- Không sửa: `references/governance-defaults.json`

Yêu cầu nội dung (mỗi dòng là một quyết định phải có mặt):
- **SKILL.md** (dưới khoảng 90 dòng):
  - không có chữ `Codex` hay `CODEX_`;
  - mục đầu "Choose the orchestrator": bảng §6.2 rút gọn, link `references/orchestration.md`, và lệnh `orchestrator route`;
  - bảng L0–L3, trong đó L1 là Leader làm trực tiếp;
  - các chế độ giữ nguyên;
  - eligibility qua `scripts/coding_team.py` và 7 lệnh;
  - bảng 6 vai trò cùng `ct-reviewer-verify`;
  - câu "default tiers come from the installed `platforms/<platform>.json`; Leader may raise or lower them";
  - câu "report ENFORCED/ADVISORY honestly; never present ADVISORY as a guarantee";
  - cấm lồng agent, kể cả qua shell;
  - các cổng duyệt giữ nguyên, thêm điều kiện L3 của `ct-security`.
- **agent-contracts.md:**
  - handoff thêm: role/variant ID, Preferred Tier, Requested Model, guarantees thực tế, skill kỹ thuật được phép (gọi tên), skill điều phối bị cấm, lựa chọn isolation và lý do;
  - contract riêng từng vai trò thay bằng link tới `../agents/roles/ct-*.md`;
  - handoff evidence thêm `Effective Model` (hoặc `unverified`).
- **model-routing.md:** spec §5.3: ba khái niệm, `static_agent_model` (Codex `omit` và lý do), thứ tự ưu tiên, leo thang sau 1 vòng sửa thất bại, biến ép model, STRICT_MODEL, câu "valid JSON is not evidence routing works".
- **control-policy.md:**
  - 7 lệnh, đường dẫn state theo nền tảng (trỏ tới `platforms/*.json`), hợp đồng mã chat;
  - `coding_team.py` gồm `--dry-run`, `--overwrite-modified`, `--replace-unmanaged`, `--recover`, `--docs-mcp`;
  - phần chuyển từ bản cài cũ, bọc trong `<!-- legacy-migration -->`.
- **orchestration.md** (mới): spec §6.1–6.6, gồm:
  - nêu rõ ADVISORY và giới hạn;
  - bảng có thứ tự;
  - `route` và các fact;
  - ví dụ "đổi tên trên 10 file" và "xác thực 2 file";
  - phân loại skill;
  - coding-team giữ cả task và dùng spec/plan của Superpowers;
  - ownership theo repo và vị trí file (không nằm trong `.git/`);
  - unresolved SDD ≠ tiến trình đang chạy;
  - các trường bàn giao, kèm câu "termination evidence is caller-asserted: verify with runtime tools before recording";
  - các lệnh CLI;
  - fallback khi không có Superpowers;
  - câu "`doctor` cannot know `available_in_session`".
- **workflow.md, debate-governance.md, quality-safety.md:**
  - đổi tên vai trò;
  - quy tắc isolation theo mục đích (ba trường hợp ở mục "Quy trình thực thi");
  - quality-safety thêm quy tắc Gate 2 L3 §6.7.
- **task-ledger.md:** ánh xạ `Architect=ct-architect`, `Developer=ct-developer`, `QA=ct-reviewer`; định dạng ledger không đổi.

- [ ] **Step 1: Viết lại các file.**
- [ ] **Step 2: Kiểm tra link thủ công** bằng lệnh một dòng ở bản 1 (quét `](…)` tương đối trong `SKILL.md` và `references/*.md`). Kỳ vọng không in ra gì.
- [ ] **Step 3: Chạy toàn bộ test.** Kỳ vọng PASS.
- [ ] **Step 4: Commit cục bộ** với message `docs: multi-platform roles, model routing and orchestration policy`.

### Task 13: Fixture phân luồng, fact tất định và test nhất quán tài liệu

**Files:**
- Create: `tests/orchestration_cases.json`, `tests/orchestration_facts.json`, `tests/test_orchestration_fixture.py`, `tests/test_skill_docs.py`

**Interfaces:**
- Consumes: `check_behavioral_results.check_results` (không sửa), `ct_orchestration.route`, `ct_orchestration.ORCHESTRATORS`, `ct_roles.load_roles`.
- Produces:
  - `orchestration_cases.json`: cùng lược đồ với `mode_cases.json`, có thêm các trường được kiểm tra: `orchestrator`, `accept_task`, `handoff_valid`, `gate2_pass`. Dùng cho đánh giá hành vi **mù** trên runtime thật (Task 14 và 15).
  - `orchestration_facts.json`: map `case_id → facts`, chỉ cho các ca có kiểm tra `orchestrator`.

Ba loại kiểm thử được tách bạch, và docstring của mỗi test phải nói rõ loại của nó:
- **(a) Fixture và schema:** fixture không tự mâu thuẫn với checker.
- **(b) Logic Python:** `route(facts)` cho đúng kỳ vọng.
- **(c) Hành vi agent:** không chạy trong CI; thuộc smoke và đánh giá ở Task 14 và 15.

Các ca bắt buộc giữ nguyên bảng của bản 1:
- `rename_symbol_10_files`, `auth_change_2_files_l3`, `planned_low_risk_sequential`, `planned_low_risk_no_superpowers`, `three_independent_failures`, `architecture_dispute`, `user_names_orchestrator`;
- `active_sdd_workspace`: đổi kỳ vọng thành `accept_task is False`, `action == 'hold'`, `ask_user is True` (vì đây là tiến độ chưa giải quyết, không phải tiến trình đang chạy);
- `handoff_missing_worker_evidence`, `worker_requests_sdd`, `l3_missing_security_report`, `l3_security_report_stale_snapshot`.

- [ ] **Step 1: Viết `tests/test_orchestration_fixture.py`:**
  - `test_fixture_shape_and_unique_ids` (a): id không trùng, phép kiểm tra hợp lệ, `orchestrator` thuộc `ORCHESTRATORS`, đủ 12 id.
  - `test_golden_responses_satisfy_checker` (a): docstring ghi `"Validates fixture/checker consistency only; does not evaluate agent behaviour."`.
  - `test_route_matches_fixture_expectations` (b): với mỗi id trong `orchestration_facts.json`, `route(facts)['orchestrator']` bằng giá trị `equals.orchestrator` của fixture.
  - `test_facts_cover_every_orchestrator_case` (b): mọi ca có kiểm tra `orchestrator` đều có facts.
- [ ] **Step 2: Viết `tests/test_skill_docs.py`:**
  - `test_role_ids_documented`;
  - `test_no_legacy_command_syntax` (loại trừ khối `<!-- legacy-migration -->`);
  - `test_skill_is_platform_neutral`;
  - `test_relative_links_resolve` (cho `SKILL.md`, `references/*.md`, `agents/roles/*.md`, `README.md`);
  - `test_orchestration_doc_states_advisory_and_limits`: có `ADVISORY`, `caller-asserted`, `not a running process`, và các cụm `L3`, `architect`, `interface`, `independent`, `plan`, `small` xuất hiện theo thứ tự tăng dần.
- [ ] **Step 3: Viết hai file JSON.**
- [ ] **Step 4: Chạy toàn bộ test.** Kỳ vọng PASS (test tài liệu FAIL thì sửa ở Task 12).
- [ ] **Step 5: Commit cục bộ** với message `test: orchestration fixture, deterministic routing facts and doc checks`.

### Task 14: README, CONTRIBUTING, gói runtime, trình export và bộ smoke test

**Files:**
- Modify: `README.md`, `CONTRIBUTING.md`, `scripts/build_runtime_package.py`, `scripts/export_evaluation_inputs.py`, `tests/test_runtime_package.py`, `tests/test_evaluation_export.py`, `tests/test_integration_cli.py` (bỏ `expectedFailure`)
- Create: `docs/smoke/platform-smoke.md`, `docs/smoke/result-template.json`

**Interfaces:**
- Produces:
  - `build_package(source_root, output=None, platform: str | None = None)`: tài nguyên đóng gói gồm thêm `platforms`; khi có `platform`, chỉ giữ `platforms/<platform>.json`. Có thêm tham số CLI `--platform`.
  - `export_inputs` chép thêm thư mục `platforms`.

Yêu cầu nội dung:
- **README:**
  - tên repo `chauvbm/coding-team`, giới thiệu cho 3 nền tảng;
  - mỗi nền tảng một khối lệnh cài: clone, `install --dry-run`, rồi `install --platform <p> [--default on|off] [--global-rules]`;
  - Claude Code có ví dụ `--docs-mcp` cho Context7;
  - chính sách xung đột và các cờ; `--recover`;
  - phần chuyển từ bản cài cũ, bọc trong `<!-- legacy-migration -->`;
  - bảng lệnh;
  - bảng mức bảo đảm theo nền tảng (gồm câu "shell access makes no-delegation ADVISORY");
  - tóm tắt phân luồng; `doctor`.
- **CONTRIBUTING:**
  - zip đổi tên thành `coding-team.zip`;
  - `--platform`;
  - smoke test và đánh giá hành vi chạy ngoài CI;
  - quy trình nhánh, review rồi mới push.
- **docs/smoke/platform-smoke.md:**
  - checklist §9B cho mỗi nền tảng: dispatch từng vai trò, ghi đè model, cách ly, không lồng agent (gồm cả thử khởi chạy AI CLI qua shell ở vai trò có shell, kỳ vọng worker từ chối theo WORKER_CLAUSE và báo ADVISORY), lỗi runtime;
  - **đánh giá phân luồng mù:** `export_evaluation_inputs.py --cases tests/orchestration_cases.json`, chạy evaluator mới trên runtime, chấm bằng `check_behavioral_results.py --cases tests/orchestration_cases.json`;
  - cài vào dự án tạm bằng `--scope project`;
  - nơi lưu `evaluations/<YYYY-MM-DD>-platform-smoke/<platform>.json`.
- **result-template.json:**
  - `platform`, `runtime_version`, `date`;
  - `checks` gồm `dispatch`, `model_override`, `isolation`, `no_nesting`, `runtime_failure`, `routing_eval`; mỗi mục có `status` (`PASS`, `FAIL` hoặc `NOT_RUN`), `evidence`, và `reason_if_not_run`.

- [ ] **Step 1: Cập nhật test** `test_runtime_package.py`:
  - `setUp` tạo `platforms/a.json`, `platforms/b.json`;
  - kỳ vọng thêm có hai file đó;
  - thêm `test_platform_filter_keeps_one_config`.

  Cập nhật `test_evaluation_export.py` để chép cả `platforms`. Bỏ `expectedFailure` ở Task 11.
- [ ] **Step 2: Chạy các test đó.** Kỳ vọng FAIL.
- [ ] **Step 3: Sửa hai script; viết README, CONTRIBUTING, docs/smoke.**
- [ ] **Step 4: Chạy toàn bộ test.** Kỳ vọng PASS.
- [ ] **Step 5: Commit cục bộ** với message `docs: multi-platform README and smoke/evaluation kit; package platforms`.

### Task 15: Kiểm chứng cuối, release gate và merge

**Files:**
- Create (theo kết quả): `evaluations/2026-11-13-platform-smoke/<platform>.json`

- [ ] **Step 1:** `python -m unittest discover -s tests -p "test_*.py" -v` và `git diff --check main...HEAD`. Kỳ vọng mọi test PASS (các SKIP có sẵn từ trước vẫn được phép), không có lỗi khoảng trắng.
- [ ] **Step 2: Final review toàn nhánh** (do SDD đảm nhiệm), đối chiếu với spec bản 2 và mọi mục trong Global Constraints.
- [ ] **Step 3: Dry-run chỉ đọc trên máy thật:** `python scripts/coding_team.py install --platform claude-code --dry-run --global-rules`. Xác nhận các đích nằm dưới `%USERPROFILE%\.claude`, và cây thư mục không đổi (so sánh danh sách file trước và sau trong `.claude/coding-team`). Báo kết quả cho người dùng.
- [ ] **Step 4: Smoke test và đánh giá Claude Code.** **Hỏi người dùng trước**, vì việc này tốn token và chạy một phiên `claude -p` lồng nhau. Cài `--scope project` vào một thư mục tạm, làm theo `docs/smoke/platform-smoke.md`, ghi `evaluations/2026-11-13-platform-smoke/claude-code.json`.
- [ ] **Step 5: Release gate.**
  - Với Codex và Antigravity: người dùng tự chạy checklist rồi gửi kết quả, hoặc **xác nhận rõ ràng** chấp nhận merge khi các mục đó là `NOT_RUN`.
  - Ghi `evaluations/2026-11-13-platform-smoke/<platform>.json` cho cả 3 nền tảng; mục không chạy thì `status: NOT_RUN` kèm lý do.
  - Commit cục bộ (`test: record platform smoke results`), rồi Controller push nhánh.
- [ ] **Step 6: Merge**, chỉ khi người dùng xác nhận: `git checkout main && git merge --ff-only feat/multi-platform && git push origin main`.
- [ ] **Step 7: Cập nhật graph.** `graphify update .`, xác nhận `graphify-out/graph.json` đọc được. Kiểm tra CI bằng `gh run list --branch main --limit 1`, kỳ vọng `completed success`.
