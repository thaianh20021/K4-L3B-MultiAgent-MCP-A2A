# Kết quả thực hiện README mục 4, 5, 6

Ngày kiểm tra: 25/09/2026. Case set: `l3b-competition-v1`.

## Kết quả kỹ thuật

| Kiểm tra                 | Kết quả                             |
| ------------------------ | ----------------------------------- |
| `day09 validate-inputs`  | PASS, 100 case                      |
| `day09 mcp-tools`        | Discovery thành công, 10 tool       |
| `python -m ruff check .` | PASS                                |
| `python -m pytest -q`    | 31 passed                           |
| `day09 run`              | Hoàn tất 100/100 case, exit code 0  |
| `day09 validate`         | OK: 100 outputs / 2600 trace events |

Lượt chạy chính từ 17:18:06 đến 17:30:09, giờ Việt Nam. Artifacts nằm ở
`outputs/<case_id>.json` và `traces/trace.jsonl` (778.458 bytes).

Trace chính ghi 8 MCP attempts/case, tổng 800, dưới budget 12 attempts/case.
Trong đó có 60 tool errors của `get_refund_timeline`; các lỗi này không bị retry.
Các lời gọi khảo sát khi triển khai không nằm trong 800 attempts của lượt chạy
chính, nhưng vẫn có thể được server tính trong audit. Trace khảo sát riêng ở
`traces/discovery.jsonl` và không được đưa vào output hoặc trace chính.

## Kết quả nghiệp vụ và giới hạn

- Entity resolution: 100 `resolved`, candidate còn lại được loại qua customer history.
- Case status: 100 `needs_investigation`.
- Primary issue: 60 `insufficient_evidence`, 20 `refund_pending`, 20 `refund_failed`.
- Không khuyến nghị chi tiền khi nguồn evidence còn mâu thuẫn hoặc số dư chưa xác minh.

Đây là kết quả kiểm tra schema, trace và consistency local; chưa có điểm từ scorer
của Competition. Các case cần điều tra tiếp không được coi là đã giải quyết xong
khiếu nại nghiệp vụ.

Ví dụ quan sát được qua MCP ở `L3B_CASE_001`:

- `get_order` trả ngày mua 11/05/2018, trong khi history có thêm một phiên bản
  cùng order ID với ngày mua 20/12/2017.
- `get_order_items` có hai phiên bản cùng item ID, freight lần lượt 10 và 18 BRL.
- `get_payment_timeline` có hai dòng cùng payment sequence 1 nhưng giá trị
  lần lượt 89 và 16 BRL.
- Shipment summary có dates giao hàng tháng 05/2018 nhưng event `delivered_late`
  ngày 04/01/2018.
- `get_refund_timeline` trả tool error, không phải evidence xác nhận ledger rỗng.

Workflow chỉ chọn authoritative order row theo mô tả tool; không tự chọn một
phiên bản item/payment, giả định lỗi refund là 0, hoặc lấy claim trong input làm
kết luận. Các xung đột chưa có precedence được giữ `selected_source: null`.

Để kết luận chắc chắn hơn, cần nguồn MCP nhất quán hoặc policy nêu rõ cách chọn
giữa các phiên bản cùng ID, đồng thời refund tool cần trả evidence hợp lệ. Sau
khi nguồn được sửa, chạy lại `day09 run` và `day09 validate` để có evidence mới.

## Các thay đổi chính

- Gateway: discovery có schema/pagination/cache, tương thích MCP SDK 2,
  validate arguments/envelope và chặn evidence_ref chéo case.
- Workflow: entity/customer, order/product, shipment, payment/refund, policy,
  conflict resolver, verifier; A2A messages có case correlation.
- Efficiency: case-local cache có khóa cho call trùng, tối đa 3 requests đồng
  thời, timeout 20 giây/attempt, tối đa 1 retry cho lỗi timeout/transport.
- Verifier và artifact validator: kiểm tra scope, claim linkage, lifecycle,
  financial totals, source conflict và responsibility.
- `ARCHITECTURE.md` mô tả thiết kế; `requirements-lock.txt` ghi phiên bản thư viện.
- Test release safety kiểm tra payload/secrets trong Git inventory để cho phép
  input và output runtime được giữ local theo README.
