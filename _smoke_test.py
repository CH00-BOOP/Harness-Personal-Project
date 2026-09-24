"""冒烟测试:跑一遍演示用例,验证 Agent Loop + 自纠错闭环。"""
import json
import urllib.request

BASE = "http://127.0.0.1:8000"


def call(path, payload=None, method="POST"):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read().decode("utf-8"))


def chat(msg):
    r = call("/api/chat", {"message": msg, "session_id": "test"})
    tools = [s["tool"] for s in r["trace"] if s["type"] == "tool_call"]
    print(f"\nQ: {msg}")
    print(f"   工具链: {tools}")
    print(f"   handoff={r['handoff']} ticket={r.get('ticket_id')}")
    print(f"   A: {r['reply']}")
    return r


print("====== 第一幕:单工具 ======")
chat("颐养老人手机的电池能用多久?")
chat("我的订单 ORD10001 到哪了?")

print("\n====== 第二幕:多工具+推理 ======")
chat("这款手机适合送长辈吗?")

print("\n====== 第三幕:自纠错 - 触发转人工 ======")
r = chat("扫地机器人 X1 能翻越多高的门槛?")
tid = r["ticket_id"]
assert r["handoff"] and tid, "应触发转人工"

print("\n-- 人工补充答案 + 审核入库 --")
call(f"/api/tickets/{tid}/answer", {"answer": "扫地机器人 X1 可自动翻越最高约 2cm 的门槛。"})
audits = call("/api/audits?status=pending", method="GET")
aid = audits[0]["id"]
print(f"   待审核 #{aid}: {audits[0]['answer']}")
print("   审核结果:", call(f"/api/audits/{aid}/approve"))

print("\n====== 回放验证:同一问题这次应直接答对 ======")
r2 = chat("扫地机器人 X1 能翻越多高的门槛?")
assert not r2["handoff"], "补充后不应再转人工"
assert "2cm" in r2["reply"], "应答出 2cm"

print("\n====== 第四幕:统计看板 ======")
print("   metrics:", call("/api/metrics", method="GET"))

print("\nALL PASSED")
