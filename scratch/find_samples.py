import json, glob

by_topic = {}
for path in sorted(glob.glob("inputs/*.json")):
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    cls = d.get("customer_request", {}).get("claims", [])
    for c in cls:
        top = c.get("topic")
        if top != "requested_full_refund" and top not in by_topic:
            by_topic[top] = d["case_id"]

for top, cid in sorted(by_topic.items()):
    print(f"Topic: {top:<25} -> Case: {cid}")
