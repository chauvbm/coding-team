# Tiến độ giai đoạn 1

Cập nhật ngày 2026-11-15. [SPEC.md](SPEC.md) ghi phạm vi và tiêu chí nghiệm thu. [PLAN.md](PLAN.md) ghi trạng thái kế hoạch.

## Mốc khôi phục

- Tag `pre-antigravity-cli-20261115` trỏ đến `7b1adf57bf832ea0773f2dad7ccae4f1876f6d78` trên `codex/review-driven-hardening`. GitHub đã nhận cả commit và tag. Commit giữ source tree của `c04c93d`. [Mốc trên GitHub](https://github.com/chauvbm/coding-team/tree/pre-antigravity-cli-20261115)
- Nhánh `codex/antigravity-phase-1` xuất phát từ nền đa platform `origin/main` tại `234131df6624fe45c5c1144e9596d8b0a88fbc53` trong worktree riêng. Quy trình duyệt Spec/Plan từ checkpoint cần được chuyển sang nền này.
- Bộ test ở nền `234131d` chạy 233 test, 229 đạt và 4 bỏ qua trên Windows. Graphify đã lập chỉ mục worktree này.

## Quyết định và review

- Bản 1 dùng Antigravity CLI qua tài khoản Antigravity. Architect và Security review riêng trước khi Leader trình người dùng. Cả hai yêu cầu sửa. Các nhóm phát hiện gồm bàn giao async, quyền và hook hiệu lực trước khi chạy, phạm vi snapshot, xử lý tiến trình và lần chạy, cùng hành vi duyệt Spec/Plan cần giữ. Phát hiện quan trọng là Google không hỗ trợ bọc `agy` trong ứng dụng bên thứ ba theo gói người dùng cá nhân. [FAQ](https://antigravity.google/docs/faq/#can-i-use-antigravity-cli-headless-mode-in-third-party-coding-agents-or-automate-account-switching)
- Bản 2 khảo sát Antigravity CLI chạy Gemini API trực tiếp. Người dùng chọn chỉ dùng Antigravity. Bản 2 được thay thế trước khi trình duyệt.
- Bản 3 đề xuất Codex Desktop làm Leader và Antigravity Desktop nhận việc trong phiên đã đăng ký. Architect và Security yêu cầu thêm bài thử hai việc liên tiếp, quyền của helper và kiểm chính sách ngay trước khi worker nhận việc hoặc nhận ngữ cảnh.

- Bản 4 được Architect và Security chấp nhận trong phạm vi review. Các phát hiện MAJOR về hai việc liên tiếp, định danh, quyền helper và kiểm policy khi nhận việc đã được kiểm lại. Leader chỉnh đúng một dẫn chứng từ tham chiếu commit sang file đã đối chiếu SHA-256. So sánh xác nhận phần hành vi và nghiệm thu của bản được review giữ nguyên.
- Phiên bản trao cho người dùng duyệt có Git blob `f77f131f2af597d7d115ad14bc26217409c374ed` và SHA-256 `B5F58C0C145EDB724D2B4DFDABB71D42DC1C7FFB751D4CCB6C2679D6B890CC8F`.

## Trạng thái

| Chặng | Trạng thái |
| --- | --- |
| Mốc trên GitHub | Đã xác minh |
| Leader viết Spec | Bản 4 đã cập nhật |
| Review Spec | Architect và Security chấp nhận bản 4 |
| Người dùng duyệt Spec | Chờ duyệt bản 4 và phương án Antigravity Desktop |
| Leader viết và trình Plan | Chờ duyệt Spec |
| Triển khai và thử runtime | Chờ duyệt Plan |

## Bằng chứng và tiếp tục

- Máy khảo sát chạy Windows 10 Pro build 19045 và có Antigravity Desktop 2.32.1.0. Team Bridge đã thử luồng Desktop đánh thức đúng phiên đã đăng ký. Mốc đó là bằng chứng về cơ chế nhận tín hiệu. Luồng này trong coding-team cần thử riêng. Bằng chứng cục bộ ở `D:/projects/team-bridge/docs/gate0/RESULT.md` với SHA-256 `551C4D51B379D46F470274DC8A3D4F6C50F169F8DE5A287B600D55477E66BCFA`.
- Bước tiếp theo là người dùng duyệt Spec và phương án dùng Antigravity Desktop. Sau đó Leader ghi mốc duyệt và viết [PLAN.md](PLAN.md) theo đúng phiên bản Spec. Test runtime và quy trình triển khai chờ duyệt Plan.
