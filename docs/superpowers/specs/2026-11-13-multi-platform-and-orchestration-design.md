# Thiết kế: coding-team đa nền tảng (Phần 1) và phân luồng với Superpowers (Phần 2)

- Ngày: 2026-11-13
- Trạng thái: chờ chủ repo duyệt
- Repo: `chauvbm/coding-team` (trước đây là `coding-team-codex`)

## 1. Mục đích và lý do

Hiện tại coding-team chỉ chạy được trên Codex: đường dẫn, biến môi trường, file chỉ dẫn toàn cục, metadata skill và tên model đều gắn với Codex. Chủ repo muốn **một mã nguồn duy nhất** cài được cho **Claude Code, Codex và Antigravity**.

Ngoài ra, một chuyên gia đã review và chỉ ra rằng coding-team chồng chéo với các bộ điều phối của Superpowers (Subagent-Driven Development và Dispatching Parallel Agents). Vì vậy cần một chính sách phân luồng rõ ràng: **mỗi task chỉ có một bộ điều phối agent chính**.

Repo `chauvbm/coding-team-agy` là bản tách riêng cho Antigravity. Bản đó đã kiểm chứng thực tế các tính năng của Antigravity: `invoke_subagent` với các tầng model, agent tĩnh có danh sách công cụ được phép, và các đường dẫn `~/.gemini/...`. Thiết kế này dùng bản agy làm nguồn tham khảo kỹ thuật cho Antigravity. Bản agy không phải là nền của thiết kế.

## 2. Mục tiêu, phi mục tiêu, điều cần tránh

**Mục tiêu**
- Cài được trên cả 3 nền tảng từ cùng một repo, bằng một công cụ dòng lệnh duy nhất.
- Có 3 file cấu hình, mỗi nền tảng một file. Mỗi file định nghĩa model mặc định cho từng vai trò cùng các đường dẫn và định danh riêng của nền tảng đó.
- Dùng chung một danh sách 6 vai trò. Trên mỗi nền tảng, mỗi vai trò có một file agent tĩnh, và quyền của vai trò được giới hạn ở tầng runtime khi nền tảng hỗ trợ.
- Chuẩn hoá bộ lệnh `/coding-team`.
- Có chính sách phân luồng với Superpowers và phân loại skill thành bộ điều phối, giai đoạn và kỹ thuật.

**Phi mục tiêu**
- Không sửa Superpowers.
- Không viết lại phần governance: Gate 1, Gate 2, STOP_HOLD, các ngân sách và task ledger vẫn giữ nguyên ý nghĩa.
- Không chạy lại hay sửa các benchmark và evaluation lịch sử trong `benchmarks/` và `evaluations/`.

**Điều cần tránh** (nếu xảy ra thì coi là thất bại, kể cả khi mọi thứ "chạy được")
- Model trong cấu hình bị dùng như một giá trị cố định. Đây chỉ là mặc định; Leader vẫn tự điều chỉnh, trừ khi người dùng ghim model hoặc bật STRICT_MODEL.
- Hai bộ điều phối cùng chạy trên một task, hoặc worker tự điều phối thêm agent.
- Trình cài đặt ghi đè cấu hình hay chỉ dẫn của người dùng mà không sao lưu, hoặc ghi ra ngoài các khối có đánh dấu.
- Coding-team không còn chạy được khi máy không cài Superpowers.

## 3. Các ràng buộc

- Python 3.11 trở lên, chỉ dùng thư viện chuẩn. CI chạy trên ma trận Ubuntu/Windows × Python 3.11/3.13 hiện có.
- Ghi file `.md` theo UTF-8 **không BOM**, vì bộ đọc frontmatter của Antigravity từ chối BOM.
- Không ghi đè cài đặt có sẵn của người dùng. Nếu có ghi thì phải sao lưu trước và hỗ trợ `--dry-run`.
- Task ledger vẫn dùng ba vai trò cổng duyệt logic `Architect`, `Developer`, `QA`. Danh sách vai trò mới ánh xạ vào ba vai trò này (xem §5.1). Ledger không đổi định dạng.

## 4. Các hướng đã cân nhắc

| Hướng | Mô tả | Đánh đổi | Kết quả |
|---|---|---|---|
| A. Lõi chung + bộ chuyển đổi cho từng nền tảng (**đã chọn**) | SKILL.md và các file trong references dùng chung. `platforms/<tên>.json` mô tả từng nền tảng. Trình cài đặt sinh ra các file riêng của nền tảng. | Thêm một tầng sinh file, đổi lại mọi quy tắc chỉ có một nguồn. | Chủ repo đồng ý |
| B. Ba bản skill riêng, mỗi nền tảng một bản | Sao chép SKILL.md và references thành 3 bản | Dễ viết lúc đầu nhưng các bản sẽ lệch nhau, như codex và agy đã lệch | Loại |
| C. Một bản cài dùng chung, tự nhận ra nền tảng lúc chạy | Một thư mục cài, đoán nền tảng qua biến môi trường | Mỗi nền tảng đọc skill ở thư mục khác nhau nên không cài chung được | Loại; chỉ giữ việc nhận diện nền tảng làm phương án dự phòng trong `doctor` |

Danh sách vai trò: chọn **6 vai trò hợp nhất** (§5.1). Không dùng 6 vai trò cũ của codex (`Specialist` quá chung chung) và không dùng 7 vai trò của agy (tách backend/frontend dù cùng quyền và cùng model).

Bộ điều phối cho task cần phản biện: **coding-team giữ cả task** (§6.4). Không chia giai đoạn kiểu Gate 1 do coding-team làm rồi giao phần triển khai cho SDD.

## 5. Phần 1: đa nền tảng

### 5.1 Sáu vai trò

Một vai trò chỉ được tách riêng khi nó khác hẳn ở **quyền** hoặc ở **tầng model mặc định**.

| ID | Trách nhiệm | Quyền | Tầng mặc định | Vai trò cổng duyệt (ledger) |
|---|---|---|---|---|
| `ct-architect` | Thiết kế, hợp đồng giao diện, ma trận phân chia file | Chỉ đọc | `strong` | Architect |
| `ct-researcher` | Khảo sát code, tra tài liệu, điều tra một câu hỏi có giới hạn (thay cho Investigator) | Chỉ đọc, được dùng MCP tra tài liệu nếu có | `light` | — |
| `ct-developer` | Triển khai. Có thể chạy nhiều phiên bản cùng lúc, mỗi phiên bản nhận tham số `focus` (backend, frontend, data…) | Ghi file và chạy lệnh, chỉ trong phạm vi file được giao | `standard` | Developer |
| `ct-test-engineer` | Viết và chạy test, gồm cả E2E trên trình duyệt khi có MCP trình duyệt | Ghi file test và chạy lệnh | `standard` | — |
| `ct-reviewer` | QA độc lập, không sửa phần triển khai | Chỉ đọc và chạy các kiểm tra không phá huỷ | `strong` | QA |
| `ct-security` | Kiểm tra bảo mật, hiệu năng và xử lý đồng thời. Chỉ gọi khi có rủi ro; bắt buộc ở L3 | Chỉ đọc | `strong` | — (người duyệt bổ sung ở L3) |

Vai trò `Specialist` cũ bị bỏ. Các câu hỏi chuyên môn đi theo `focus` của `ct-developer` hoặc `ct-reviewer`, còn câu hỏi bảo mật giao cho `ct-security`. Contract trong `references/agent-contracts.md` được viết lại cho 6 vai trò này. Contract của các vai trò kiểm tra (`ct-reviewer`, `ct-test-engineer`) lấy thêm từ bản agy: e2e chỉ được nhắm vào localhost hoặc staging, và tắt các công cụ Playwright không an toàn.

### 5.2 Tầng model trừu tượng và 3 file cấu hình nền tảng

Vai trò không gắn trực tiếp với tên model. Mỗi vai trò có một **tầng trừu tượng**: `inherit` (dùng model của Leader), `strong`, `standard` hoặc `light`. File cấu hình của từng nền tảng đổi tầng trừu tượng thành giá trị mà nền tảng đó hiểu.

`platforms/claude-code.json`, `platforms/codex.json`, `platforms/antigravity.json` thay cho `references/model-profiles.json`. Lược đồ chung (schema v2):

```json
{
  "schema_version": 2,
  "platform": "claude-code",
  "tiers": {
    "inherit":  {"model": null,     "effort": null},
    "strong":   {"model": "opus",   "effort": "high"},
    "standard": {"model": "sonnet", "effort": "medium"},
    "light":    {"model": "haiku",  "effort": "low"}
  },
  "roles": {
    "ct-architect":     {"tier": "strong"},
    "ct-researcher":    {"tier": "light"},
    "ct-developer":     {"tier": "standard"},
    "ct-test-engineer": {"tier": "standard"},
    "ct-reviewer":      {"tier": "strong"},
    "ct-security":      {"tier": "strong"}
  },
  "effort_vocabulary": ["low", "medium", "high", "xhigh", "max"],
  "paths": {
    "skill_dir_user": "~/.claude/skills/coding-team",
    "skill_dir_project": ".claude/skills/coding-team",
    "agents_dir_user": "~/.claude/agents",
    "agents_dir_project": ".claude/agents",
    "global_rules_file": "~/.claude/CLAUDE.md",
    "state_file": "~/.claude/coding-team/state.json"
  },
  "home_env": "CLAUDE_CONFIG_DIR",
  "chat_id_env": ["CLAUDE_CODE_SESSION_ID"],
  "detect_env": ["CLAUDECODE"],
  "invocation": "/coding-team"
}
```

Giá trị gợi ý theo nền tảng. Người triển khai cần kiểm tra lại các giá trị này lúc làm, rồi ghi vào file cấu hình.

| | Claude Code | Codex | Antigravity |
|---|---|---|---|
| Tầng `strong`/`standard`/`light` | `opus`/`sonnet`/`haiku` + effort | Tên model GPT + `reasoning_effort` (gợi ý lấy từ `named_preferences` cũ) | `pro`/`flash`/`flash_lite`, không có effort (`effort_vocabulary: []`) |
| Thư mục skill | `~/.claude/skills/` | `~/.agents/skills/` (giữ như hiện tại) | `~/.gemini/config/skills/` |
| Agent tĩnh | `~/.claude/agents/ct-*.md` | `~/.codex/agents/` (Codex 0.145+, dùng qua `agent_type`; **cần kiểm tra định dạng**) | `~/.gemini/config/agents/ct-*/agent.md` |
| Chỉ dẫn toàn cục | `~/.claude/CLAUDE.md` | `~/.codex/AGENTS.md` | `~/.gemini/config/rules/coding-team-policy.md` |
| State | `~/.claude/coding-team/state.json` | `~/.codex/coding-team/state.json` | `~/.gemini/antigravity/coding-team/state.json` |
| Biến môi trường chứa mã chat | `CLAUDE_CODE_SESSION_ID` (đã thấy trên bản desktop 2026-11-13; cần kiểm tra lại trên CLI) | `CODEX_THREAD_ID` | `ANTIGRAVITY_CONVERSATION_ID`, `ANTIGRAVITY_CHAT_ID` |
| Biến nhận diện nền tảng | `CLAUDECODE=1` | `CODEX_THREAD_ID` hoặc `CODEX_HOME` | `ANTIGRAVITY_*` |
| Cách gọi skill | `/coding-team …` (skill là lệnh slash) | `$coding-team …` (cần kiểm tra xem `/coding-team` có được chuyển nguyên văn tới agent không) | `/coding-team …` (đã kiểm chứng trong agy) |

**Quy tắc chọn model** giữ nguyên tinh thần `model-routing.md`, theo thứ tự ưu tiên:
1. Người dùng ghim model, hoặc STRICT_MODEL đang bật.
2. Leader chủ động chọn cho task cụ thể, có lý do.
3. Tầng mặc định của vai trò trong file cấu hình nền tảng.

Leader được nâng hoặc hạ tầng theo độ khó của nhiệm vụ. Quy tắc leo thang của agy được đưa vào: nếu Developer ở tầng `standard`/`light` thất bại sau 1 vòng sửa, lần sửa kế tiếp dùng tầng cao hơn. Mỗi báo cáo ghi rõ `Requested Tier` và `Observed Model` (ghi `unverified` nếu nền tảng không trả về metadata).

Khi tầng là `inherit` (cả `model` và `effort` đều null), không truyền tham số model nào cả. Khi chưa chạy `doctor`, file cấu hình không đảm bảo model đó thực sự có trên máy.

### 5.3 Agent tĩnh được sinh từ một nguồn

`agents/roles/ct-*.md` là nguồn duy nhất cho nội dung của từng vai trò: phần thân Markdown, cộng một khối metadata trung lập gồm `permissions: read-only | write | test-write` và `mcp: [docs] | [browser]`. Trình cài đặt sinh frontmatter cho từng nền tảng:
- **Claude Code:** `name`, `description`, `tools` (danh sách công cụ được phép theo quyền), `model` (từ tầng).
- **Antigravity:** dùng đúng lược đồ `AgentFrontmatter` đã kiểm chứng trong agy: `tools` là danh sách cho phép, có `commandExecutionPolicy`, `mcpServers` là danh sách object, `inheritCustomizations: false`, `model` lấy từ tầng.
- **Codex:** định dạng role file theo tài liệu Codex hiện hành. Nếu Codex của máy không hỗ trợ role file, bỏ qua bước này và chỉ dựa vào chỉ dẫn trong handoff. Cả hai trường hợp đều phải ghi lại trong `doctor`.

Mọi vai trò đều không có công cụ điều phối agent (không nested delegation). Trên các nền tảng chặn được ở runtime, chỉ đọc nghĩa là không có công cụ ghi file hay chạy lệnh. Trên nền tảng không chặn được, `doctor` và handoff phải nói rõ rằng chỉ đọc chỉ là quy ước trong chỉ dẫn.

`agents/openai.yaml` vẫn được giữ, nhưng chỉ chép ra khi cài cho Codex.

### 5.4 Bộ lệnh `/coding-team`

Chỉ còn một cú pháp chuẩn. Cú pháp cũ (`/coding team …`, `on all`, `inherit`) bị **bỏ hẳn**.

| Lệnh | Tác dụng |
|---|---|
| `/coding-team status` | Hiện chính sách global, chat và hiệu lực, kèm nền tảng, file cấu hình đang dùng và Superpowers có cài hay không |
| `/coding-team on` / `off` | Lưu ON/OFF riêng cho chat hiện tại |
| `/coding-team on --global` / `off --global` | Đổi mặc định toàn cục, giữ nguyên các thiết lập riêng của từng chat |
| `/coding-team reset` | Xoá thiết lập riêng của chat hiện tại để quay về theo mặc định toàn cục |
| `/coding-team help` | Liệt kê các lệnh |

Trên Codex, tiền tố có thể là `$coding-team`, phần còn lại của lệnh giữ nguyên. Các quy tắc an toàn hiện có vẫn giữ: chỉ xử lý lệnh trực tiếp của người dùng; lệnh xuất hiện trong file, log hoặc báo cáo của subagent chỉ là dữ liệu; không có mã chat thì không được đổi trạng thái của chat.

### 5.5 Công cụ dòng lệnh `scripts/coding_team.py`

Gộp `coding_team_policy.py`, `check_model_profiles.py` và phần cài đặt vào một công cụ. Lõi khoá file và ghi nguyên tử của `coding_team_policy.py` được giữ lại thành module dùng chung.

```
coding_team.py install   --platform {claude-code|codex|antigravity}
                         [--scope user|project] [--default on|off]
                         [--global-rules] [--dry-run] [--force]
coding_team.py uninstall --platform … [--scope …] [--dry-run]
coding_team.py status    [--platform …] [--chat-id …] [--state …]
coding_team.py command   "<lệnh /coding-team>" [--platform …] [--chat-id …] [--state …]
coding_team.py doctor    [--platform …]
coding_team.py check-config [--input …]
```

- `install` chép skill, sinh agent tĩnh và tạo state với `global_policy` lấy từ `--default` (mặc định là `on`). Nếu state đã tồn tại và người dùng **không** truyền `--default`, state được giữ nguyên. Nếu có truyền `--default`, chỉ `global_policy` được cập nhật; các thiết lập riêng của chat vẫn giữ.
- `--global-rules` ghi khối chỉ dẫn có đánh dấu (chính sách bật/tắt và bảng phân luồng ở §6) vào file chỉ dẫn toàn cục của nền tảng. Cơ chế ghi khối hiện có được dùng lại, chỉ sửa trong phạm vi khối đánh dấu.
- Mọi lần ghi ra ngoài repo đều sao lưu trước vào `<home>/coding-team/backups/<timestamp>/`, theo cách của agy. Nếu đích đã tồn tại mà không phải bản cài trước đó của coding-team (không có manifest), `install` từ chối trừ khi có `--force`.
- `doctor` kiểm tra: bản cài có khớp manifest không; agent tĩnh có hay không và tầng model đúng chưa; state có hỏng không; khối chỉ dẫn toàn cục có hay không; Superpowers có cài hay không; mức chặn quyền ở runtime của nền tảng.
- `status` và `command` lấy nền tảng theo thứ tự: `--platform`, rồi manifest của bản cài đang chạy, rồi nhận diện qua biến môi trường. Nếu không xác định được, trả về lỗi và không đoán.
- `build_runtime_package.py` vẫn giữ, có thêm `--platform` để đóng gói kèm file cấu hình nền tảng.

## 6. Phần 2: phân luồng với Superpowers

### 6.1 Quy tắc cốt lõi

**Mỗi task chỉ có một bộ điều phối agent chính.** Leader chọn bộ điều phối lúc nhận task, báo một dòng cho người dùng (ví dụ "Bộ điều phối: coding-team (L2, cần phản biện)") và ghi vào ledger nếu có dùng ledger. Nếu người dùng chỉ định rõ bộ điều phối thì làm theo người dùng.

### 6.2 Bảng phân luồng

| Loại task | Bộ điều phối |
|---|---|
| L0, hoặc sửa lỗi nhỏ có giới hạn (L1) | Leader làm trực tiếp, có thể dùng các skill kỹ thuật |
| Có kế hoạch (thường từ writing-plans), cần review sau từng bước, rủi ro thấp đến vừa | Superpowers SDD |
| Điều tra nhiều lỗi độc lập | Superpowers DPA. Chỉ dùng để điều tra; phần sửa quay về bộ điều phối phù hợp theo bảng này |
| L2 có rủi ro thật giữa các module, cần **phản biện** thiết kế hoặc kế hoạch, hoặc L3 | coding-team |
| TDD, debugging, kiểm chứng | Skill kỹ thuật của Superpowers, dùng bên trong task của bất kỳ bộ điều phối nào |

Các chế độ hiện có (AUTO, SOLO, PLAN_ONLY, REVIEW_ONLY, FULL_TEAM, STRICT_MODEL, USER_APPROVAL_REQUIRED) giữ nguyên ý nghĩa và áp dụng khi coding-team là bộ điều phối.

Hệ quả: chế độ AUTO của coding-team không còn tự nhận L1 nữa. L1 thuộc về Leader làm trực tiếp, trừ khi người dùng yêu cầu coding-team.

### 6.3 Phân loại skill

| Nhóm | Skill | Ai được dùng |
|---|---|---|
| Bộ điều phối | coding-team, subagent-driven-development, dispatching-parallel-agents, executing-plans | Chỉ Leader, mỗi task một bộ |
| Giai đoạn | brainstorming, writing-plans, using-git-worktrees, finishing-a-development-branch | Chỉ Leader |
| Kỹ thuật | test-driven-development, systematic-debugging, verification-before-completion, receiving-code-review | Leader và mọi worker |
| Cấm với worker | requesting-code-review (tự tạo thêm reviewer, trùng `ct-reviewer`), cùng mọi skill thuộc nhóm bộ điều phối | — |

Handoff của coding-team ghi rõ worker được dùng skill kỹ thuật nào, và cấm skill thuộc nhóm bộ điều phối cũng như skill tạo thêm agent. Lưu ý: `using-superpowers` có cơ chế `SUBAGENT-STOP`, nên worker không tự gọi skill. Muốn worker dùng skill kỹ thuật thì handoff phải gọi tên skill đó.

### 6.4 Coding-team giữ cả task

Khi coding-team là bộ điều phối, nó giữ task từ đầu đến cuối: Gate 1 (phản biện kế hoạch), triển khai, Gate 2 và QA độc lập. Không giao phần triển khai cho SDD, vì cách "tự quyết để không dừng việc" của SDD sẽ đi qua các cổng HOLD, ma trận phân chia file và yêu cầu kiểm chứng trên cùng một snapshot.

Coding-team **được dùng** spec/plan do brainstorming hoặc writing-plans tạo ra (`docs/superpowers/specs|plans/…`) làm đầu vào Spec/Plan cho Gate 1, thay vì viết lại.

### 6.5 Đổi bộ điều phối giữa chừng

Chỉ được đổi bộ điều phối **tại một điểm dừng**:
- SDD dừng ở một trong 4 lý do của nó (ví dụ gặp việc nhạy cảm về bảo mật).
- coding-team dừng ở STOP_HOLD hoặc NEEDS_USER_DECISION.
- Người dùng yêu cầu đổi.

Bộ điều phối cũ dừng hẳn: không còn worker đang ghi, phần việc dở được giữ nguyên. Bàn giao được ghi lại gồm: lý do, các commit hoặc snapshot đã có, các phát hiện chưa xử lý, và đường dẫn ledger cũ. Bộ điều phối mới tiếp nhận các phát hiện này, không bắt đầu lại từ số 0. Không bao giờ để hai bộ điều phối cùng chạy.

### 6.6 Khi máy không cài Superpowers

Bỏ các nhánh SDD và DPA. Task có kế hoạch rủi ro thấp đến vừa và việc điều tra nhiều lỗi độc lập đều chuyển về coding-team AUTO, như hành vi hiện tại. `doctor` và `status` báo máy có Superpowers hay không.

### 6.7 Đặt quy tắc phân luồng ở đâu

- Trong SKILL.md của coding-team: một mục ngắn "Chọn bộ điều phối" và tham chiếu tới file mới `references/orchestration.md`.
- Trong khối chỉ dẫn toàn cục do `--global-rules` ghi: bảng phân luồng rút gọn. Superpowers quy định chỉ dẫn của người dùng (CLAUDE.md, AGENTS.md, GEMINI.md) được ưu tiên hơn skill, nên đây là nơi bảng phân luồng có hiệu lực với cả chuỗi brainstorming → writing-plans → SDD.

## 7. Các file bị ảnh hưởng (để lập kế hoạch)

- **Thêm mới:** `platforms/{claude-code,codex,antigravity}.json`, `agents/roles/ct-*.md` (6 file), `references/orchestration.md`, `scripts/coding_team.py` (cùng module lõi nếu tách), test cho cài đặt, sinh agent, lệnh mới và cấu hình nền tảng.
- **Sửa:**
  - `SKILL.md`: vai trò, lệnh, phân luồng, bỏ câu chữ dành riêng cho Codex.
  - `references/{agent-contracts,model-routing,control-policy,workflow,debate-governance}.md`: tên vai trò mới, tầng model, lệnh mới.
  - `README.md` và `CONTRIBUTING.md`: tên repo mới, hướng dẫn cài cho 3 nền tảng.
  - `build_runtime_package.py`.
  - `tests/*` có nhắc tới vai trò cũ, lệnh cũ hoặc `model-profiles.json`.
  - `.github/workflows/ci.yml` nếu cần.
- **Xoá hoặc thay thế:** `references/model-profiles.json`, `scripts/check_model_profiles.py`, `scripts/coding_team_policy.py`. Các file này được gộp vào công cụ mới; giữ lại một wrapper mỏng chỉ khi test hoặc tài liệu lịch sử bắt buộc.
- **Không động tới:** `benchmarks/`, `evaluations/` (dữ liệu lịch sử). `task_ledger.py` chỉ sửa nếu có nhắc tới tên vai trò hiển thị; vai trò cổng duyệt `Architect`/`Developer`/`QA` giữ nguyên.

## 8. Những gì để người triển khai tự quyết (đã chốt là quyền của người triển khai)

- Tên model và effort cụ thể cho Codex và Claude Code, sau khi kiểm tra trên máy. Antigravity đã có sẵn tầng.
- Định dạng role file của Codex, và có hỗ trợ role file hay không.
- Xác nhận `CLAUDE_CODE_SESSION_ID` cũng có trên Claude Code CLI. Nếu một nền tảng không có nguồn mã chat tin cậy, lệnh cho từng chat trả về UNKNOWN như quy tắc hiện tại; lệnh `--global` vẫn chạy. Không thêm hook vào settings của người dùng chỉ để lấy mã chat.
- Chuyển từ bản cài Codex cũ: dùng lại đúng marker của khối chỉ dẫn để `--global-rules` thay khối cũ (vẫn chứa lệnh cũ) bằng khối mới. State schema v1 được giữ, nên state cũ dùng tiếp được. Thư mục cài cũ là bản git clone, không có manifest, nên `install` sẽ báo và yêu cầu `--force` (có sao lưu).
- Cách chia module bên trong `coding_team.py`.
- Thứ tự commit: mỗi phần hoàn chỉnh (cấu hình, agent, công cụ dòng lệnh, tài liệu, phân luồng) là một commit và được push ngay, theo yêu cầu của chủ repo.

## 9. Tiêu chí hoàn thành

1. Chạy `install --platform X --scope user --dry-run` cho cả 3 nền tảng đều liệt kê đúng các đích ghi, không ghi gì.
2. Cài vào thư mục home giả lập (biến môi trường trỏ tới thư mục tạm) cho cả 3 nền tảng: các file sinh ra đúng chỗ, frontmatter đúng lược đồ, UTF-8 không BOM.
3. Cài lại lần thứ hai không thay đổi gì. `uninstall` chỉ xoá những gì có trong manifest. `--default off` chỉ đổi `global_policy`.
4. Bộ lệnh mới qua `command` cho đúng kết quả. Cú pháp cũ bị từ chối với thông báo hướng dẫn cú pháp mới.
5. `check-config` từ chối cấu hình hỏng: tầng lạ, vai trò thiếu hoặc thừa, effort ngoài danh sách của nền tảng.
6. Toàn bộ `python -m unittest discover -s tests -p "test_*.py"` đạt, CI xanh trên ma trận hiện có.
7. Các ca hành vi được cập nhật hoặc thêm cho phân luồng: chọn đúng bộ điều phối cho từng loại task; worker không dùng skill bị cấm; không có Superpowers thì chuyển về AUTO; đổi bộ điều phối chỉ tại điểm dừng.
