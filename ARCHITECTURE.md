# L3B Architecture Record

Workflow được triển khai tại `src/student_agent/workflow.py`; luật phân tích thuần ở
`analysis.py`, kiểm tra độc lập ở `verification.py`. Đây là các agent chuyên trách
chạy trong cùng process Python, trao đổi message có cấu trúc; không cần dịch vụ LLM
hoặc framework A2A bên ngoài. Trace chỉ chứa sự kiện quan sát được.

## 1. System overview

Coordinator resolve entity trước, tải policy theo phiên bản của case, sau đó giao
order/product, shipment và payment/refund cho ba coroutine độc lập. Kết quả được
reconcile và kiểm tra schema cùng các bất biến trước khi ghi file JSON nguyên tử.

```text
Input → Entity Resolver → Coordinator → Specialists → Conflict Resolver → Verifier → Output
            │                              │                  │             │
            └──────────────────────────── MCP ────────────────┴──────────── Trace
```

## 2. Agent ownership

| Actor | Input | Trách nhiệm | Tool permission | Output/handoff |
| --- | --- | --- | --- | --- |
| entity-agent | Candidate, claimed ID, customer hint | Đối chiếu history và order; loại candidate không thuộc history | get_customer_history, get_order | Resolution, customer context, order và conflicts |
| coordinator | Case và các AgentMessage | Điều phối theo DAG; quản lý scope/budget; tổng hợp output | Discovery | Task assignment và output cho verifier |
| order-agent | Order đã resolve | Kiểm tra item, tổng tiền, freight, product context và dòng trùng | get_order_items, get_product_context | Items, totals, conflicts |
| shipment-agent | Order đã resolve | Đối chiếu dates/events, shipping limit và seller trễ | get_shipment_summary | Shipment analysis, conflicts |
| payment-agent | Order đã resolve | Thu thập capture/refund lifecycle; đối chiếu với item totals | get_payment_timeline, get_refund_timeline | Ledger cho bước reconciliation |
| policy-agent | Policy version và kết quả chuyên trách | Áp dụng policy lên issue đã chứng minh, giới hạn refund | get_policy | Status, action, refund, responsibility |
| conflict-agent | Kết quả chuyên trách | Gom xung đột, phân biệt đã chọn source với chưa thể giải quyết | Không gọi MCP | Conflict list, trạng thái unresolved |
| verifier | Output, case, refs đã tiêu thụ | Schema, scope, linkage, totals và consistency | Không gọi MCP | verification_completed hoặc lỗi chặn finalize |

Áp dụng least privilege; tool discovery không đồng nghĩa mọi actor đều được gọi mọi tool.

## 3. Entity resolution và A2A protocol

Customer history là nguồn xác minh membership. Claimed order chỉ được chấp nhận
khi có trong history; nếu không có claimed ID, giao của candidate và history phải
chỉ có một phần tử. Candidate không nằm trong history được reject. `get_order`
phải xác nhận đúng order và customer row. Nếu không có customer hint, chỉ thử
candidate duy nhất và dùng customer_unique_id do order trả về để xác minh tiếp.
Không quét rộng khi thiếu định danh. Hai candidate phù hợp mà chưa có định danh
chính xác dẫn đến `ambiguous`; không tìm thấy trong history dẫn đến `not_found`.
Nếu input không có candidate IDs nhưng history chỉ có một order, order đó được
xác minh tiếp bằng `get_order` trước khi resolve.

`AgentMessage` có `case_id`, `sender`, `recipient`, `payload`, `evidence_refs`.
Coordinator kiểm tra correlation, người nhận và refs khi nhận message; payload
được copy để tránh agent khác sửa kết quả đã bàn giao. DAG chỉ chạy một lần,
không có vòng lặp agent. Entity resolved dùng confidence 0.97; chưa resolve 0.30.
Đây là heuristic có công bố, chưa được hiệu chỉnh bằng nhãn đánh giá riêng.

## 4. Evidence và conflict lifecycle

Gateway discovery toàn bộ trang tool một lần mỗi connection, lưu input schema và
validate arguments trước call. Adapter hỗ trợ response SDK với snake_case và
camelCase; MCP envelope phải qua public JSON Schema. CaseContext còn kiểm tra
domain và entity/case/policy identifiers xuất hiện trong payload. Ref được giữ
nguyên; gateway chặn ref lặp lại ở case khác trong connection. Quyền sở hữu team
và run cuối cùng do audit server xác minh vì public envelope không chứa hai field này.

Mỗi lần agent dùng evidence đều emit `tool_result_consumed`. Output và từng claim
liên kết refs từ ledger của chính case. Không lưu cache evidence ra đĩa để tái sử
dụng cho lượt chạy sau. `result_hash` được giữ nguyên và kiểm tra định dạng theo
schema; không tự đặt thuật toán canonicalization chưa được server công bố.

Tool discovery mô tả `get_order` là authoritative order row, nên nó được chọn khi
history projection khác. Xung đột giữa các phiên bản item/payment hoặc giữa
shipment events và dates không có source precedence công bố thì giữ
`selected_source: null`, `UNRESOLVED_SOURCE_CONFLICT`. Các xung đột chặn chi tiền,
hạ confidence và yêu cầu manual review. Schema giới hạn 5 conflict descriptions;
trace vẫn ghi tổng số xung đột phát hiện được.

Dòng giống hệt được deduplicate. Hai phiên bản khác nhau cùng item ID/payment
sequence không được cộng gộp hoặc tự chọn để kết luận tiền. Số tiền dùng Decimal
hai chữ số thập phân, chỉ đổi sang JSON number ở biên output. Refund có reference
dùng sự kiện cuối cùng theo thời gian; missing refund evidence được giữ `null`,
không đổi thành số 0. Không lấy nhãn claim trong input làm primary issue.

Policy quyết định action/status và mức tiền cho phép sau khi xác lập issue từ
evidence. Refund không vượt entitlement, mức policy và captured trừ refunded đã
biết. Seller ID từ policy được ràng buộc lại với seller của order hiện tại.

## 5. Failure and efficiency policy

| Failure | Retry budget | Fallback | Trace event/code |
| --- | ---: | --- | --- |
| MCP timeout/transport | Tối đa 1 retry/tool | Missing evidence; needs_investigation | handoff / MCP_RETRY, MCP_TIMEOUT |
| MCP tool error, sai scope/schema/domain | 0 | Cache thất bại trong case; không dùng payload | handoff / MCP_INVALID_OR_UNAVAILABLE |
| Tool không được discovery | 0 | Không call; missing evidence | handoff / TOOL_UNAVAILABLE |
| Entity not found/ambiguous | 0 | Không chạy order specialists; không refund | handoff / RESULT_READY, policy_decided |
| Source conflict | 0 | Chọn source chỉ khi có căn cứ; còn lại manual review | handoff / UNRESOLVED_CONFLICT |
| Invalid specialist/output invariant | 0 | Chặn finalize; báo lỗi để sửa | Không ghi verification_completed thành công |
| Hết call budget | 0 | Không gọi tiếp; missing evidence | handoff / CALL_BUDGET_EXHAUSTED |

Budget cứng 12 attempts/case, tính cả retry và call lỗi. Đường đi bình thường là
8 calls: history, order, policy, items, product, shipment, payment timeline, refund
timeline. Không gọi thêm `get_order_payments` vì payment timeline đã có payments;
không gọi sellers khi items đã đủ seller IDs. Timeout mỗi attempt 20 giây; tối đa
3 MCP requests cùng lúc. Case chạy tuần tự để giữ output và trace dễ kiểm chứng.
Cache theo tool + arguments nằm trong CaseContext; context bị bỏ khi case kết thúc.
Khóa theo cache key gộp các yêu cầu đồng thời giống nhau thành một lời gọi MCP.
Tool discovery được chia sẻ trong connection, không chia sẻ evidence giữa case.

## 6. Verification invariants

- Public JSON Schema: enum, confidence bounds, unique IDs và giới hạn array.
- Case ID, resolved orders và affected orders trùng nhau; rejected không giao resolved.
- Evidence phải được tiêu thụ trong case hiện tại; tất cả claim IDs được đánh giá đúng một lần.
- Claim refs nằm trong output refs; claim khẳng định phải có evidence.
- Timeline được chuyên trách kiểm tra thứ tự purchase, handoff, delivered và events.
- Refund lines cộng đúng tổng; refunded không vượt captured; balance nhất quán.
- Refund dương cần action_required, entity resolved và đủ known refundable balance.
- Seller trễ/chịu trách nhiệm thuộc affected sellers; selected source có trong sources.
- Conflict chưa giải quyết và entity chưa resolve cần needs_investigation.
- `day09 validate` còn kiểm tra trace đủ lifecycle, receive đầu/finalize cuối,
  event IDs duy nhất, output-to-consumed-ref linkage và không dùng ref chéo case.

Các kiểm tra local không thay thế scoring và provenance audit của Competition.

## 7. Reproducibility

Không dùng model ngoài: quyết định deterministic trên evidence; không có random
seed cho nghiệp vụ. Event ID và timestamp trace được sinh mới ở mỗi lượt chạy.
Python >=3.11; môi trường đã kiểm thử tại máy này là Python 3.13.14. Các version
thư viện thực tế được ghi trong `requirements-lock.txt`; `pyproject.toml` giữ
version bounds của starter. Thiết lập từ `.env`, không ghi key vào trace hoặc tài liệu.

```powershell
.venv\Scripts\python.exe -m pip install -r requirements-lock.txt
.venv\Scripts\python.exe -m pip install -e ".[dev]"
.venv\Scripts\python.exe -m ruff check .
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\day09.exe validate-inputs
.venv\Scripts\day09.exe mcp-tools
.venv\Scripts\day09.exe run
.venv\Scripts\day09.exe validate
```

Trên Linux/macOS, activate `.venv` rồi dùng `python`/`day09` tương ứng. Output ở
`outputs/<case_id>.json`, trace chính ở `traces/trace.jsonl`. Inputs, outputs,
trace và `.env` được gitignore. Test release safety kiểm tra Git inventory để
không cản trở việc tải input/chạy local nhưng vẫn cấm commit payload và secrets.

Trong lượt kiểm tra MCP thực tế, đã quan sát nhiều phiên bản khác nhau cùng mã
đơn/dòng thanh toán, events không khớp dates, và `get_refund_timeline` trả tool
error trên một số order. Những trường hợp này được báo là evidence gaps/conflicts;
pass schema không có nghĩa đã có kết luận nghiệp vụ chắc chắn hay điểm thi cao.
