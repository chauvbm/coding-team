# coding-team đa nền tảng và phân luồng Superpowers — Kế hoạch triển khai

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Mục tiêu:** Từ một mã nguồn, cài được coding-team cho Claude Code, Codex và Antigravity. Bộ cài sinh agent tĩnh cho 6 vai trò, đi kèm bộ lệnh `/coding-team` mới và chính sách phân luồng một-bộ-điều-phối với Superpowers.

**Kiến trúc:**
- Lõi dùng chung gồm `SKILL.md`, `references/` và các module Python trong `scripts/`.
- Mỗi nền tảng có một file dữ liệu adapter `platforms/<tên>.json`.
- Một CLI duy nhất `scripts/coding_team.py` điều phối các module:
  - `ct_storage`: khoá file và ghi nguyên tử.
  - `ct_platform`: cấu hình nền tảng và đường dẫn.
  - `ct_roles`: vai trò, năng lực, sinh agent.
  - `ct_policy`: state và lệnh bật/tắt.
  - `ct_rules`: khối chỉ dẫn toàn cục.
  - `ct_install`: cài và gỡ theo kiểu giao dịch.
  - `ct_orchestration`: metadata điều phối và bàn giao.

**Tech Stack:** Python 3.11+ chỉ dùng thư viện chuẩn (`json`, `tomllib`, `hashlib`, `argparse`, `unittest`), Markdown, JSON.

**Spec:** `docs/superpowers/specs/2026-11-13-multi-platform-and-orchestration-design.md` (bản 2, đã duyệt). Người thực thi phải đọc cả spec lẫn kế hoạch này.

## Global Constraints

- Python 3.11 trở lên, chỉ dùng thư viện chuẩn. CI giữ ma trận Ubuntu/Windows × Python 3.11/3.13; lệnh test là `python -m unittest discover -s tests -p "test_*.py" -v`.
- File `.md` sinh ra hoặc ghi ra phải là UTF-8 **không BOM**, dùng LF.
- Không đổi định dạng ledger. Vai trò cổng duyệt logic vẫn là `Architect`, `Developer`, `QA`.
- **Không sửa:**
  - `benchmarks/` và `evaluations/`.
  - Các fixture đang được replay: `tests/behavioral_cases.json`, `tests/sequential_cases.json`, `tests/reviewer_transfer_cases.json`, `tests/mode_cases.json`, `tests/realistic_cases_v2.json`, `tests/realistic_cases_v3.json`.
  - `tests/check_behavioral_results.py`.
- Không thêm hook vào settings của người dùng.
- Không ghi ra ngoài các marker `<!-- coding-team-policy:start -->` / `<!-- coding-team-policy:end -->` trong file chỉ dẫn toàn cục.
- Mọi lần ghi ra ngoài repo đều phải sao lưu trước.
- Chỉ được ghi `ENFORCED` khi có cơ chế runtime tương ứng. Vai trò nào có shell thì `no_file_write` không bao giờ là `ENFORCED`. `write:assigned` và `write:tests` luôn là `ADVISORY`.
- ID vai trò: `ct-architect`, `ct-researcher`, `ct-developer`, `ct-test-engineer`, `ct-reviewer`, `ct-security`. `ct-reviewer` có thêm biến thể `ct-reviewer-verify`.
- Tầng model: `inherit`, `strong`, `standard`, `light`.
- Nền tảng: `claude-code`, `codex`, `antigravity`.
- Cú pháp lệnh duy nhất là `/coding-team status|on|off|on --global|off --global|reset|help`; trên Codex chấp nhận thêm tiền tố `$coding-team`. Cú pháp cũ (`/coding team …`, `on all`, `off all`, `inherit`) bị từ chối, kèm gợi ý cú pháp mới.
- State bật/tắt luôn nằm ở cấp người dùng. Không có nguồn mã chat tin cậy thì chat là `UNKNOWN`, không được đổi trạng thái chat. Không bao giờ đoán mã chat.
- Theo yêu cầu của chủ repo, sau **mỗi task**: commit vào `main` rồi `git push origin main`. Commit message kết thúc bằng dòng `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Quyết định của kế hoạch (bổ sung cho spec)

- Tầng mặc định của vai trò chỉ nằm trong `platforms/*.json`, mục `roles`. File `agents/roles/roles.json` chỉ chứa năng lực, không chứa tầng, để mỗi giá trị có một nguồn duy nhất.
- File agent tĩnh không ghi `isolation: worktree`. Khi chỉ kiểm chứng một snapshot đã commit, Leader truyền `isolation` lúc dispatch. Nếu ghi cố định vào file, mọi lần kiểm chứng sẽ bỏ sót thay đổi chưa commit.
- `backups/` và `install.lock` nằm cạnh `state_file` (trong `state_file.parent`).
- `--scope project` không được kết hợp với `--default`, cũng không được kết hợp với `--global-rules`.
- Một workspace SDD được coi là đang dở khi tồn tại file `.superpowers/sdd/*/progress.md`, vì SDD tự xoá workspace sau final review.
- Antigravity: không khai báo `mcpServers`. `inheritMcp: true` chỉ cho vai trò có `docs_lookup` hoặc `browser_testing`. Việc tắt công cụ Playwright không an toàn được ghi là `ADVISORY` trong contract.

## Review Focus

1. **Đường dẫn home hoặc cấu hình có dấu cách, dấu nháy đơn, ký tự tiếng Việt** (ví dụ `C:\Users\Nguyễn Văn A`): cài đặt phải thành công, và các lệnh nhúng trong khối chỉ dẫn phải chạy được nguyên văn. Test nằm ở Task 5 và Task 6.
2. **Biến môi trường được đặt nhưng rỗng hoặc chỉ có khoảng trắng** (`CLAUDE_CONFIG_DIR=""`, `CLAUDE_CODE_SESSION_ID=" "`): coi như chưa đặt, không sinh ra đường dẫn `""` hay mã chat `""`. Test nằm ở Task 2.
3. **Hai biến nhận diện nền tảng cùng xuất hiện** (ví dụ Codex chạy trong terminal của Claude Code, có cả `CLAUDECODE=1` lẫn `CODEX_THREAD_ID`): `detect_platform` phải trả `None` và CLI báo lỗi yêu cầu `--platform`, không được đoán. Test nằm ở Task 2 và Task 8.
4. **File chỉ dẫn toàn cục có sẵn BOM, CRLF hoặc byte không phải UTF-8**: phần byte nằm ngoài marker phải giữ nguyên từng byte. Test nằm ở Task 5.
5. **Người dùng sửa một file đã cài rồi chạy `install` lại**: thay đổi không được mất âm thầm. Bản cũ được sao lưu và đường dẫn được liệt kê trong `modified_overwritten`. Test nằm ở Task 6.

---

### Task 1: Module lưu trữ dùng chung `ct_storage`

**Files:**
- Create: `scripts/ct_storage.py`
- Modify: `scripts/task_ledger.py:14-21` (đổi import)
- Test: `tests/test_storage.py`

**Interfaces:**
- Produces:
  - `locked(path: Path, timeout: float = 5.0)`: context manager, giữ khoá OS trên file `<path>.lock`.
  - `atomic_write(path: Path, payload: bytes) -> None`.
  - `shell_argument(value: str) -> str`: quote một tham số cho shell của máy chủ (PowerShell khi `os.name == 'nt'`, ngược lại POSIX).
  - Cả ba được chuyển nguyên văn từ `scripts/coding_team_policy.py`, giữ đúng hành vi.

- [ ] **Step 1: Viết test.** Chuyển các test sau từ `tests/test_policy_helper.py` sang `tests/test_storage.py`, trỏ tới `ct_storage`: `test_shell_argument_quotes_metacharacters_for_both_shells`, `test_atomic_replace_failure_preserves_original_and_cleans_temp`, `test_atomic_fsync_failure_preserves_original_and_cleans_temp`, `test_os_lock_timeout_and_process_exit_release`, `test_os_lock_released_when_body_raises`. Đầu file test thêm `sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))` rồi `import ct_storage`.
- [ ] **Step 2: Chạy** `python -m unittest tests.test_storage -v`. Kỳ vọng FAIL với `ModuleNotFoundError: ct_storage`.
- [ ] **Step 3: Tạo `scripts/ct_storage.py`** với ba hàm trên, chép từ `coding_team_policy.py`. Trong `task_ledger.py`, thay khối `try/except` import bằng import `ct_storage`, giữ nhánh dự phòng `importlib` trỏ tới `ct_storage.py`.
- [ ] **Step 4: Chạy** `python -m unittest tests.test_storage tests.test_task_ledger -v`. Kỳ vọng PASS.
- [ ] **Step 5: Commit và push.** `git add scripts/ct_storage.py scripts/task_ledger.py tests/test_storage.py && git commit -m "refactor: extract shared storage primitives" && git push origin main`.

### Task 2: Cấu hình nền tảng `platforms/*.json` và `ct_platform`

**Files:**
- Create: `platforms/claude-code.json`, `platforms/codex.json`, `platforms/antigravity.json`, `scripts/ct_platform.py`
- Test: `tests/test_platform_config.py`

**Interfaces:**
- Consumes: không có.
- Produces (trong `ct_platform`):
  - Hằng số `PLATFORMS`, `ROLES`, `TIERS`. `ROLES` gồm đúng 6 ID trong Global Constraints, không có biến thể.
  - `REPO_ROOT: Path`.
  - `read_json_unique(path: Path) -> object`: từ chối khoá JSON trùng ở mọi cấp, đọc `utf-8-sig`.
  - `validate_platform(data: dict) -> dict`: trả `{'valid': True, 'platform': str, 'availability_verified': False}`, raise `ValueError` khi có lỗi.
  - `load_platform(name: str, source_root: Path = REPO_ROOT, input_path: Path | None = None) -> dict`.
  - `resolve_config_root(cfg: dict, env: Mapping[str, str], home: Path) -> Path`.
  - `target_paths(cfg: dict, scope: str, env: Mapping[str, str], home: Path, project_dir: Path | None) -> dict[str, Path | None]`. Các khoá: `skill_dir`, `agents_dir`, `global_rules_file` (là `None` khi `scope == 'project'`), `state_file`, `backup_root`, `lock_file`.
  - `detect_platform(env: Mapping[str, str]) -> str | None`.
  - `resolve_tier(cfg: dict, role: str, tier: str | None = None) -> dict`: trả `{'tier', 'model', 'effort'}`.
  - `chat_id_from_env(cfg: dict, env: Mapping[str, str]) -> str | None`.

Lược đồ v2 (khoá bắt buộc, đúng tập này, không thừa không thiếu):
- `schema_version` = 2, `platform`, `verified_on` (chuỗi `YYYY-MM-DD`).
- `config_root` = `{"env": str|null, "default": str}`.
- `tiers`: đúng 4 khoá của `TIERS`, mỗi khoá là `{"model", "effort"}`; `inherit` phải là `{null, null}`.
- `roles`: đúng 6 khoá của `ROLES`, mỗi khoá là `{"tier"}`.
- `effort_vocabulary`, `static_agent_model` ∈ `{default, omit}`, `tool_allowlist_enforced` (bool).
- `tools`: đúng các khoá `read`, `docs_lookup`, `write`, `shell`, `browser_testing`, `delegate`, mỗi khoá là danh sách chuỗi.
- `agent_format` ∈ `{claude-md, codex-toml, antigravity-md}`.
- `paths`: gồm `skill_dir`, `agents_dir`, `global_rules_file`, `state_file`; chỉ được dùng placeholder `{config_root}` và `{home}`.
- `project_paths`: gồm `skill_dir`, `agents_dir`; là đường dẫn tương đối, không placeholder, không `..`.
- `chat_id_env`, `detect_env`, `force_model_env` (danh sách chuỗi), `invocation`.

Giá trị cụ thể (ghi `"verified_on": "2026-11-13"`; các giá trị spec ghi "cần xác minh" phải được kiểm tra lại trước khi ghi):

| Khoá | claude-code | codex | antigravity |
|---|---|---|---|
| config_root | env `CLAUDE_CONFIG_DIR`, default `{home}/.claude` | env `CODEX_HOME`, default `{home}/.codex` | env `null`, default `{home}/.gemini` |
| tiers strong/standard/light | `opus`/`high`, `sonnet`/`medium`, `haiku`/`low` | `gpt-6.1-sol`/`high`, `gpt-6-luna`/`medium`, `gpt-6-luna`/`low` | `pro`/null, `flash`/null, `flash_lite`/null |
| roles | architect strong, researcher light, developer standard, test-engineer standard, reviewer strong, security strong | như cột trái | như cột trái |
| effort_vocabulary | `low, medium, high, xhigh, max` | `low, medium, high, xhigh, max, ultra` | `[]` |
| static_agent_model | `default` | `omit` | `omit` |
| tool_allowlist_enforced | `true` | `false` | `true` |
| tools.read | `Read, Grep, Glob` | `[]` | chép đúng danh sách công cụ đọc trong file `ct-architect.md` của repo agy |
| tools.docs_lookup | `WebFetch, WebSearch` | `[]` | `[]` |
| tools.write | `Edit, Write, NotebookEdit` | `[]` | `write_to_file, replace_file_content, multi_replace_file_content, notebook_edit` |
| tools.shell | `Bash, PowerShell` | `[]` | `run_command, manage_task` |
| tools.browser_testing | `Bash, PowerShell` | `[]` | `run_command` |
| tools.delegate | `Agent` | `[]` | `invoke_subagent, define_subagent` |
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
  - `test_shipped_platforms_validate`: cả 3 file đều cho `valid is True`, `availability_verified is False`, và `platform` khớp tên file.
  - `test_schema_rejects_bad_shapes`: các biến thể sau phải raise `ValueError`:
    - thiếu tầng, hoặc có tầng lạ;
    - `inherit` khác null;
    - vai trò thiếu, thừa, hoặc dùng tên vai trò cũ `QA / Reviewer`;
    - effort nằm ngoài `effort_vocabulary`;
    - `static_agent_model: "always"`;
    - `project_paths` tuyệt đối hoặc có `..`;
    - placeholder lạ `{user}`;
    - `schema_version: 1`.
  - `test_duplicate_keys_rejected`: `read_json_unique` raise `ValueError` với `{"a":1,"a":2}`.
  - `test_config_root_env_and_empty_env`:
    - `CLAUDE_CONFIG_DIR=<tmp>/cfg` cho `skill_dir == <tmp>/cfg/skills/coding-team`;
    - `CLAUDE_CONFIG_DIR=""` và `"  "` cho `<home>/.claude/...`.
  - `test_project_scope_paths`: `scope='project'` cho `skill_dir == project_dir/'.claude/skills/coding-team'`, `global_rules_file is None`, và `state_file` vẫn ở cấp người dùng.
  - `test_backup_and_lock_next_to_state`: `backup_root == state_file.parent/'backups'`, `lock_file == state_file.parent/'install.lock'`.
  - `test_detect_platform`:
    - `{'CLAUDECODE': '1'}` cho `'claude-code'`;
    - `{'CODEX_THREAD_ID': 't'}` cho `'codex'`;
    - `{}` cho `None`;
    - `{'CLAUDECODE': '1', 'CODEX_THREAD_ID': 't'}` cho `None`;
    - `{'CLAUDECODE': ''}` cho `None`.
  - `test_resolve_tier`:
    - Claude Code `ct-architect` cho `{'tier': 'strong', 'model': 'opus', 'effort': 'high'}`;
    - ghi đè `tier='light'` cho `haiku`;
    - `tier='inherit'` cho model và effort đều `None`;
    - tầng lạ raise `ValueError`.
  - `test_chat_id_from_env`:
    - `CLAUDE_CODE_SESSION_ID=' abc '` cho `'abc'`;
    - giá trị rỗng hoặc chỉ khoảng trắng cho `None`;
    - Antigravity ưu tiên `ANTIGRAVITY_CONVERSATION_ID` rồi mới tới `ANTIGRAVITY_CHAT_ID`.
- [ ] **Step 2: Chạy** `python -m unittest tests.test_platform_config -v`. Kỳ vọng FAIL (module chưa có).
- [ ] **Step 3: Tạo 3 file JSON và `scripts/ct_platform.py`** theo Interfaces. `target_paths` thay `{home}` và `{config_root}`; `scope` chỉ nhận `'user'` hoặc `'project'`, giá trị khác raise `ValueError`.
- [ ] **Step 4: Chạy lại test.** Kỳ vọng PASS.
- [ ] **Step 5: Commit và push** với message `feat: add per-platform adapter configs`.

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
  - `load_roles(directory: Path) -> dict[str, dict]`: khoá là role id. Mỗi giá trị là `{'description': str, 'ledger_role': str|None, 'variants': {variant_id: [capability, ...]}, 'body': str}`.
  - `guarantees(capabilities: list[str], cfg: dict) -> dict[str, str]`: khoá `no_file_write`, `write_scope`, `command_limits`, `no_delegate`; giá trị là `ENFORCED`, `ADVISORY` hoặc `N/A`.
  - `tools_for(capabilities: list[str], cfg: dict) -> list[str]`: không trùng lặp, giữ thứ tự, không bao giờ chứa phần tử nào của `cfg['tools']['delegate']`.
  - `render_agent(role_id: str, variant_id: str, roles: dict, cfg: dict) -> tuple[str, bytes]`: trả đường dẫn tương đối dạng POSIX dưới `agents_dir` và nội dung UTF-8 không BOM, dùng LF.
  - `render_all(roles: dict, cfg: dict) -> list[tuple[str, bytes]]`.

`roles.json` (schema_version 1). Biến thể mặc định trùng tên vai trò:

| Vai trò | ledger_role | Biến thể và năng lực |
|---|---|---|
| ct-architect | `Architect` | `ct-architect`: `[read]` |
| ct-researcher | null | `ct-researcher`: `[read, docs_lookup]` |
| ct-developer | `Developer` | `ct-developer`: `[read, write:assigned, shell]` |
| ct-test-engineer | null | `ct-test-engineer`: `[read, write:tests, shell, browser_testing]` |
| ct-reviewer | `QA` | `ct-reviewer`: `[read]`; `ct-reviewer-verify`: `[read, shell:checks]` |
| ct-security | null | `ct-security`: `[read]` |

Quy tắc của `guarantees`, với `E = cfg['tool_allowlist_enforced']`:
- `no_file_write`:
  - Có bất kỳ `write:*` nào → `N/A`.
  - Không có `write:*` nhưng có `shell`, `shell:checks` hoặc `browser_testing` → `ADVISORY`.
  - Còn lại: `ENFORCED` nếu `E`, ngược lại `ADVISORY`.
- `write_scope`: có `write:*` → `ADVISORY`, ngược lại `N/A`.
- `command_limits`: có `shell:checks` → `ADVISORY`, ngược lại `N/A`.
- `no_delegate`: `ENFORCED` nếu `E`, ngược lại `ADVISORY`.

Phần thân mỗi file `ct-*.md`:
- Chuyển nội dung contract của vai trò tương ứng từ `references/agent-contracts.md`: Architect → ct-architect, Investigator → ct-researcher, Developer → ct-developer, Test Engineer → ct-test-engineer, QA / Reviewer → ct-reviewer (thêm mục "Chế độ verify"), Specialist → ct-security (viết lại thành kiểm tra bảo mật, hiệu năng, xử lý đồng thời, kèm điều kiện L3 ở spec §6.7).
- ct-test-engineer thêm mục `browser_testing` theo spec §5.1: provider `auto`, chỉ cho phép `playwright-cli` và `playwright-mcp`, target chỉ localhost hoặc staging được duyệt, tắt `browser_run_code_unsafe` và `browser_file_upload` (ADVISORY).
- ct-developer thêm mục `focus`.

`render_agent` luôn mở đầu phần thân bằng hai khối:
1. `WORKER_CLAUSE`, nguyên văn:
   > You are a coding-team worker, not the Leader. Leader-only rules, routing policy and orchestrator skills (coding-team, subagent-driven-development, dispatching-parallel-agents, executing-plans, requesting-code-review) do not apply to you and must not be invoked. Never dispatch, spawn or define agents. Use technique skills (test-driven-development, systematic-debugging, verification-before-completion, receiving-code-review) only when your handoff names them.
2. Mục `## Runtime guarantees (<platform>)`, mỗi guarantee một dòng `- <key>: <level>`.

Frontmatter theo `agent_format`:
- **`claude-md`**, file `<variant>.md`:
  - `name`, `description` (chuỗi trong nháy kép JSON);
  - `tools: <tools_for nối bằng ", ">`;
  - nếu `static_agent_model == 'default'` và model của tầng khác null thì thêm `model:` và `effort:`.
- **`codex-toml`**, file `<variant>.toml`:
  - `name`, `description`, `developer_instructions` (WORKER_CLAUSE + guarantees + thân, đặt trong chuỗi TOML literal nhiều dòng `'''…'''`; nếu thân có `'''` thì dùng chuỗi basic `"""…"""` và escape);
  - không có `model`, `model_reasoning_effort`, `sandbox_mode`.
- **`antigravity-md`**, file `<variant>/agent.md`:
  - `name`, `description`, `mainAgent: false`, `subagent: true`, `hidden: false`, `inheritCustomizations: true`;
  - `inheritMcp`: `true` khi có `docs_lookup` hoặc `browser_testing`, ngược lại `false`;
  - `commandExecutionPolicy`: `auto` khi có `shell`, `shell:checks` hoặc `browser_testing`, ngược lại `off`;
  - `tools:` là danh sách YAML;
  - không có `model` (vì `omit`).

- [ ] **Step 1: Viết test** trong `tests/test_roles.py`:
  - `test_catalog_matches_platform_roles`: `set(load_roles(...)) == set(ct_platform.ROLES)`; các `ledger_role` khác null đúng là `{'Architect', 'Developer', 'QA'}`.
  - `test_guarantee_matrix`: với cấu hình Claude Code:
    - `ct-architect` cho `no_file_write == 'ENFORCED'`, `no_delegate == 'ENFORCED'`;
    - `ct-reviewer-verify` cho `no_file_write == 'ADVISORY'`, `command_limits == 'ADVISORY'`;
    - `ct-developer` cho `write_scope == 'ADVISORY'`, `no_file_write == 'N/A'`.
    - Với Codex, mọi guarantee khác `N/A` đều là `ADVISORY`.
  - `test_read_only_never_gets_shell_or_delegate`: Claude Code `ct-architect` có tools đúng là `Read, Grep, Glob`; không biến thể nào chứa `Agent`; Antigravity không biến thể nào chứa `invoke_subagent` hay `define_subagent`.
  - `test_claude_frontmatter`: `ct-architect.md` chứa `model: opus` và `effort: high`; không chứa `isolation:`; bắt đầu bằng `---\n`; không có BOM.
  - `test_codex_toml_parses_without_model_or_sandbox`: mọi file `.toml` qua `tomllib.loads` đều có `name`, `description`, `developer_instructions`; không có `model`, `model_reasoning_effort`, `sandbox_mode`. Thử thêm một thân chứa `'''` và `\`: vẫn parse được và nguyên văn khớp.
  - `test_antigravity_flags`: mọi `agent.md` chứa `mainAgent: false`, `subagent: true`, `inheritCustomizations: true`; `ct-architect` có `commandExecutionPolicy: off`; `ct-developer` có `commandExecutionPolicy: auto`; không file nào có dòng `model:`.
  - `test_worker_clause_and_guarantees_present`: mọi nội dung sinh ra đều chứa `WORKER_CLAUSE` và `## Runtime guarantees`.
  - `test_render_all_count`: mỗi nền tảng sinh đúng 7 file (6 vai trò + `ct-reviewer-verify`).
- [ ] **Step 2: Chạy** `python -m unittest tests.test_roles -v`. Kỳ vọng FAIL.
- [ ] **Step 3: Tạo `roles.json`, 6 file thân và `scripts/ct_roles.py`** theo Interfaces và các quy tắc ở trên.
- [ ] **Step 4: Chạy lại test.** Kỳ vọng PASS.
- [ ] **Step 5: Commit và push** với message `feat: add role catalog, capability matrix and agent renderers`.

### Task 4: State bật/tắt và lệnh mới `ct_policy`

**Files:**
- Create: `scripts/ct_policy.py`
- Test: `tests/test_policy.py`

**Interfaces:**
- Consumes: `ct_storage.locked`, `ct_storage.atomic_write`.
- Produces:
  - `class LegacyCommandError(ValueError)`, thuộc tính `.hint: str`.
  - `validate_state(state: dict) -> None`: schema v1 giữ nguyên.
  - `load_state(path) -> dict`, `save_state(path, state) -> None`.
  - `initialize_state(path, default: str | None = None) -> tuple[dict, bool]`:
    - state chưa có: tạo với `global_policy = default or 'ON'`;
    - state đã có và `default` khác `None`: chỉ đổi `global_policy`, giữ `chat_policies`;
    - state đã có và `default is None`: không đổi gì;
    - giá trị bool trả về cho biết có ghi hay không;
    - `default` nhận `'on'`/`'off'` không phân biệt hoa thường.
  - `describe(state: dict, chat_id: str | None) -> dict`: khoá `global_policy`, `chat_id`, `chat_policy` (`ON`/`OFF`/`INHERIT`/`UNKNOWN`), `effective_policy` (`ON`/`OFF`/`None`).
  - `parse_command(text: str) -> tuple[str, str | None]`: trả một trong `('status', None)`, `('help', None)`, `('chat', 'ON')`, `('chat', 'OFF')`, `('global', 'ON')`, `('global', 'OFF')`, `('chat', 'RESET')`.
  - `apply_command(path, text: str, chat_id: str | None) -> dict`:
    - `help` trả `{'help': HELP_LINES}` và không đọc state;
    - `status` trả `describe`;
    - lệnh chat khi thiếu mã chat raise `ValueError('chat command requires a trusted chat id; use --global or pass --chat-id')`;
    - không có gì thay đổi thì không ghi.
  - `HELP_LINES: tuple[str, ...]`: 7 dòng, mỗi lệnh một dòng.

- [ ] **Step 1: Viết test** trong `tests/test_policy.py`. Chuyển các test tương ứng từ `tests/test_policy_helper.py` (khởi tạo, chat precedence, status không ghi, state hỏng thì fail-closed, cập nhật song song không mất chat override) sang API mới, và thêm:
  - `test_new_grammar`: chuẩn hoá khoảng trắng và hoa thường cho cả hai tiền tố `/coding-team` và `$coding-team`, đủ 7 lệnh.
  - `test_legacy_syntax_rejected_with_hint`: các đầu vào `/coding team on`, `/coding-team on all`, `/coding-team off all`, `/coding-team inherit` đều raise `LegacyCommandError`. `hint` lần lượt chứa `/coding-team on`, `/coding-team on --global`, `/coding-team off --global`, `/coding-team reset`.
  - `test_embedded_or_quoted_commands_rejected`: tập đầu vào cũ của `test_only_complete_unquoted_commands_are_accepted`, chuyển sang cú pháp mới, đều raise `ValueError`.
  - `test_reset_removes_only_this_chat`: chat `a` OFF, chat `b` OFF; `reset` với `a` cho `chat_policies == {'b': 'OFF'}`.
  - `test_initialize_default`: tạo mới với `'off'` cho `OFF`. Trên state có chat override: `default='on'` chỉ đổi global; `default=None` giữ nguyên byte của file.
  - `test_help_needs_no_state`: `apply_command(<đường dẫn không tồn tại>, '/coding-team help', None)` trả `help` với `len == 7`, không tạo file.
- [ ] **Step 2: Chạy** `python -m unittest tests.test_policy -v`. Kỳ vọng FAIL.
- [ ] **Step 3: Viết `scripts/ct_policy.py`** theo Interfaces. Giữ nguyên các kiểm tra lệnh nằm trong nháy, lệnh nhiều dòng và lệnh bị nhúng trong câu của bản cũ.
- [ ] **Step 4: Chạy lại test.** Kỳ vọng PASS.
- [ ] **Step 5: Commit và push** với message `feat: new /coding-team command grammar and policy state`.

### Task 5: Khối chỉ dẫn toàn cục `ct_rules`

**Files:**
- Create: `scripts/ct_rules.py`
- Test: `tests/test_rules.py`

**Interfaces:**
- Consumes: `ct_storage.locked`, `ct_storage.atomic_write`, `ct_storage.shell_argument`.
- Produces:
  - `START = '<!-- coding-team-policy:start -->'`, `END = '<!-- coding-team-policy:end -->'` (giữ nguyên marker cũ để thay được khối của bản cũ).
  - `render_rules_block(*, platform: str, python: str, cli: Path, state: Path, skill_dir: Path) -> str`.
  - `upsert_block(path: Path, block: str) -> bool`: trả `True` khi có thay đổi. Marker bị hỏng, trùng hoặc đảo ngược thì raise `ValueError`.
  - `remove_block(path: Path) -> bool`.

Khối chỉ dẫn render bằng tiếng Anh và phải chứa:
1. **Lệnh kiểm tra trạng thái:** `{python} {cli} status --platform {platform} --state {state}`, mọi đường dẫn qua `shell_argument`; trên Windows có tiền tố `& `.
2. **Mẫu lệnh** `{python} {cli} command '<exact command>' --platform {platform} --state {state}`, trong đó `<exact command>` cũng đi qua `shell_argument`.
3. **Danh sách 7 lệnh mới.**
4. **Hợp đồng mã chat:** ưu tiên `--chat-id`, sau đó tới biến runtime tin cậy, còn lại là `UNKNOWN`, không bao giờ đoán.
5. **Bảng phân luồng rút gọn** theo đúng thứ tự spec §6.2, gồm cả câu "file count alone is never the criterion", kèm đường dẫn tới `{skill_dir}/references/orchestration.md`.
6. **Câu nêu giới hạn:** `Routing is ADVISORY: these instructions guide the Leader; they do not enforce a single orchestrator.`
7. **Câu dành cho worker:** `Subagents/workers: these rules are Leader-only.`

- [ ] **Step 1: Viết test** trong `tests/test_rules.py`. Chuyển từ `test_policy_helper.py` các test `test_bootstrap_idempotent_preserves_unrelated_bytes_and_custom_paths` (giữ tiền tố BOM/CRLF/`\xff` và hậu tố `\x80`), `test_bootstrap_append_to_missing_and_unmarked_files`, `test_bootstrap_rejects_malformed_duplicate_and_reversed_markers`, đổi sang `upsert_block` và `render_rules_block`. Thêm:
  - `test_generated_commands_execute_with_special_paths`:
    - dùng đường dẫn chứa `spaces apostrophe' $(echo SUBSTITUTED) Nguyễn`;
    - chép `scripts/` của repo và `platforms/claude-code.json` vào một skill dir tạm;
    - khởi tạo state qua `ct_policy.initialize_state`;
    - chạy lệnh status trích từ khối chỉ dẫn qua PowerShell (Windows) hoặc `/bin/sh -c`, với env `CLAUDE_CODE_SESSION_ID=fixture-shell`, kỳ vọng `effective_policy == 'ON'`;
    - chạy mẫu lệnh với `'/coding-team off'`, kỳ vọng `'OFF'`.
    - Test này phụ thuộc Task 8. Đánh dấu `@unittest.skipUnless((SCRIPTS/'coding_team.py').exists(), 'CLI added in Task 8')`; Task 8 Step 4 sẽ chạy lại test này.
  - `test_block_contents`: khối chứa `/coding-team on --global`, `/coding-team reset`, `Routing is ADVISORY`, `Leader-only`, `references/orchestration.md`; không chứa `on all` hay `inherit`.
  - `test_remove_block_preserves_rest`: chèn khối rồi gỡ; byte còn lại giống hệt file gốc trước khi chèn (khi file gốc có kết thúc bằng newline).
- [ ] **Step 2: Chạy** `python -m unittest tests.test_rules -v`. Kỳ vọng FAIL.
- [ ] **Step 3: Viết `scripts/ct_rules.py`.** Logic upsert chép từ `install_global_instructions` cũ (khoá, đếm marker, ghi nguyên tử).
- [ ] **Step 4: Chạy lại test.** Kỳ vọng PASS; test thực thi lệnh được báo SKIP.
- [ ] **Step 5: Commit và push** với message `feat: platform-aware global rules block with routing policy`.

### Task 6: Cài đặt và gỡ cài đặt theo kiểu giao dịch `ct_install`

**Files:**
- Create: `scripts/ct_install.py`
- Test: `tests/test_install.py`

**Interfaces:**
- Consumes: `ct_platform.target_paths`, `ct_platform.load_platform`, `ct_roles.load_roles`, `ct_roles.render_all`, `ct_rules.render_rules_block`, `ct_rules.upsert_block`, `ct_rules.remove_block`, `ct_policy.initialize_state`, `ct_storage.locked`, `ct_storage.atomic_write`.
- Produces:
  - `class UnmanagedTargetError(ValueError)`, có `.paths: list[str]`.
  - `class InstallRolledBack(RuntimeError)`.
  - `@dataclass InstallPlan`: `platform: str`, `scope: str`, `paths: dict`, `files: list[tuple[str, str, bytes]]` (mỗi phần tử là root label `'skill_dir'` hoặc `'agents_dir'`, đường dẫn tương đối POSIX, nội dung), `rules_block: str | None`, `state_default: str | None`.
  - `build_plan(source_root: Path, cfg: dict, *, scope: str, env: Mapping, home: Path, project_dir: Path | None, default: str | None, global_rules: bool, python: str) -> InstallPlan`:
    - `scope == 'project'` kết hợp với `default` hoặc `global_rules` thì raise `ValueError`;
    - các file của skill: `SKILL.md`, `LICENSE`, `references/**`, `agents/roles/**` (để `doctor` chạy từ bản cài vẫn tính được guarantees), các file `scripts/*.py` trừ `export_evaluation_inputs.py` và `build_runtime_package.py`, file `platforms/<platform>.json`, và riêng Codex thêm `agents/openai.yaml`.
  - `apply_plan(plan: InstallPlan, *, dry_run: bool, replace_unmanaged: bool) -> dict`. Kết quả có các khoá: `dry_run`, `written`, `unchanged`, `removed_stale`, `kept_modified_stale`, `modified_overwritten`, `backup_dir`, `manifest`, `rules_file`, `state`.
  - `uninstall(cfg: dict, *, scope: str, env: Mapping, home: Path, project_dir: Path | None, dry_run: bool, remove_modified: bool) -> dict`. Kết quả có các khoá: `removed`, `kept_modified`, `missing`, `rules_removed`, `backup_dir`.
  - Hằng số `MANIFEST_NAME = 'install-manifest.json'`.

**Manifest** (ở `<skill_dir>/install-manifest.json`, schema_version 1):
- `platform`, `scope`, `installed_at` (ISO UTC);
- `files: {"skill_dir": {rel: sha256}, "agents_dir": {rel: sha256}}`;
- `rules_file: str | null`.

Bản thân manifest không nằm trong `files`. Một file được coi là **do coding-team quản lý** khi nó có trong manifest của chính `skill_dir` đó.

**Các bước của `apply_plan`** (khi `dry_run=True`, chỉ trả về những gì *sẽ* làm, không tạo file hay thư mục nào):
1. Giữ `ct_storage.locked(paths['lock_file'], timeout=30)` trong suốt giao dịch.
2. Đọc manifest cũ (nếu có).
3. Tìm các đích không do coding-team quản lý:
   - `skill_dir` đã tồn tại mà không có manifest;
   - file agent đã tồn tại mà không có trong manifest.

   Nếu có và chưa bật `replace_unmanaged`, raise `UnmanagedTargetError` liệt kê đường dẫn, không ghi gì.
4. Dựng staging trong `backup_root/.staging-<timestamp>`.
5. Sao lưu mọi đích sẽ bị thay hoặc xoá vào `backup_root/<timestamp>/`, giữ nguyên cấu trúc theo root label. File chỉ dẫn toàn cục cũng được sao lưu. Với `skill_dir` không do coding-team quản lý, sao lưu cả thư mục.
6. Thay từng file bằng `atomic_write`. File cũ trong manifest nhưng không còn trong plan: xoá nếu hash còn khớp; nếu không khớp thì giữ lại và đưa vào `kept_modified_stale`. File do coding-team quản lý có hash hiện tại khác manifest thì vẫn ghi đè, nhưng đưa vào `modified_overwritten`.
7. `upsert_block` nếu có `rules_block`.
8. `initialize_state(state_file, plan.state_default)`.
9. Ghi manifest. `installed_at` chỉ đổi khi có ít nhất một thay đổi, để cài lại lần hai cho manifest giống hệt từng byte.
10. Bất kỳ exception nào ở bước 6–9: khôi phục từ bản sao lưu (xoá file mới được tạo, chép lại file đã sao lưu), rồi raise `InstallRolledBack` có `__cause__` là lỗi gốc.

Không có gì để sao lưu thì không tạo thư mục backup (`backup_dir` là `None`).

**Gỡ cài đặt:**
- Không có manifest thì raise `ValueError('not a coding-team managed installation: <skill_dir>')`.
- File có hash khớp thì xoá. File không khớp thì giữ lại, trừ khi bật `remove_modified` (trường hợp này sao lưu trước rồi mới xoá).
- `remove_block` nếu manifest có `rules_file`.
- **Không xoá state** bật/tắt.
- Xoá thư mục `skill_dir` nếu đã trống.

- [ ] **Step 1: Viết test** trong `tests/test_install.py`. Mọi test dùng `home` và `project_dir` tạm, env tự dựng, và `source_root` là repo.
  - `test_dry_run_writes_nothing`: cả 3 nền tảng; danh sách đích nằm dưới `config_root`; cây thư mục tạm không đổi.
  - `test_install_layout_per_platform`:
    - Claude Code có `skills/coding-team/SKILL.md`, `agents/ct-architect.md`, `agents/ct-reviewer-verify.md`;
    - Codex có `agents/ct-architect.toml`, `.agents/skills/coding-team/agents/openai.yaml`;
    - Antigravity có `config/agents/ct-architect/agent.md`;
    - skill dir chỉ có đúng một file trong `platforms/`.
  - `test_config_root_env_respected`: `CLAUDE_CONFIG_DIR=<tmp>/alt dir` thì mọi file nằm dưới `<tmp>/alt dir`.
  - `test_second_install_is_noop`: `written == []`, manifest giống hệt từng byte, `backup_dir is None`.
  - `test_rollback_on_midway_failure`: cài lần 1; patch `ct_install.atomic_write` để raise ở lần ghi thứ 3 của lần cài thứ 2 (với nội dung khác); kỳ vọng `InstallRolledBack`, mọi file giống hệt sau lần cài 1, không còn thư mục `.staging-*`.
  - `test_concurrent_install_waits_for_lock`: giữ `locked(lock_file)` ở tiến trình con 2 giây; `apply_plan` ở tiến trình chính hoàn tất sau khi khoá được nhả, và kết quả hợp lệ.
  - `test_unmanaged_target_requires_flag`: tạo trước `skill_dir/SKILL.md` (mô phỏng bản git clone cũ); kỳ vọng `UnmanagedTargetError` và file không đổi. Bật `replace_unmanaged=True` thì thành công, bản cũ nằm trong `backup_dir`.
  - `test_modified_managed_file_is_backed_up_and_reported` (Review Focus 5): cài; sửa `agents/ct-architect.md`; cài lại; kỳ vọng file đó nằm trong `modified_overwritten` và bản sửa nằm trong backup.
  - `test_scope_rules`:
    - `scope='project'` kết hợp `default='off'` raise `ValueError`;
    - `scope='project'` kết hợp `global_rules=True` raise `ValueError`;
    - cài theo dự án không tạo hay đổi `state_file`, và không đụng tới `global_rules_file`.
  - `test_default_and_state_preservation`: state có chat override; cài với `default='off'` thì global là `OFF` và chat override còn nguyên; cài với `default=None` thì byte của state không đổi.
  - `test_global_rules_migration`: `AGENTS.md` (Codex) có sẵn khối marker cũ chứa `/coding team on all`; sau khi cài có `--global-rules`, khối mới thay đúng một khối, phần byte ngoài marker giữ nguyên, không còn `on all`.
  - `test_uninstall_keeps_modified`: sửa một file đã cài; gỡ thì file đó nằm trong `kept_modified` và vẫn còn, các file khác bị xoá; `state_file` vẫn còn. Gỡ với `remove_modified=True` thì xoá cả file đó và có bản sao lưu.
  - `test_uninstall_unmanaged_refused`.
  - `test_home_with_special_characters` (Review Focus 1): `home` là `<tmp>/Nguyễn Văn A's home`; cài Claude Code có `--global-rules` thành công, và `CLAUDE.md` chứa đường dẫn đã được quote.
- [ ] **Step 2: Chạy** `python -m unittest tests.test_install -v`. Kỳ vọng FAIL.
- [ ] **Step 3: Viết `scripts/ct_install.py`** theo Interfaces và các bước ở trên. Hash tính trên đúng byte được ghi.
- [ ] **Step 4: Chạy** `python -m unittest tests.test_install tests.test_roles tests.test_platform_config -v`. Kỳ vọng PASS.
- [ ] **Step 5: Commit và push** với message `feat: transactional installer with manifest, backup and rollback`.

### Task 7: Metadata điều phối và bàn giao `ct_orchestration`

**Files:**
- Create: `scripts/ct_orchestration.py`
- Test: `tests/test_orchestration.py`

**Interfaces:**
- Consumes: `ct_storage.locked`, `ct_storage.atomic_write`.
- Produces:
  - `STATE_REL = Path('.coding-team/orchestration.json')`.
  - `ORCHESTRATORS = ('leader-direct', 'coding-team', 'superpowers-sdd', 'superpowers-dpa', 'superpowers-executing-plans')`.
  - `class OrchestrationConflict(ValueError)`, có `.blockers: list[str]`.
  - `active_sdd_workspaces(workspace: Path) -> list[str]`: các đường dẫn POSIX tương đối của `.superpowers/sdd/*/progress.md` đang tồn tại.
  - `status(workspace: Path) -> dict`: khoá `owner` (record hoặc `None`), `active_sdd` (đã trừ các workspace có trong `acknowledged_sdd` của record), `can_claim: bool`, `blockers: list[str]`.
  - `claim(workspace: Path, task_id: str, mode: str, phase: str) -> dict`:
    - có blocker thì raise `OrchestrationConflict`;
    - claim lại cùng `task_id` với owner `coding-team` thì chỉ cập nhật `phase`.
  - `validate_handoff(record: dict) -> None`: raise `ValueError` liệt kê mọi trường thiếu hoặc sai.
  - `handoff(workspace: Path, record: dict) -> dict`: sau khi validate, owner mới là `record['to']`, ghi `handoff_from`, `handoff_record` và `acknowledged_sdd`.
  - `release(workspace: Path, task_id: str) -> dict`: chỉ owner hiện tại được release.

Record điều phối: `{"task_id", "orchestrator", "mode", "phase", "handoff_from", "handoff_record", "acknowledged_sdd": []}`. Lần ghi đầu tạo `.coding-team/.gitignore` với nội dung `*\n`.

Record bàn giao bắt buộc có:
- `task_id`, `reason` (chuỗi không rỗng);
- `from`, `to`: thuộc `ORCHESTRATORS` và phải khác nhau;
- `workers`: danh sách `{id, terminated, evidence}` với `terminated is True` và `evidence` không rỗng cho **mọi** phần tử (danh sách rỗng được phép);
- `snapshot`: `{head: str, uncommitted: list, untracked: list}`;
- `tests`: `{commands: list, result: str, snapshot: str}`;
- `findings`: danh sách;
- `previous_ledger`: chuỗi hoặc null;
- `acknowledged_sdd`: danh sách.

- [ ] **Step 1: Viết test** trong `tests/test_orchestration.py`:
  - `test_active_sdd_blocks_claim`: tạo `.superpowers/sdd/plan-a/progress.md`; `status` cho `can_claim is False`, blocker có nhắc `plan-a`; `claim` raise `OrchestrationConflict`.
  - `test_other_owner_blocks_claim`: owner là `superpowers-sdd` thì `claim` của coding-team raise.
  - `test_handoff_requires_terminated_workers`: thiếu `evidence`, hoặc `terminated: False`, thì `validate_handoff` raise và thông báo có tên trường; thiếu `snapshot.untracked` cũng raise.
  - `test_valid_handoff_then_claim`: SDD dở; bàn giao từ `superpowers-sdd` sang `coding-team` với `acknowledged_sdd=['.superpowers/sdd/plan-a/progress.md']`; sau đó `status` cho `can_claim is True` và `claim` thành công, `handoff_from == 'superpowers-sdd'`.
  - `test_gitignore_created_and_release`: sau `claim`, `.coding-team/.gitignore` có nội dung `*\n`; `release` bởi task khác raise; `release` đúng task thì owner là `None`.
- [ ] **Step 2: Chạy** `python -m unittest tests.test_orchestration -v`. Kỳ vọng FAIL.
- [ ] **Step 3: Viết `scripts/ct_orchestration.py`.**
- [ ] **Step 4: Chạy lại test.** Kỳ vọng PASS.
- [ ] **Step 5: Commit và push** với message `feat: orchestration ownership, SDD detection and handoff validation`.

### Task 8: CLI `coding_team.py`, `doctor`, và gỡ các script cũ

**Files:**
- Create: `scripts/coding_team.py`, `tests/test_cli.py`
- Delete: `scripts/coding_team_policy.py`, `scripts/check_model_profiles.py`, `references/model-profiles.json`, `tests/test_policy_helper.py`, `tests/test_model_profiles.py`
- Modify: `.github/workflows/ci.yml` chỉ khi cần (dự kiến không cần)

**Interfaces:**
- Consumes: toàn bộ các module của Task 1–7.
- Produces:
  - `main(argv: list[str] | None = None) -> int`. Luôn in đúng một object JSON ra stdout. Exit code: `0` thành công hoặc khoẻ; `1` hợp lệ nhưng kết quả âm (doctor có vấn đề, `orchestrator status` không claim được); `2` lỗi.
  - Các subcommand:
    - `install --platform P [--scope user|project] [--project-dir D] [--default on|off] [--global-rules] [--dry-run] [--replace-unmanaged]`
    - `uninstall --platform P [--scope …] [--project-dir D] [--dry-run] [--remove-modified]`
    - `status [--platform P] [--chat-id ID] [--state F]`
    - `command TEXT [--platform P] [--chat-id ID] [--state F]`
    - `doctor [--platform P] [--scope …] [--project-dir D]`
    - `check-config [--platform P | --input F]`
    - `orchestrator {status|claim|handoff|release} [--workspace W] [--task-id T] [--mode M] [--phase PH] [--input F]`
  - `resolve_platform(arg: str | None, env: Mapping) -> str`. Thứ tự: tham số `--platform`; trường `platform` trong `install-manifest.json` ở thư mục cha của `scripts/` (bản cài đang chạy); `detect_platform(env)`. Không xác định được thì raise `ValueError('cannot determine platform; pass --platform')`.
  - `doctor(cfg, *, scope, env, home, project_dir) -> dict`. Khoá:
    - `manifest`: `ok`, `missing` hoặc `invalid`;
    - `modified_files`, `missing_files`;
    - `agents`: `{variant: {"present", "requested_tier", "model_in_file", "guarantees"}}`;
    - `force_model_env_active`: danh sách tên biến đang bật;
    - `state`: `ok`, `missing` hoặc `corrupt`;
    - `rules_block`: `present`, `absent`, `malformed` hoặc `not_applicable`;
    - `superpowers`: `{"installed": "found"|"not_found", "enabled": true|false|null, "available_in_session": "determined by Leader at runtime"}`;
    - `healthy: bool`.
  - Dò Superpowers: tìm `**/skills/subagent-driven-development/SKILL.md` dưới `config_root` và `{home}/.agents/skills`. Với Claude Code, `enabled` lấy từ `settings.json` → `enabledPlugins`: khoá có chứa `superpowers` cho `true`/`false`, không có thì `null`.

- [ ] **Step 1: Viết test** trong `tests/test_cli.py`. Gọi bằng subprocess `[sys.executable, 'scripts/coding_team.py', …]`; env có `HOME` và `USERPROFILE` trỏ vào thư mục tạm, đồng thời xoá các biến nhận diện nền tảng.
  - `test_install_status_command_roundtrip`: `install --platform claude-code --default on`; `status --platform claude-code` với `CLAUDE_CODE_SESSION_ID=s1` cho `effective_policy ON`; `command '/coding-team off'` cho `OFF`; `command '/coding-team on all'` cho exit 2, JSON chứa `hint`.
  - `test_ambiguous_platform_detection` (Review Focus 3): env có cả `CLAUDECODE=1` và `CODEX_THREAD_ID=x`, chạy `status` từ repo không kèm `--platform` cho exit 2, thông báo chứa `--platform`.
  - `test_installed_copy_infers_platform`: chạy `<skill_dir>/scripts/coding_team.py status` không kèm `--platform` cho `platform == 'claude-code'`. Chạy `<skill_dir>/scripts/coding_team.py doctor` từ bản cài cho exit 0 (bản cài tự đọc được `agents/roles/` của chính nó).
  - `test_chat_unknown_status_and_global_still_works`: không có mã chat thì `status` cho `chat_policy UNKNOWN` (exit 0), `command '/coding-team off'` cho exit 2, `command '/coding-team off --global'` cho exit 0.
  - `test_doctor_reports_guarantees_and_force_env`: sau khi cài, `doctor` cho `healthy True`, `agents['ct-architect']['guarantees']['no_file_write'] == 'ENFORCED'`; có `CLAUDE_CODE_SUBAGENT_MODEL_FORCE=1` thì `force_model_env_active` chứa tên biến đó; xoá một file agent thì exit 1 và `missing_files` khác rỗng.
  - `test_check_config`: file hợp lệ cho exit 0; file có tầng lạ cho exit 2.
  - `test_orchestrator_cli`: `status` khi có SDD dở cho exit 1; `handoff --input` (record hợp lệ) cho exit 0; `claim` cho exit 0.
  - `test_legacy_scripts_removed`: `scripts/coding_team_policy.py` và `references/model-profiles.json` không còn tồn tại.
- [ ] **Step 2: Chạy** `python -m unittest tests.test_cli -v`. Kỳ vọng FAIL.
- [ ] **Step 3: Viết `scripts/coding_team.py`** và xoá 5 file trong danh sách Delete. Ngay đầu `coding_team.py`, chèn thư mục của chính nó vào `sys.path` để import được `ct_*` khi chạy từ bản cài.
- [ ] **Step 4: Chạy toàn bộ** `python -m unittest discover -s tests -p "test_*.py" -v`. Kỳ vọng PASS, và `test_generated_commands_execute_with_special_paths` ở Task 5 giờ chạy thật (không còn SKIP).
- [ ] **Step 5: Commit và push** với message `feat: unified coding_team CLI with doctor; remove Codex-only helpers`.

### Task 9: Viết lại SKILL.md và các file trong references cho đa nền tảng và phân luồng

**Files:**
- Modify: `SKILL.md`, `references/agent-contracts.md`, `references/model-routing.md`, `references/control-policy.md`, `references/workflow.md`, `references/debate-governance.md`, `references/quality-safety.md`, `references/task-ledger.md`
- Create: `references/orchestration.md`
- Không sửa: `references/governance-defaults.json`

**Interfaces:**
- Consumes: tên lệnh và CLI ở Task 8, ID vai trò ở Task 3, ID bộ điều phối ở Task 7.
- Produces: tài liệu vận hành. Task 10 kiểm tra các nội dung này bằng test.

Yêu cầu nội dung (mỗi dòng là một quyết định phải có trong tài liệu):
- **SKILL.md** (giữ dưới khoảng 90 dòng):
  - frontmatter `name: coding-team`, description không nhắc tên nền tảng nào;
  - không còn chữ `Codex` hay `CODEX_`;
  - mục "Choose the orchestrator" ở đầu file: bảng §6.2 rút gọn và link tới `references/orchestration.md`;
  - bảng L0–L3, trong đó L1 là "Leader direct" trừ khi người dùng yêu cầu coding-team;
  - các chế độ giữ nguyên;
  - mục eligibility dùng `scripts/coding_team.py status|command` và 7 lệnh;
  - bảng 6 vai trò (ID, trách nhiệm, năng lực) kèm câu "default tiers come from the installed `platforms/<platform>.json`; Leader may raise or lower them";
  - câu về mức bảo đảm "report ENFORCED/ADVISORY honestly; never present ADVISORY as a guarantee";
  - cấm lồng agent;
  - các cổng duyệt giữ nguyên, thêm điều kiện L3 của `ct-security` (§6.7).
- **agent-contracts.md:**
  - giữ phần compact/full handoff;
  - handoff thêm các trường: role/variant ID, Preferred Tier, Requested Model, guarantees thực tế, skill kỹ thuật được phép (gọi tên), skill điều phối bị cấm;
  - phần contract riêng từng vai trò thay bằng link tới `../agents/roles/ct-*.md`;
  - handoff evidence thêm `Effective Model` (hoặc `unverified`).
- **model-routing.md:** viết lại theo spec §5.3: tầng, ba khái niệm, `static_agent_model` (Codex `omit` và lý do), thứ tự ưu tiên ghim → Leader → mặc định, leo thang sau 1 vòng sửa thất bại, biến ép model, cách xử lý dưới STRICT_MODEL, câu "valid JSON is not evidence routing works".
- **control-policy.md:** 7 lệnh, bảng đường dẫn state theo nền tảng (trỏ tới `platforms/*.json`), hợp đồng mã chat §5.5, cách chạy `coding_team.py`, `install --global-rules`, chuyển từ bản cài cũ.
- **orchestration.md** (mới): §6.1–6.6 của spec, gồm:
  - nêu rõ ADVISORY và giới hạn;
  - bảng có thứ tự;
  - ví dụ "đổi tên trên 10 file" và "xác thực 2 file";
  - phân loại skill;
  - coding-team giữ cả task và dùng spec/plan của Superpowers;
  - các trường bắt buộc khi bàn giao;
  - lệnh `coding_team.py orchestrator status|claim|handoff|release`;
  - fallback khi không có Superpowers;
  - câu "`doctor` cannot know `available_in_session`; Leader decides from its skill list".
- **workflow.md, debate-governance.md, quality-safety.md:**
  - đổi tên vai trò: Investigator → `ct-researcher`, Test Engineer → `ct-test-engineer`, QA / Reviewer → `ct-reviewer`, Specialist → `ct-security`/`focus`;
  - quality-safety thêm quy tắc Gate 2 L3 của §6.7: thiếu báo cáo, báo cáo gắn với snapshot khác, hoặc còn BLOCKER/MAJOR mở thì không PASS; source đổi thì báo cáo mất hiệu lực;
  - nhắc `ct-reviewer-verify` khi cần chạy kiểm tra.
- **task-ledger.md:** thêm một đoạn ánh xạ `Architect=ct-architect`, `Developer=ct-developer`, `QA=ct-reviewer`, và câu nói rằng định dạng ledger không đổi.

- [ ] **Step 1: Viết lại các file** theo yêu cầu ở trên.
- [ ] **Step 2: Kiểm tra link thủ công:** `python -c "import re,pathlib;[print(p,l) for p in [pathlib.Path('SKILL.md'),*pathlib.Path('references').glob('*.md')] for l in re.findall(r'\]\(([^)#]+)',p.read_text(encoding='utf-8')) if not l.startswith('http') and not (p.parent/l).exists()]"`. Kỳ vọng không in ra gì.
- [ ] **Step 3: Chạy toàn bộ test.** Kỳ vọng PASS.
- [ ] **Step 4: Commit và push** với message `docs: multi-platform roles, model routing and orchestration policy`.

### Task 10: Fixture hành vi cho phân luồng và test nhất quán tài liệu

**Files:**
- Create: `tests/orchestration_cases.json`, `tests/test_orchestration_fixture.py`, `tests/test_skill_docs.py`

**Interfaces:**
- Consumes: `check_behavioral_results.check_results` (không sửa), tài liệu của Task 9, `ct_roles.load_roles`.
- Produces: fixture `orchestration_cases.json` theo cùng lược đồ với `mode_cases.json` (`schema_version`, `instructions`, `cases[{id, input, checks}]`). Các trường kiểm tra thêm: `orchestrator` (thuộc `ORCHESTRATORS` của Task 7), `accept_task` (bool), `handoff_valid` (bool), `gate2_pass` (bool).

Các ca bắt buộc (id → kỳ vọng chính):

| id | Kỳ vọng chính |
|---|---|
| `rename_symbol_10_files` | `orchestrator == 'leader-direct'` |
| `auth_change_2_files_l3` | `orchestrator == 'coding-team'` |
| `planned_low_risk_sequential` | `orchestrator == 'superpowers-sdd'` |
| `planned_low_risk_no_superpowers` | `orchestrator == 'coding-team'` |
| `three_independent_failures` | `orchestrator == 'superpowers-dpa'` |
| `architecture_dispute` | `orchestrator == 'coding-team'` |
| `user_names_orchestrator` | Người dùng yêu cầu SDD cho một việc L2 không có rủi ro an toàn; kỳ vọng `superpowers-sdd` |
| `active_sdd_workspace` | `accept_task is False`, `action == 'hold'` |
| `handoff_missing_worker_evidence` | `handoff_valid is False`, `can_write_affected is False` |
| `worker_requests_sdd` | Worker trong coding-team muốn tự gọi SDD; `action == 'hold'`, `orchestrator == 'coding-team'` |
| `l3_missing_security_report` | `gate2_pass is False`, `gate == 'HOLD'` |
| `l3_security_report_stale_snapshot` | `gate2_pass is False`, `gate == 'HOLD'` |

Mỗi ca cũng phải đặt `gate`, `operating_state`, `can_write_affected`, `action` sao cho thoả các invariant chung của `check_results` (HOLD/PAUSED thì không được ghi).

- [ ] **Step 1: Viết `tests/test_orchestration_fixture.py`:**
  - `test_fixture_shape_and_unique_ids`: id không trùng; mọi `checks` chỉ dùng `equals`/`one_of`/`lte`; giá trị `orchestrator` nằm trong `ORCHESTRATORS`; đủ 12 id ở bảng trên.
  - `test_golden_responses_satisfy_checker`: với mỗi ca, dựng một response vàng (`required` mặc định hợp lệ, `reset_counters=False`, `repeat_arguments=False`, rationale không rỗng, sau đó áp mọi giá trị `equals` và phần tử đầu của `one_of`); kỳ vọng `check_results(cases, responses) == []`. Mục đích là chứng minh fixture không tự mâu thuẫn với các invariant của checker.
- [ ] **Step 2: Viết `tests/test_skill_docs.py`:**
  - `test_role_ids_documented`: mọi role id và `ct-reviewer-verify` đều xuất hiện trong `SKILL.md` và `references/agent-contracts.md`.
  - `test_no_legacy_command_syntax`: `SKILL.md`, `references/*.md`, `README.md` không chứa `/coding team `, `on all`, `off all` hay `` `inherit` ``. Có thể loại trừ đoạn mô tả việc chuyển từ bản cũ trong `control-policy.md` và `README.md` nếu đoạn đó được bọc bằng marker `<!-- legacy-migration -->`.
  - `test_skill_is_platform_neutral`: `SKILL.md` không chứa `Codex` hay `CODEX_`.
  - `test_relative_links_resolve`: mọi link tương đối trong `SKILL.md`, `references/*.md`, `agents/roles/*.md`, `README.md` đều tồn tại.
  - `test_orchestration_doc_states_advisory`: `references/orchestration.md` chứa `ADVISORY` và đủ 6 điều kiện, đúng thứ tự §6.2. Kiểm tra bằng chỉ mục của các cụm `L3`, `architect`, `interface`, `independent`, `plan`, `small`, sao cho chỉ mục tăng dần.
- [ ] **Step 3: Viết `tests/orchestration_cases.json`.**
- [ ] **Step 4: Chạy toàn bộ test.** Kỳ vọng PASS (sửa tài liệu ở Task 9 nếu test tài liệu FAIL).
- [ ] **Step 5: Commit và push** với message `test: orchestration fixture and documentation consistency checks`.

### Task 11: README, CONTRIBUTING, gói runtime và trình export

**Files:**
- Modify: `README.md`, `CONTRIBUTING.md`, `scripts/build_runtime_package.py`, `scripts/export_evaluation_inputs.py`, `tests/test_runtime_package.py`, `tests/test_evaluation_export.py`
- Create: `docs/smoke/platform-smoke.md`, `docs/smoke/result-template.json`

**Interfaces:**
- Consumes: CLI của Task 8.
- Produces:
  - `build_package(source_root, output=None, platform: str | None = None)`: tài nguyên đóng gói gồm thêm `platforms`; khi có `platform`, chỉ giữ `platforms/<platform>.json`. Có thêm tham số CLI `--platform`.
  - `export_inputs` chép thêm thư mục `platforms`.

Yêu cầu nội dung:
- **README:**
  - tên repo `chauvbm/coding-team`;
  - giới thiệu cho 3 nền tảng;
  - mỗi nền tảng một khối lệnh cài: `git clone https://github.com/chauvbm/coding-team.git` rồi `python scripts/coding_team.py install --platform <p> [--default on|off] [--global-rules]`, kèm `--dry-run` trước;
  - mục chuyển từ bản cài Codex cũ (`--replace-unmanaged`, khối chỉ dẫn được thay, state được giữ), bọc trong `<!-- legacy-migration -->`;
  - bảng lệnh;
  - bảng mức bảo đảm ENFORCED/ADVISORY theo nền tảng;
  - tóm tắt phân luồng và link `references/orchestration.md`;
  - `doctor`.
  - Bỏ hướng dẫn cũ dành riêng cho Codex (`agents/openai.yaml` chỉ còn nhắc là được cài cho Codex).
- **CONTRIBUTING:** đổi tên file zip thành `coding-team.zip`; mục gói runtime nhắc `--platform`; nêu rằng smoke test runtime chạy ngoài CI.
- **docs/smoke/platform-smoke.md:**
  - checklist theo spec §9B, 5 mục cho mỗi nền tảng;
  - cách cài vào một dự án tạm bằng `--scope project` để không đụng cấu hình người dùng;
  - prompt mẫu cho Leader (dispatch từng vai trò; yêu cầu một tầng khác mặc định; cho `ct-architect` thử ghi một file; cho một worker thử tạo agent con);
  - nơi lưu kết quả `evaluations/<YYYY-MM-DD>-platform-smoke/<platform>.json`.
- **result-template.json:** các khoá `platform`, `runtime_version`, `date`, `checks` (`dispatch`, `model_override`, `isolation`, `no_nesting`, `runtime_failure`, mỗi khoá có `status` thuộc `PASS`/`FAIL`/`NOT_RUN` và `evidence`).

- [ ] **Step 1: Cập nhật test:**
  - `test_runtime_package.py`: `setUp` tạo thêm `platforms/a.json` và `platforms/b.json`; kỳ vọng tập file đóng gói có thêm `platforms/a.json` và `platforms/b.json`. Thêm `test_platform_filter_keeps_one_config`: `platform='a'` thì chỉ còn `platforms/a.json`.
  - `test_evaluation_export.py`: thêm `'platforms'` vào các thư mục ở `setUp` và kiểm tra `skill/platforms/example.txt` tồn tại.
- [ ] **Step 2: Chạy hai file test đó.** Kỳ vọng FAIL.
- [ ] **Step 3: Sửa hai script; viết README, CONTRIBUTING, docs/smoke.**
- [ ] **Step 4: Chạy toàn bộ test** (gồm `test_skill_docs` với README). Kỳ vọng PASS.
- [ ] **Step 5: Commit và push** với message `docs: multi-platform README, smoke checklist; package platforms`.

### Task 12: Kiểm chứng cuối, smoke test Claude Code và cập nhật graph

**Files:**
- Create (chỉ khi người dùng đồng ý chạy smoke test): `evaluations/2026-11-13-platform-smoke/claude-code.json`

- [ ] **Step 1: Chạy kiểm tra cuối.** `python -m unittest discover -s tests -p "test_*.py" -v` và `git diff --check`. Kỳ vọng mọi test PASS (các SKIP có sẵn từ trước vẫn được phép) và không có lỗi khoảng trắng.
- [ ] **Step 2: Chạy dry-run trên cấu hình thật, chỉ đọc.** `python scripts/coding_team.py install --platform claude-code --dry-run --global-rules`. Kỳ vọng các đích nằm dưới `%USERPROFILE%\.claude` và không ghi gì. Báo kết quả cho người dùng.
- [ ] **Step 3: Hỏi người dùng** trước khi chạy smoke test Claude Code. Smoke test cài `--scope project` vào một thư mục tạm, rồi chạy `claude -p` ở đó; việc này tốn token và chạy một phiên Claude lồng. Nếu người dùng đồng ý: làm theo `docs/smoke/platform-smoke.md`, ghi kết quả vào file JSON theo template, mục nào không chạy được thì ghi `NOT_RUN` kèm lý do. Codex và Antigravity do người dùng tự chạy theo checklist.
- [ ] **Step 4: Cập nhật graph.** `graphify update .` từ thư mục gốc repo, rồi xác nhận `graphify-out/graph.json` tồn tại và đọc được.
- [ ] **Step 5: Commit và push** (nếu có file smoke) với message `test: record Claude Code platform smoke results`. Sau đó kiểm tra CI của lần push cuối bằng `gh run list --branch main --limit 1`, kỳ vọng `completed success`.
