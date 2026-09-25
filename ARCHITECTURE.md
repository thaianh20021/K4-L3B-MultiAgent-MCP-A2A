# L3B Architecture Record

Tài liệu mô tả các quyết định có thể kiểm chứng. Hệ thống không ghi prompt bí
mật hoặc chain-of-thought vào output hay trace.

## 1. System overview

Vẽ hoặc mô tả luồng từ input/candidate resolution đến MCP investigation, specialist agents, conflict resolver, verifier, output và trace.

```text
Input → Coordinator → MCP Specialists → Workers AI → Verifier → Output
            │                              │                  │             │
            └──────────────────────────── MCP ────────────────┴──────────── Trace
```

CLI đọc đúng 100 input đã cung cấp. Workflow không tạo hoặc sửa file trong
`inputs/`. Mỗi case có cache riêng, một request Workers AI và một output JSON.

## 2. Agent ownership

| Actor | Input | Trách nhiệm | Tool permission | Output/handoff |
| --- | --- | --- | --- | --- |
| Entity | candidates | Kiểm tra candidate và chọn order có evidence | `get_order` | order evidence → coordinator |
| Customer | customer hint | Kiểm tra customer history | `get_customer_history` | customer evidence → coordinator |
| Coordinator | case + specialist evidence | Phân công, cache, đóng gói model context | Không gọi domain tool trực tiếp | final draft → verifier |
| Order/product | resolved order | Xác định item, seller và product context | `get_order_items`, `get_product_context`, `get_sellers` | entity evidence → coordinator |
| Shipment | resolved order | Phân loại timeline và trách nhiệm giao hàng | `get_shipment_summary` | shipment evidence → coordinator |
| Payment/refund | resolved order | Đối soát capture, payment và refund | `get_order_payments`, `get_payment_timeline`, `get_refund_timeline` | financial evidence → coordinator |
| Policy/conflict | policy version + evidence | Áp dụng policy và biểu diễn conflict | `get_policy` | policy decision → coordinator |
| Verifier | model draft + allowed refs | Kiểm tra case, claim và provenance | Không gọi MCP | verified output → coordinator |

Tool discovery chỉ xác nhận contract có sẵn; mỗi actor chỉ tiêu thụ evidence từ
nhóm tool nêu trên.

## 3. Entity resolution và A2A protocol

Entity agent gọi `get_order` cho từng candidate duy nhất. Candidate có payload
not-found bị loại; candidate đầu tiên có dữ liệu hợp lệ được dùng cho downstream.
Model nhận cả candidate evidence để xác nhận `resolved_order_ids` và
`rejected_candidates`.

Mọi call và handoff tương quan bằng `case_id`. Workflow tuyến tính, không cho
agent tự gọi lại coordinator nên không có vòng lặp. Handoff chỉ chứa mã quyết
định và evidence refs, không chứa suy luận riêng.

## 4. Evidence và conflict lifecycle

Gateway validate mọi MCP envelope bằng public schema. Collector lưu nguyên
`evidence_ref`, domain, data và warnings; không sửa hoặc tự tạo reference. Mỗi
evidence được dùng sẽ emit `tool_result_consumed`.

Workers AI chỉ được phép chọn refs trong `allowed_evidence_refs`. Verifier từ
chối output có ref ngoài case hoặc claim ID không khớp input. Conflict được ghi
trong `data_conflicts` với ít nhất hai source, source được chọn hoặc `null`, và
resolution code. Cache bị hủy khi `solve_case()` kết thúc nên evidence không
được tái sử dụng chéo case.

## 5. Failure and efficiency policy

| Failure | Retry budget | Fallback | Trace event/code |
| --- | ---: | --- | --- |
| MCP runtime failure | 1 | Thất bại sau lần gọi thứ hai | Không consume evidence lỗi |
| Entity not found/ambiguous | 0 | Model trả `not_found` hoặc `ambiguous` | `handoff/EVIDENCE_READY` |
| Source conflict | 0 | Giữ conflict và giảm confidence | `policy_decided/POLICY_APPLIED` |
| Invalid model result | 1 repair | Thất bại rõ ràng sau repair | Không finalize output lỗi |

Cache key là `(tool_name, sorted arguments)` trong một case. Workflow chỉ gọi
hai candidate đã cung cấp và các tool theo investigation scope; không quét dữ
liệu rộng. Retry dùng cùng tham số, tối đa một lần, và không biến missing
evidence thành dữ liệu phỏng đoán.

## 6. Verification invariants

Trước finalize:

- `schema_version` và `case_id` phải đúng;
- mọi input claim xuất hiện đúng một lần;
- mọi output/claim evidence ref thuộc case hiện tại;
- entity resolution chỉ dùng supplied candidates;
- shipment verdict, responsible party và action phải nhất quán;
- captured/refunded/refundable và recommended refund dùng số BRL không âm;
- confidence nằm trong `[0, 1]`;
- CLI validate toàn bộ output bằng public JSON Schema.

## 7. Reproducibility

Model: `@cf/meta/llama-3.1-8b-instruct-fp8-fast`, temperature `0`, tối đa 4096
output tokens. Config lấy từ `CLOUDFLARE_ACCOUNT_ID`,
`CLOUDFLARE_API_TOKEN`, và `CLOUDFLARE_AI_MODEL`; không ghi API key vào source,
output hoặc trace.

CLI xử lý tuần tự để giữ audit và quota dễ kiểm soát. Không dùng random seed.
Dependencies được pin theo `pyproject.toml`. Lệnh chạy:

```bash
day09 run
day09 validate
```
