"""
measure_latency.py - end-to-end response latency through the Rasa REST channel.
Requires rasa (5005) and action server (5055) running.  Run: python scripts/measure_latency.py
"""
import json, statistics, time, uuid, urllib.request

URL = "http://localhost:5005/webhooks/rest/webhook"
SCRIPT = ["hi", "I want to plan a trip", "from London", '/inform{"destination":"Paris"}', "next Friday",
          '/inform{"budget":"400"}', '/inform{"sustainability_level":"high"}', "show me eco friendly hotels",
          "how can I offset my emissions", "what does the colour mean"]


def post(sender, msg):
    req = urllib.request.Request(URL, data=json.dumps({"sender": sender, "message": msg}).encode(),
                                 headers={"Content-Type": "application/json"})
    t = time.perf_counter(); urllib.request.urlopen(req, timeout=10).read()
    return time.perf_counter() - t


if __name__ == "__main__":
    rows = {m: [] for m in SCRIPT}
    for run in range(5):
        sender = f"lat-{uuid.uuid4().hex[:6]}"
        for m in SCRIPT:
            rows[m].append(post(sender, m))
    out = {m: {"median_s": round(statistics.median(v), 3), "max_s": round(max(v), 3)} for m, v in rows.items()}
    json.dump(out, open("results/latency.json", "w"), indent=2)
    for m, v in out.items():
        print(f"{v['median_s']:6.3f}s  max {v['max_s']:6.3f}s  {m}")
