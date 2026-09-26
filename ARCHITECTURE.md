# L3B Architecture Record — Multi-Agent MCP + A2A System

Tài liệu thiết kế kiến trúc hệ thống Multi-Agent điều tra khiếu nại thương mại điện tử (Day09 L3B) sử dụng mô hình **Llama 3.1 (8B-Instruct)**.

## 1. System overview

Luồng xử lý từ input case, phân giải thực thể (Entity Resolution), phân công nhiệm vụ (A2A Coordinator), gọi MCP Tools thu thập bằng chứng, các Specialist Agents chuyên biệt, Conflict Resolver, Verifier và tạo Output + Trace:

```text
Input → Entity Resolver → Coordinator → Specialists (Order/Shipment/Payment/Policy) → Conflict Resolver → Verifier → Output
            │                              │                                             │             │
            └──────────────────────────── MCP Gateway (Evidence Collection) ─────────────┴──────────── Trace (trace.jsonl)
```

- **Input Case**: Chứa thông tin khiếu nại (`complaint`), các mốc thời gian (`claim_date`), danh sách `candidate_order_ids` (nếu có).
- **Entity Resolver**: Xác minh `customer_unique_id`, khớp danh sách candidate order IDs thành `resolved_order_ids` hoặc `rejected_candidates`.
- **Coordinator**: Điều phối quy trình A2A, khởi tạo task, quản lý luồng dữ liệu giữa các Agent.
- **Specialists**:
  - **Order/Product Agent**: Phân tích danh mục sản phẩm, người bán (`seller_ids`), giá trị sản phẩm.
  - **Shipment Agent**: Truy vấn lịch sử vận chuyển, đánh giá tiến độ giao hàng (`on_time`, `seller_delay`, `logistics_delay`, `lost`, `returned`).
  - **Payment/Refund Agent**: Phân tích dòng tiền (`captured_total_brl`, `refunded_total_brl`, `refundable_total_brl`), đối soát thanh toán.
  - **Policy Agent**: Đối soát quy định hoàn tiền và chính sách bảo vệ người mua.
- **Conflict Resolver**: Phát hiện và giải quyết mâu thuẫn dữ liệu giữa các nguồn (ví dụ: system log vs merchant claim).
- **Verifier**: Kiểm tra tính nhất quán, tuân thủ JSON Schema (`day09-l3b-output-v2`), cân chỉnh `confidence` và xác nhận tất cả `evidence_ref` hợp lệ trước khi `case_finalized`.

## 2. Agent ownership

| Actor | Input | Trách nhiệm | Tool permission | Output/handoff |
| --- | --- | --- | --- | --- |
| `entity-agent` | `case` raw, candidates | Phân giải Order ID & Customer context | `get_customer_history`, `lookup_order` | `entity_resolution`, `customer_context` |
| `coordinator` | `case` | Điều phối luồng làm việc A2A & emit trace | discovery (`list_tools`) | Handoff cho Specialists |
| `order-agent` | `resolved_order_ids` | Phân tích chi tiết đơn hàng & item | `get_order_details`, `get_product_info` | `affected_entities` (item/seller) |
| `shipment-agent` | `resolved_order_ids` | Đánh giá tiến độ & bên chịu trách nhiệm trễ | `get_shipment_tracking`, `get_carrier_status` | `shipment_analysis` |
| `payment-agent` | `resolved_order_ids` | Đối soát dòng tiền & hạn mức hoàn tiền | `get_payment_records`, `get_refund_history` | `payment_analysis`, `financial_resolution` |
| `policy-agent` | `primary_issue`, dispute context | Đánh giá chính sách & quy tắc bồi thường | `get_policy_rules` | `claim_assessments`, `resolution_actions` |
| `conflict-resolver` | Outputs từ Specialists | Phát hiện & giải quyết mâu thuẫn giữa các nguồn | Read-only state | `data_conflicts` |
| `verifier` | Full structured output | Validate schema, evidence refs & confidence | Final validation | Final Output JSON |

## 3. Entity resolution và A2A protocol

- **Candidate Ranking**: Đánh giá ứng viên dựa trên khớp `customer_unique_id`, mốc thời gian mua hàng (`purchase_timestamp`), danh mục mặt hàng khiếu nại.
- **Confidence Threshold**:
  - `confidence >= 0.85`: Ghi nhận vào `resolved_order_ids`, chuyển status = `"resolved"`.
  - `0.40 <= confidence < 0.85`: Trạng thái `"ambiguous"`, lưu các candidate kém phù hợp vào `rejected_candidates`.
  - `confidence < 0.40`: Trạng thái `"not_found"`.
- **A2A Protocol & Correlation**:
  - Mọi sự kiện giao tiếp và handoff giữa các Agent đều gắn với `case_id`.
  - Quản lý trạng thái bằng Directed Acyclic Graph (DAG) cố định: `Coordinator -> Entity Resolver -> Specialists -> Conflict Resolver -> Verifier`. Không cho phép gọi quay vòng (no circular loops).
  - Timeout tối đa cho mỗi case: 30 giây.

## 4. Evidence và conflict lifecycle

- **Validation & Provenance**: Mọi phản hồi từ MCP Gateway được kiểm tra qua schema `mcp-evidence-response-v1`. Mỗi `evidence_ref` (dạng `ev_...`) được giữ nguyên gốc, không sửa đổi hay tự tạo.
- **Trace Emission**: Ngay khi tiêu thụ dữ liệu từ tool, emit sự kiện `tool_result_consumed` đính kèm `evidence_refs` tương ứng.
- **Source Precedence & Conflict Resolution**:
  - Ưu tiên nguồn: `mcp_carrier_log` > `mcp_payment_gateway` > `order_system` > `user_statement`.
  - Khi có mâu thuẫn giữa 2 nguồn, ghi nhận vào `data_conflicts` với `resolution_code` phù hợp (ví dụ: `CARRIER_LOG_OVER_STATEMENT`).
- **Isolation**: Caching evidence theo từng `case_id`. Không tái sử dụng evidence giữa các case khác nhau.

## 5. Failure and efficiency policy

| Failure | Retry budget | Fallback | Trace event/code |
| --- | ---: | --- | --- |
| MCP timeout / HTTP Error | 2 retries (exponential backoff) | Đánh dấu `insufficient_evidence` | `policy_decided` / `MCP_TIMEOUT_FALLBACK` |
| Entity not found/ambiguous | 1 retry với fuzzy query | Trả về `status: "ambiguous"` hoặc `"not_found"` | `policy_decided` / `ENTITY_AMBIGUOUS` |
| Source conflict | 0 retries (xử lý bằng rules) | Áp dụng rule ưu tiên source | `policy_decided` / `CONFLICT_RESOLVED` |
| Invalid specialist result | 1 retry với prompt định hướng | Dùng default structured fallback từ schema | `verification_completed` / `RETRY_SPECIALIST` |

- **Efficiency Strategy**:
  - Dùng **In-Memory Cache** theo `(case_id, tool_name, kwargs)` để đảm bảo 1 query không bao giờ bị gọi lại 2 lần.
  - Hạn chế quét rộng (no broad scan): Chỉ truy vấn thông tin theo các `case_id` và `order_id` liên quan trực tiếp.

## 6. Verification invariants

Trước khi xuất file JSON đầu ra (`case_finalized`), hệ thống Verifier bắt buộc kiểm tra các điều kiện bất biến (invariants):
1. **Schema Compliance**: Đạt validate theo schema `day09-l3b-output-v2.schema.json`.
2. **Case ID Match**: `output["case_id"] == input["case_id"]`.
3. **Evidence Ownership**: Mọi `evidence_ref` trong output phải xuất hiện trong danh sách audit log đã trả về từ MCP Gateway của case đó.
4. **Entity Scope**: Tất cả Order IDs trong `affected_entities`, `customer_context` phải nằm trong tập `resolved_order_ids`.
5. **Financial Reconciliation**:
   - `recommended_refund_brl` <= `refundable_total_brl`.
   - Sum của `refund_lines` bằng `recommended_refund_brl`.
6. **Confidence Calibration**: `confidence` phải nằm trong dải `[0.0, 1.0]` và phản ánh mức độ đầy đủ của evidence thu thập được.

## 7. Reproducibility

- **Model**: `Meta-Llama-3.1-8B-Instruct` (Local via Ollama / vLLM / LiteLLM hoặc OpenAI-compatible API)
- **Parameters**: `temperature = 0.0`, `top_p = 0.9`, `max_tokens = 2048`
- **Dependencies**: `python >= 3.11`, `openai >= 1.0.0`, `mcp >= 2.0.0`, `jsonschema >= 4.25`
- **Concurrency**: `max_concurrent_cases = 1` (xử lý tuần tự từng case để đảm bảo tính ổn định và chính xác)
- **Execution Command**: `day09 run`

