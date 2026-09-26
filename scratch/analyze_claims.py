import json, glob
from collections import Counter

topics = Counter()
claims_count = Counter()
for path in glob.glob("inputs/*.json"):
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    cls = d.get("customer_request", {}).get("claims", [])
    claims_count[len(cls)] += 1
    for c in cls:
        topics[c.get("topic")] += 1

print("Claims count distribution:", claims_count)
print("Topics distribution:", topics.most_common(20))
