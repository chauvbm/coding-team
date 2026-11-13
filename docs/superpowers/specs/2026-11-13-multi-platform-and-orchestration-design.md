# Thiết kế: coding-team đa nền tảng (Phần 1) và phân luồng với Superpowers (Phần 2)

- Ngày: 2026-11-13. Bản 2 đã cập nhật theo review của chuyên gia (xem §11).
- Trạng thái: chờ chủ repo duyệt
- Repo: `chauvbm/coding-team` (trước đây là `coding-team-codex`)

## 1. Mục đích và lý do

Hiện tại coding-team chỉ chạy được trên Codex: đường dẫn, biến môi trường, file chỉ dẫn toàn cục, metadata skill và tên model đều gắn với Codex. Chủ repo muốn **một mã nguồn duy nhất** cài được cho **Claude Code, Codex và Antigravity**.

Một chuyên gia đã chỉ ra rằng coding-team chồng chéo với các bộ điều phối của Superpowers là SDD và DPA. Vì vậy cần một chính sách phân luồng rõ ràng: **mỗi task chỉ có một bộ điều phối agent chính**.

Repo riêng tư `chauvbm/coding-team-agy` là nguồn tham khảo kỹ thuật cho Antigravity. Bản đó đã kiểm chứng `invoke_subagent` theo tầng, agent tĩnh có allowlist công cụ, và các đường dẫn `~/.gemini/...`. Repo này không phải nền của thiết kế.

**Nguyên tắc xuyên suốt của bản 2:** thiết kế không được hứa một bảo đảm mạnh hơn khả năng cưỡng chế thực tế của runtime. Mỗi bảo đảm về quyền, về model và về phân luồng đều phải ghi rõ mức của nó:
- `ENFORCED`: runtime thực sự chặn.
- `ADVISORY`: chỉ là chỉ dẫn cho model.

## 2. Mục tiêu, phi mục tiêu, điều cần tránh

**Mục tiêu**
- Cài được trên cả 3 nền tảng từ cùng một repo, bằng một công cụ dòng lệnh.
- Có 3 file cấu hình, mỗi nền tảng một file: đổi tầng model trừu tượng ra giá trị thật, khai báo đường dẫn và định danh của nền tảng.
- Dùng chung 6 vai trò. Agent tĩnh được sinh từ một nguồn, và mức cưỡng chế quyền của từng vai trò được báo cáo trung thực.
- Chuẩn hoá bộ lệnh `/coding-team`.
- Có chính sách phân luồng với Superpowers và phân loại skill.

**Phi mục tiêu**
- Không sửa Superpowers.
- Không thêm hook vào settings của người dùng.
- Không viết lại governance: Gate 1, Gate 2, STOP_HOLD, các ngân sách và định dạng ledger giữ nguyên ý nghĩa.
- Không chạy lại `benchmarks/` và `evaluations/` lịch sử.
- Benchmark đa nền tảng mới là một dự án tiếp theo, không thuộc phạm vi này (xem §10).

**Điều cần tránh** (nếu xảy ra thì coi là thất bại, kể cả khi mọi thứ "chạy được")
- Model trong cấu hình hoặc trong file agent tĩnh trở thành giá trị cố định, khiến Leader không ghi đè được.
- Công bố một vai trò là read-only, hoặc công bố đã "ngăn hai bộ điều phối", trong khi đó chỉ là chỉ dẫn.
- Hai bộ điều phối cùng chạy trên một giai đoạn, hoặc worker tự điều phối thêm agent.
- Trình cài đặt ghi đè cấu hình hay chỉ dẫn của người dùng mà không sao lưu, xoá file người dùng đã sửa, hoặc để lại bản cài nửa cũ nửa mới.
- Coding-team không còn chạy được khi không có Superpowers.

## 3. Các ràng buộc

- Python 3.11 trở lên, chỉ dùng thư viện chuẩn. CI chạy trên ma trận Ubuntu/Windows × Python 3.11/3.13 hiện có.
- Ghi file `.md` theo UTF-8 không BOM, vì Antigravity từ chối BOM.
- Ledger vẫn dùng ba vai trò cổng duyệt logic `Architect`, `Developer`, `QA`. Không đổi định dạng ledger. Thông tin điều phối được lưu trong metadata riêng (§6.6).
- Các sự thật về runtime đã kiểm chứng ngày 2026-11-13 với tài liệu chính thức. Người triển khai phải kiểm tra lại các mục ghi "cần xác minh".
  - **Claude Code**
    - Thứ tự chọn model của subagent: tham số `model` lúc gọi, rồi `model` trong frontmatter, rồi `CLAUDE_CODE_SUBAGENT_MODEL`, rồi model của phiên chính. Tham số lúc gọi chỉ nhận alias.
    - `CLAUDE_CODE_SUBAGENT_MODEL_FORCE` làm runtime bỏ qua cả frontmatter lẫn tham số lúc gọi.
    - `permissionMode: plan` bị bỏ qua nếu phiên chính đang ở chế độ `acceptEdits`, `bypassPermissions` hoặc auto.
    - Subagent được lồng tối đa 3 tầng, trừ khi bỏ `Agent` khỏi `tools`.
    - `CLAUDE_CONFIG_DIR` chuyển toàn bộ thư mục `~/.claude` sang chỗ khác.
  - **Codex**
    - Custom agent là file TOML trong `~/.codex/agents/` hoặc `.codex/agents/`, bắt buộc có `name`, `description`, `developer_instructions`.
    - `sandbox_mode` và `approval_policy` trong file agent **không** có hiệu lực, vì subagent kế thừa sandbox của phiên cha.
    - Nếu file agent đặt `model` hoặc `model_reasoning_effort`, giá trị trong file được ưu tiên.
  - **Antigravity**
    - `mainAgent` mặc định là `true`. `subagent` quyết định agent có gọi được qua `invoke_subagent` hay không.
    - `inheritCustomizations` chưa có trong tài liệu chính thức. Agy đã dùng trường này, cần xác minh lại.

## 4. Các hướng đã cân nhắc

| Hướng | Đánh đổi | Kết quả |
|---|---|---|
| A. Lõi chung + bộ chuyển đổi (adapter) cho từng nền tảng | Thêm một tầng sinh file, đổi lại mọi quy tắc chỉ có một nguồn | **Đã chọn**, chủ repo và chuyên gia cùng đồng ý |
| B. Ba bản skill riêng | Các bản sẽ lệch nhau, như codex và agy đã lệch | Loại |
| C. Một bản cài dùng chung, tự nhận diện nền tảng | Mỗi nền tảng đọc skill ở thư mục khác nhau nên không cài chung được | Loại; chỉ giữ việc nhận diện làm dự phòng |

- **Danh sách vai trò:** 6 vai trò hợp nhất (§5.1). Không dùng 6 vai trò cũ (vì `Specialist` quá chung chung) hay 7 vai trò của agy (tách backend/frontend dù cùng quyền).
- **Task cần phản biện:** coding-team giữ cả task (§6.4). Không làm Gate 1 rồi giao phần triển khai cho SDD.

## 5. Phần 1: đa nền tảng

### 5.1 Sáu vai trò và năng lực yêu cầu

Một vai trò chỉ được tách riêng khi nó khác hẳn ở **quyền** hoặc ở **tầng model mặc định**. Mỗi vai trò khai báo một tập `requested_capabilities` trung lập:

| ID | Trách nhiệm | requested_capabilities | Tầng ưu tiên | Vai trò ledger |
|---|---|---|---|---|
| `ct-architect` | Thiết kế, hợp đồng giao diện, ma trận phân chia file | `read` | `strong` | Architect |
| `ct-researcher` | Khảo sát code, tra tài liệu, điều tra một câu hỏi có giới hạn | `read`, `docs_lookup` | `light` | — |
| `ct-developer` | Triển khai. Có thể chạy nhiều phiên bản, mỗi phiên bản có một `focus` | `read`, `write:assigned`, `shell` | `standard` | Developer |
| `ct-test-engineer` | Viết và chạy test, cả E2E trên trình duyệt | `read`, `write:tests`, `shell`, `browser_testing` | `standard` | — |
| `ct-reviewer` | QA độc lập, có hai chế độ: `review` và `verify` (§5.4) | `review`: `read`. `verify`: `read`, `shell:checks` | `strong` | QA |
| `ct-security` | Kiểm tra bảo mật, hiệu năng, xử lý đồng thời. Bắt buộc ở L3 (§6.7) | `read` | `strong` | — (điều kiện của Gate 2 ở L3) |

Không vai trò nào có năng lực `delegate`, tức là không được dùng công cụ tạo thêm agent.

`browser_testing` là năng lực trung lập, cấu hình như sau:

```json
{ "provider": "auto", "allowed": ["playwright-cli", "playwright-mcp"], "target_policy": ["localhost", "approved-staging"] }
```

Adapter dò công cụ có sẵn rồi chọn một. Các công cụ không an toàn bị tắt: với Playwright MCP là `browser_run_code_unsafe` và `browser_file_upload`, như agy đã làm.

`Specialist` cũ bị bỏ. Câu hỏi chuyên môn đi theo `focus`, câu hỏi bảo mật giao cho `ct-security`.

### 5.2 Năng lực yêu cầu và năng lực thực tế (ENFORCED / ADVISORY)

Adapter của từng nền tảng đổi mỗi năng lực yêu cầu thành `effective_capabilities` kèm mức cưỡng chế. **Chỉ được ghi `ENFORCED` khi adapter chỉ ra được cơ chế runtime tương ứng.** Ma trận dự kiến:

| Năng lực | Claude Code | Codex | Antigravity |
|---|---|---|---|
| `read` (không ghi file) | ENFORCED: `tools` không có Edit/Write/NotebookEdit **và không có Bash/PowerShell**. Chỉ cấp MCP thuộc loại chỉ đọc | ADVISORY: subagent kế thừa sandbox của phiên cha. Chỉ thành ENFORCED khi chính phiên cha đang ở chế độ read-only | ENFORCED: allowlist `tools` không có công cụ ghi hay `run_command`, và `commandExecutionPolicy: off` |
| `write:assigned` (chỉ sửa file được giao) | ADVISORY: không runtime nào giới hạn được theo từng file | ADVISORY | ADVISORY |
| `write:tests` | ADVISORY | ADVISORY | ADVISORY |
| `shell` / `shell:checks` | Có shell nghĩa là **không còn** read-only ENFORCED; giới hạn lệnh là ADVISORY | Như cột trái | Như cột trái |
| không `delegate` | ENFORCED: không có `Agent` trong `tools` | ADVISORY (cần xác minh có cách chặn spawn trong role file) | ENFORCED: không có `invoke_subagent`/`define_subagent` |

Hệ quả:
- Handoff gửi cho worker và báo cáo cuối luôn ghi mức cưỡng chế thực tế. Nhãn ADVISORY không bao giờ được coi là bảo đảm an toàn.
- Một vai trò được dán nhãn read-only mà có cả shell thì không bao giờ được báo là ENFORCED.

### 5.3 Tầng model: Preferred Tier, Requested Model, Effective Model

Ba khái niệm được tách bạch:
- **Preferred Tier:** tầng mong muốn, lấy từ vai trò trong cấu hình hoặc do Leader chọn cho task.
- **Requested Model:** model hoặc effort được truyền đi lúc dispatch.
- **Effective Model:** model mà runtime thực sự dùng. Chỉ ghi khi có metadata làm bằng chứng; nếu không có thì ghi `unverified`.

`platforms/<tên>.json` (schema v2) thay cho `references/model-profiles.json`:

```json
{
  "schema_version": 2,
  "platform": "claude-code",
  "config_root": {"env": "CLAUDE_CONFIG_DIR", "default": "~/.claude"},
  "tiers": {
    "inherit":  {"model": null,     "effort": null},
    "strong":   {"model": "opus",   "effort": "high"},
    "standard": {"model": "sonnet", "effort": "medium"},
    "light":    {"model": "haiku",  "effort": "low"}
  },
  "roles": {"ct-architect": {"tier": "strong"}, "...": {}},
  "effort_vocabulary": ["low", "medium", "high", "xhigh", "max"],
  "static_agent_model": "default",
  "paths": {
    "skill_dir": "{config_root}/skills/coding-team",
    "agents_dir": "{config_root}/agents",
    "global_rules_file": "{config_root}/CLAUDE.md",
    "state_file": "{config_root}/coding-team/state.json"
  },
  "project_paths": {"skill_dir": ".claude/skills/coding-team", "agents_dir": ".claude/agents"},
  "chat_id_env": ["CLAUDE_CODE_SESSION_ID"],
  "detect_env": ["CLAUDECODE"],
  "force_model_env": ["CLAUDE_CODE_SUBAGENT_MODEL_FORCE"],
  "invocation": "/coding-team"
}
```

**`config_root`:** mỗi adapter có **một hàm duy nhất** xác định `config_root`, dựa theo biến môi trường (`CLAUDE_CONFIG_DIR`, `CODEX_HOME`, `ANTIGRAVITY_HOME`/`~/.gemini`) và giá trị mặc định. Mọi đường dẫn khác được tính từ `config_root`, không bao giờ tự mở rộng `~/.claude/...`.

**`static_agent_model`** quyết định model có được ghi vào file agent tĩnh hay không, nhằm tránh việc frontmatter cố định hoá model:
- `default`: ghi tầng của vai trò làm mặc định. Chỉ dùng khi runtime **đã xác minh** rằng model truyền lúc dispatch thắng giá trị trong file. Claude Code từ v2.1.251 trở lên dùng `default`.
- `omit`: không ghi model vào file agent; Leader luôn truyền model lúc dispatch. **Codex dùng `omit`**, vì giá trị trong file TOML thắng giá trị truyền lúc spawn. Antigravity dùng `omit` cho đến khi xác minh được thứ tự ưu tiên giữa `model` tĩnh và tầng truyền vào `invoke_subagent`.

Giá trị gợi ý theo nền tảng. Người triển khai kiểm tra lại rồi ghi vào file cấu hình.

| | Claude Code | Codex | Antigravity |
|---|---|---|---|
| Tầng strong/standard/light | `opus`/`sonnet`/`haiku` + effort | Tên model GPT + `model_reasoning_effort` (cần xác minh) | `pro`/`flash`/`flash_lite`, không có effort |
| `config_root` | `CLAUDE_CONFIG_DIR` → `~/.claude` | `CODEX_HOME` → `~/.codex` | `~/.gemini` (config tại `config/`, state tại `antigravity/`) |
| Thư mục skill | `{root}/skills/` | `~/.agents/skills/` (cần xác minh có nằm trong `CODEX_HOME` không) | `{root}/config/skills/` |
| Agent tĩnh | `{root}/agents/ct-*.md` | `{root}/agents/ct-*.toml` | `{root}/config/agents/ct-*/agent.md` |
| Chỉ dẫn toàn cục | `{root}/CLAUDE.md` | `{root}/AGENTS.md` | `{root}/config/rules/coding-team-policy.md` |
| Biến chứa mã chat | `CLAUDE_CODE_SESSION_ID` | `CODEX_THREAD_ID` | `ANTIGRAVITY_CONVERSATION_ID`, `ANTIGRAVITY_CHAT_ID` |
| Biến nhận diện nền tảng | `CLAUDECODE=1` | `CODEX_THREAD_ID` hoặc `CODEX_HOME` | `ANTIGRAVITY_*` |
| Cách gọi skill | `/coding-team …` | `$coding-team …` (cần xác minh xem `/coding-team` có được chuyển nguyên văn không) | `/coding-team …` |

**Quy tắc chọn model** giữ nguyên tinh thần `model-routing.md`, theo thứ tự ưu tiên:
1. Người dùng ghim model, hoặc STRICT_MODEL đang bật.
2. Leader chủ động chọn cho task cụ thể, có lý do.
3. Preferred Tier của vai trò.

Leader được nâng hoặc hạ tầng theo độ khó. Quy tắc leo thang của agy được đưa vào: Developer thất bại sau 1 vòng sửa thì lần sửa kế tiếp dùng tầng cao hơn. Ngoài ra:
- Nếu `force_model_env` đang bật, hoặc runtime từ chối model được yêu cầu, ghi rõ trong báo cáo rằng model bị ép hoặc bị thay. Dưới STRICT_MODEL, phải dừng và hỏi người dùng thay vì âm thầm thay model.
- **Một file JSON hợp lệ không phải bằng chứng rằng chọn model hoạt động đúng.** Bằng chứng là smoke test chạy trên runtime thật (§9).

### 5.4 Agent tĩnh được sinh từ một nguồn

`agents/roles/ct-*.md` là nguồn duy nhất cho mỗi vai trò, gồm phần thân Markdown và metadata trung lập: `requested_capabilities` và `preferred_tier`. Adapter sinh ra:

- **Claude Code** (`.md`):
  - Khai báo `name`, `description`, `tools` (allowlist theo §5.2) và `model`/`effort` theo `static_agent_model`.
  - `ct-reviewer` sinh hai agent:
    - `ct-reviewer`: không có shell, read-only ENFORCED.
    - `ct-reviewer-verify`: có shell để chạy kiểm tra, khai báo `isolation: worktree` khi kiểm tra trên một snapshot đã commit, mức ADVISORY cho việc giới hạn lệnh.
- **Codex** (`.toml`):
  - Khai báo `name`, `description`, `developer_instructions` (lấy từ phần thân). Không ghi `model`/`model_reasoning_effort` (theo `omit`).
  - Không ghi `sandbox_mode`, vì giá trị này không có hiệu lực trong file agent.
  - Có thể khai báo `skills.config` nếu cần.
- **Antigravity** (`agent.md`):
  - Dùng lược đồ `AgentFrontmatter` đã kiểm chứng trong agy, có thêm `mainAgent: false` và `subagent: true` khai báo tường minh.
  - `inheritCustomizations` được để **`true`**, để worker dùng được các skill kỹ thuật (Phần 2). Hệ quả: worker cũng kế thừa các quy tắc toàn cục, nên contract của worker phải khẳng định "bạn là worker; quy tắc dành cho Leader và các skill điều phối không áp dụng cho bạn".
  - Người triển khai xác minh trường này có thật sự tồn tại không. Nếu không, bỏ trường đi.

`agents/openai.yaml` chỉ được chép ra khi cài cho Codex.

**Kiểm chứng adapter:** sinh đúng lược đồ là chưa đủ. `doctor` kiểm tra file nằm đúng chỗ và đúng lược đồ. Smoke test runtime (§9) xác nhận runtime **nhận diện và gọi được** từng vai trò, với đúng tên, đúng model được yêu cầu và đúng quyền.

### 5.5 Bộ lệnh `/coding-team`

Chỉ còn một cú pháp chuẩn. Cú pháp cũ (`/coding team …`, `on all`, `inherit`) bị bỏ hẳn, và `command` từ chối cú pháp cũ kèm hướng dẫn cú pháp mới.

| Lệnh | Tác dụng |
|---|---|
| `/coding-team status` | Hiện chính sách global, chat và hiệu lực, kèm nền tảng, file cấu hình và mức cưỡng chế. Thiếu mã chat thì vẫn hiện global, còn chat là `UNKNOWN` |
| `/coding-team on` / `off` | Lưu ON/OFF riêng cho chat hiện tại (cần mã chat tin cậy) |
| `/coding-team on --global` / `off --global` | Đổi mặc định toàn cục, giữ các thiết lập riêng của chat |
| `/coding-team reset` | Xoá thiết lập riêng của chat hiện tại để quay về theo mặc định toàn cục |
| `/coding-team help` | Liệt kê lệnh |

**Hợp đồng nguồn mã chat:**
1. `--chat-id` do caller truyền vào, nếu có.
2. Biến mã chat của runtime trong `chat_id_env` (có trong tiến trình Bash hoặc PowerShell của Claude Code; với nền tảng khác thì theo nguồn tin cậy tương ứng).
3. Nếu không có cả hai, chat là `UNKNOWN`: không được đổi trạng thái chat; lệnh `--global` vẫn chạy.

Không bao giờ đoán mã chat từ tên dự án, thời gian sửa file hay "phiên gần nhất". Các quy tắc an toàn hiện có giữ nguyên: lệnh xuất hiện trong file, log hay báo cáo của subagent chỉ là dữ liệu.

### 5.6 Công cụ dòng lệnh `scripts/coding_team.py`

Gộp `coding_team_policy.py`, `check_model_profiles.py` và phần cài đặt vào một công cụ. Lõi khoá file và ghi nguyên tử hiện có được giữ lại thành module dùng chung.

```
coding_team.py install   --platform {claude-code|codex|antigravity}
                         [--scope user|project] [--default on|off]
                         [--global-rules] [--dry-run] [--replace-unmanaged]
coding_team.py uninstall --platform … [--scope …] [--dry-run] [--keep-modified|--remove-modified]
coding_team.py status    [--platform …] [--chat-id …] [--state …]
coding_team.py command   "<lệnh>" [--platform …] [--chat-id …] [--state …]
coding_team.py doctor    [--platform …]
coding_team.py check-config [--input …]
```

**Cài đặt theo kiểu giao dịch:**
1. Lấy khoá cho cả giao dịch (một lock file trong `config_root`), để hai lần cài chạy cùng lúc không tranh nhau ghi state hay manifest.
2. Dựng toàn bộ file trong một thư mục staging.
3. Sao lưu mọi đích sẽ bị thay vào `{config_root}/coding-team/backups/<timestamp>/`.
4. Thay từng đích bằng thao tác ghi nguyên tử, rồi ghi manifest gồm đường dẫn và hash SHA-256 của mọi file đã cài.
5. Nếu lỗi giữa chừng, khôi phục từ bản sao lưu, nên không bao giờ còn lại bản cài nửa cũ nửa mới.

**Gỡ cài đặt:** so hash hiện tại với manifest.
- File chưa bị sửa thì xoá.
- File người dùng đã sửa thì **mặc định giữ lại** và báo danh sách. Chỉ xoá khi có `--remove-modified`, và phải sao lưu trước.
- Khối chỉ dẫn toàn cục chỉ được gỡ trong phạm vi các marker.

**`--replace-unmanaged`** thay cho `--force` vốn quá rộng. Cờ này chỉ cho phép thay một đích **không** do coding-team quản lý (không có trong manifest), ví dụ bản git clone cũ. Phải sao lưu trước và báo chính xác từng đường dẫn bị ảnh hưởng. Cờ này không bỏ qua bất kỳ lỗi hay kiểm tra an toàn nào khác.

**Scope:**
- `--scope project` chỉ đặt skill và agent tĩnh vào thư mục của dự án.
- **State bật/tắt luôn ở cấp người dùng**, nên cài theo dự án không bao giờ đổi `global_policy` của các repo khác. Kết hợp `--scope project` với `--default` sẽ bị từ chối kèm giải thích.
- Với `--scope user`, `--default` chỉ đổi `global_policy`, các thiết lập riêng của chat được giữ. Không truyền `--default` thì state có sẵn giữ nguyên.

**Chuyển từ bản cài Codex cũ:**
- Thư mục cài cũ là bản git clone, không có manifest, nên `install` báo lỗi và hướng dẫn dùng `--replace-unmanaged`.
- `--global-rules` dùng lại đúng marker cũ, nên khối chỉ dẫn cũ (vẫn chứa lệnh cũ) được thay bằng khối mới.
- State schema v1 được giữ nguyên, nên các thiết lập ON/OFF cũ dùng tiếp được.

**`doctor`** báo cáo:
- Bản cài có khớp manifest không, và những file đã bị sửa.
- Agent tĩnh và ma trận năng lực thực tế (ENFORCED/ADVISORY) cho từng vai trò.
- Biến ép model có đang bật không.
- State có hỏng không.
- Khối chỉ dẫn toàn cục có hay không.
- Superpowers: `installed` (tìm thấy trên đĩa) và `enabled` (bật trong cấu hình của nền tảng, nếu đọc được). Còn `available_in_session` thì **chỉ Leader biết** lúc chạy, dựa vào danh sách skill thực tế của phiên. `doctor` không được kết luận thay.

`build_runtime_package.py` có thêm `--platform`.

## 6. Phần 2: phân luồng với Superpowers

### 6.1 Quy tắc cốt lõi và mức bảo đảm

**Mỗi giai đoạn của một task chỉ có một bộ điều phối agent chính.** Một task có thể đi qua nhiều giai đoạn, ví dụ DPA điều tra xong rồi coding-team triển khai, nhưng tại mỗi thời điểm chỉ có một bộ điều phối.

Mức bảo đảm phải nói trung thực. Vì không sửa Superpowers và không thêm hook, phân luồng là **ADVISORY có kiểm tra**:
- Leader đọc chính sách, chọn bộ điều phối, báo một dòng cho người dùng và ghi lại lựa chọn.
- Coding-team tự kiểm tra trước khi nhận task (§6.6).

Tài liệu và báo cáo **không được** tuyên bố đã ngăn tuyệt đối việc hai bộ điều phối chạy song song.

### 6.2 Tiêu chí phân luồng

Áp dụng từ trên xuống, dừng ở điều kiện đầu tiên khớp. Người dùng chỉ định rõ bộ điều phối thì luôn thắng, trừ khi chính sách an toàn cấm.

| # | Điều kiện | Bộ điều phối |
|---|---|---|
| 1 | L3: hậu quả đáng kể về bảo mật, toàn vẹn dữ liệu, production hoặc không thể đảo ngược | coding-team (trừ khi người dùng chỉ định khác và chính sách an toàn cho phép) |
| 2 | Có mâu thuẫn kiến trúc hoặc lựa chọn thiết kế cần phản biện, hoặc người dùng yêu cầu phản biện hay đồng thuận | coding-team |
| 3 | Thay đổi nhiều hợp đồng giao diện, lược đồ dữ liệu hoặc API giữa các module | Ưu tiên coding-team |
| 4 | Nhiều vấn đề **độc lập** cần điều tra | DPA, chỉ để điều tra. Sau đó qua điểm bàn giao (§6.5) rồi xét lại bảng này để chọn bên triển khai |
| 5 | Có plan, phạm vi rõ, review tuần tự sau từng task là đủ | SDD |
| 6 | Task nhỏ, ít tác động (L0 hoặc L1 có giới hạn) | Leader làm trực tiếp, có thể dùng skill kỹ thuật |

**Không dùng số file hay số module làm tiêu chí duy nhất.** Đổi tên một symbol trên 10 file vẫn có thể là mục 6; sửa xác thực trong 2 file vẫn là mục 1.

Khi máy không có Superpowers, mục 4 và mục 5 chuyển sang coding-team AUTO.

Các chế độ hiện có (AUTO, SOLO, PLAN_ONLY, REVIEW_ONLY, FULL_TEAM, STRICT_MODEL, USER_APPROVAL_REQUIRED) giữ nguyên ý nghĩa khi coding-team là bộ điều phối. AUTO không còn tự nhận L1; L1 thuộc về Leader làm trực tiếp, trừ khi người dùng yêu cầu coding-team.

### 6.3 Phân loại skill

| Nhóm | Skill | Ai được dùng |
|---|---|---|
| Bộ điều phối | coding-team, subagent-driven-development, dispatching-parallel-agents, executing-plans | Chỉ Leader, mỗi giai đoạn một bộ |
| Giai đoạn | brainstorming, writing-plans, using-git-worktrees, finishing-a-development-branch | Chỉ Leader |
| Kỹ thuật | test-driven-development, systematic-debugging, verification-before-completion, receiving-code-review | Leader và mọi worker |
| Cấm với worker | requesting-code-review (tự tạo thêm reviewer), mọi skill thuộc nhóm bộ điều phối | — |

Handoff của coding-team gọi tên các skill kỹ thuật mà worker được dùng, vì `using-superpowers` có cơ chế `SUBAGENT-STOP` nên worker không tự gọi skill. Mức chặn tạo thêm agent của worker theo §5.2: ENFORCED ở nơi runtime cho phép, còn lại là ADVISORY.

### 6.4 Coding-team giữ cả task

Khi coding-team là bộ điều phối, nó giữ task từ Gate 1 qua phần triển khai tới Gate 2. Không giao phần triển khai cho SDD, vì cách "tự quyết để không dừng việc" của SDD sẽ đi qua các cổng HOLD, ma trận phân chia file và yêu cầu kiểm chứng trên cùng snapshot.

Coding-team **được dùng** spec/plan từ brainstorming hoặc writing-plans (`docs/superpowers/specs|plans/…`) làm đầu vào Spec/Plan cho Gate 1.

### 6.5 Điểm bàn giao giữa các giai đoạn

Chỉ đổi bộ điều phối tại điểm bàn giao. Điểm bàn giao xảy ra khi:
- SDD dừng ở một trong 4 lý do của nó;
- coding-team dừng ở STOP_HOLD hoặc NEEDS_USER_DECISION;
- DPA điều tra xong;
- người dùng yêu cầu đổi.

Bản ghi bàn giao **bắt buộc** gồm:
- lý do bàn giao, bộ điều phối cũ và bộ điều phối mới;
- **trạng thái thực của các worker**: danh sách worker đã thật sự kết thúc, có bằng chứng từ runtime, chứ không chỉ là báo cáo "đã dừng". Nếu còn worker chưa kết thúc thì chưa được bàn giao;
- snapshot: `HEAD` cùng danh sách thay đổi chưa commit và file chưa được git theo dõi;
- trạng thái test gần nhất: lệnh đã chạy, kết quả, và snapshot mà test đã chạy trên đó;
- các phát hiện chưa xử lý và đường dẫn ledger hay workspace của bộ điều phối cũ.

Bộ điều phối mới tiếp nhận các phát hiện này, không bắt đầu lại từ số 0.

### 6.6 Trạng thái điều phối và kiểm tra trước khi nhận task

Coding-team lưu metadata điều phối **tách khỏi định dạng ledger**, ví dụ trong `.coding-team/orchestration.json` nằm ở thư mục làm việc và được git-ignore:

```json
{ "task_id": "task-001", "orchestrator": "coding-team", "mode": "AUTO",
  "phase": "implementation", "handoff_from": null, "handoff_record": null }
```

Trước khi nhận task, coding-team kiểm tra:
- Có workspace SDD nào **đang dở** cho cùng plan không (`.superpowers/sdd/<plan>/progress.md` tồn tại nhưng chưa có dòng hoàn tất).
- Có metadata điều phối nào đang hoạt động thuộc bộ điều phối khác không.

Nếu có, coding-team không nhận task. Nó báo cho người dùng và yêu cầu một điểm bàn giao theo §6.5. Đây là kiểm tra phía coding-team; nó không chặn được SDD tự khởi động, và tài liệu phải ghi rõ giới hạn này.

### 6.7 `ct-security` ở L3 và Gate 2

Ledger không đổi, nhưng Gate 2 ở L3 có thêm điều kiện bắt buộc:
- `ct-security` tạo một báo cáo có kết luận và bằng chứng, gắn với **đúng snapshot** đang được kiểm chứng ở Gate 2.
- `ct-reviewer` (vai trò QA trong ledger) kiểm tra báo cáo này và đưa vào verdict của Gate 2.
- Gate 2 **không được PASS** nếu báo cáo bị thiếu, chưa hoàn tất, gắn với snapshot khác, hoặc còn phát hiện BLOCKER/MAJOR chưa xử lý. Ngoại lệ duy nhất là một chấp thuận ngoại lệ hợp lệ của người dùng, được ghi lại.
- Source thay đổi sau khi security review xong thì kết quả review trên các file bị ảnh hưởng mất hiệu lực, giống quy tắc tự động vô hiệu hoá hiện có.

### 6.8 Đặt quy tắc phân luồng ở đâu

- **SKILL.md:** thêm mục "Chọn bộ điều phối", tham chiếu tới file mới `references/orchestration.md`.
- **Khối chỉ dẫn toàn cục** do `--global-rules` ghi: chứa bảng §6.2 rút gọn. Đây là cách khiến model làm theo, không phải cơ chế cưỡng chế (§6.1).

## 7. Các file bị ảnh hưởng

- **Thêm mới:**
  - `platforms/{claude-code,codex,antigravity}.json` và `agents/roles/ct-*.md` (6 file).
  - `references/orchestration.md`.
  - `scripts/coding_team.py` và các module của nó.
  - Test cho cài đặt và gỡ cài đặt, sinh agent, lệnh, cấu hình, ma trận năng lực, phân luồng.
  - Kịch bản smoke test runtime (§9).
- **Sửa:**
  - `SKILL.md`.
  - `references/{agent-contracts,model-routing,control-policy,workflow,debate-governance,quality-safety}.md`.
  - `README.md` và `CONTRIBUTING.md`: tên repo mới, cài cho 3 nền tảng, hướng dẫn chuyển từ bản cài cũ.
  - `build_runtime_package.py`.
  - Các file trong `tests/*` liên quan.
  - `.github/workflows/ci.yml` nếu cần.
- **Thay thế:** `references/model-profiles.json`, `scripts/check_model_profiles.py`, `scripts/coding_team_policy.py`. Chỉ giữ wrapper mỏng nếu test hay tài liệu lịch sử bắt buộc.
- **Không động tới:** `benchmarks/`, `evaluations/`. Không đổi định dạng ledger.

## 8. Những gì để người triển khai tự quyết

- Giá trị cụ thể của các mục ghi "cần xác minh" trong §3 và §5.3, ghi vào file cấu hình kèm ngày xác minh.
- Cách chia module bên trong `coding_team.py`.
- Thứ tự commit: mỗi phần hoàn chỉnh là một commit, push ngay theo yêu cầu của chủ repo.

## 9. Tiêu chí hoàn thành

**A. Kiểm thử tự động (chạy trong CI)**
1. `install --dry-run` cho cả 3 nền tảng liệt kê đúng các đích ghi, không ghi gì. Đường dẫn được tính từ `config_root`, kể cả khi đặt `CLAUDE_CONFIG_DIR` hay `CODEX_HOME`.
2. Cài vào thư mục home giả lập: file nằm đúng chỗ, đúng lược đồ (TOML cho Codex; `mainAgent: false` cho Antigravity), UTF-8 không BOM. Codex không có `model` trong file agent.
3. Cài lại lần hai không thay đổi gì. Lỗi giữa giao dịch (giả lập) thì khôi phục đúng trạng thái cũ. Hai lần cài chạy song song thì một bên chờ khoá.
4. Gỡ cài đặt giữ file người dùng đã sửa, trừ khi có `--remove-modified`. Gỡ chỉ xoá những gì có trong manifest.
5. Scope: `--scope project --default …` bị từ chối. Cài theo dự án không đổi `global_policy`.
6. Chuyển từ bản cài cũ: bản clone cũ cần `--replace-unmanaged`; khối chỉ dẫn cũ được thay; state cũ được giữ.
7. Lệnh mới cho đúng kết quả; cú pháp cũ bị từ chối kèm hướng dẫn; thiếu mã chat thì `status` vẫn hiện global và chat là `UNKNOWN`; hai mã chat khác nhau không ghi đè lên nhau.
8. `check-config` từ chối cấu hình hỏng.
9. Ma trận năng lực: một vai trò `read` mà có shell không bao giờ được báo ENFORCED; `write:assigned` luôn là ADVISORY.
10. Ca hành vi (fixture JSON):
    - Chọn đúng bộ điều phối theo §6.2, kể cả ca "10 file đổi tên" và ca "xác thực 2 file".
    - Worker không dùng skill bị cấm.
    - Không có Superpowers thì chuyển về AUTO.
    - Có SDD đang dở thì coding-team từ chối nhận task.
    - Bàn giao thiếu trạng thái thực của worker thì bị từ chối.
    - L3 thiếu báo cáo `ct-security` hoặc báo cáo gắn với snapshot khác thì Gate 2 không PASS.
11. Toàn bộ `python -m unittest discover -s tests -p "test_*.py"` đạt, CI xanh.

**B. Smoke test runtime** (chạy tay hoặc bằng script trên máy có runtime thật, không chạy trong CI; kết quả lưu vào `evaluations/<ngày>-platform-smoke/`). Với mỗi nền tảng:
1. Dispatch từng vai trò: runtime nhận đúng tên vai trò. Ghi Requested Model và Effective Model (hoặc `unverified`).
2. Ghi đè model: Leader yêu cầu một tầng khác với mặc định và thấy được hiệu lực, hoặc ghi rõ là không xác minh được. Kiểm tra thêm STRICT_MODEL và biến ép model.
3. Cách ly: vai trò `read` được báo ENFORCED thử ghi file thì bị runtime từ chối.
4. Không lồng agent: worker không tạo thêm được agent ở nơi báo ENFORCED.
5. Lỗi runtime: agent lỗi, hết thời gian hoặc bị huỷ, và trường hợp thiếu metadata model, đều được báo cáo trung thực, không bịa kết quả.

Báo cáo smoke test ghi phiên bản runtime và ngày chạy. Mọi mục không chạy được thì ghi `NOT_RUN` kèm lý do.

## 10. Việc tiếp theo, ngoài phạm vi

- **Benchmark đa nền tảng:** cùng một task với acceptance cố định, chạy trên từng nền tảng với các cấu hình Native solo, Superpowers SDD, coding-team AUTO và coding-team FULL. Đo thời gian, token, chi phí, tỷ lệ pass, lỗi tích hợp và số vòng review. Chỉ so sánh trong cùng điều kiện chạy.
- Thống kê chi phí và tần suất sử dụng của từng vai trò.

## 11. Thay đổi so với bản 1 (theo review của chuyên gia)

| Mục review | Xử lý |
|---|---|
| P0: quyền runtime chưa đồng nhất | Nhận. Thêm §5.2 (requested/effective, ENFORCED/ADVISORY); tách `ct-reviewer` thành review và verify |
| P0: phân luồng chỉ dựa vào CLAUDE.md | Nhận. §6.1 ghi rõ là ADVISORY có kiểm tra; thêm §6.6 (metadata điều phối, kiểm tra SDD đang dở) |
| P0: frontmatter cố định hoá model | Nhận. §5.3 tách Preferred/Requested/Effective; thêm `static_agent_model` (Codex `omit`, vì đã xác minh file TOML thắng giá trị lúc spawn); xử lý biến ép model |
| P1: adapter | Nhận. Codex sinh TOML; Antigravity `mainAgent: false`, `subagent: true` |
| P1: `inheritCustomizations: false` | Điều chỉnh: để `true` để worker dùng được skill kỹ thuật, kèm điều khoản trong contract worker |
| P1: `ct-security` ở L3 | Nhận. Thêm §6.7 |
| P1: rollback và migration của trình cài đặt | Nhận. Cài theo kiểu giao dịch, so hash khi gỡ, khoá giao dịch, scope tách bạch, `--replace-unmanaged` thay `--force` |
| P1: smoke test runtime | Nhận. Thêm §9B, chạy ngoài CI vì cần runtime thật và đăng nhập |
| `CLAUDE_CONFIG_DIR` / `config_root` | Nhận. Một hàm xác định `config_root` cho mỗi adapter |
| Hợp đồng mã chat | Nhận. Thêm vào §5.5 |
| Tiêu chí phân luồng còn chủ quan | Nhận. Bảng có thứ tự ở §6.2, không dùng số file làm tiêu chí duy nhất |
| `doctor` phân biệt các mức Superpowers | Điều chỉnh: `doctor` chỉ biết `installed` và `enabled`; `available_in_session` do Leader xác định lúc chạy |
| Năng lực browser trung lập | Nhận (§5.1) |
| Benchmark đa nền tảng | Điều chỉnh: đưa sang dự án tiếp theo (§10), không phải điều kiện để duyệt thiết kế |
