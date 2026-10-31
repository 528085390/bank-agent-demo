"""按前端的真实调用路径跑一遍订阅流程（HTTP 层，不打开浏览器）。

浏览器里点一下，前端发的是这些请求：

    POST /api/session                                       进入演示会话
    POST /api/conversations                                 新建对话
    POST /api/conversations/{id}/messages                   发一句话（SSE 流式回事件）
    POST /api/conversations/{id}/pending-actions/{id}/decide 点确认卡片上的按钮

这个脚本把同样的请求发一遍，因此它验证的是**前端真正会走的那条链路**：
真实主 Agent（Supervisor）→ 订阅子 Agent → 真实 bank_backend。

用法（三个服务都要先起来，见 docs/subscription-agent.md）：

    python scripts/ui_smoke.py
    python scripts/ui_smoke.py --bank-url http://127.0.0.1:8001/api
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path
from typing import Any

import httpx

REPO_ROOT = Path(__file__).resolve().parents[2]

AGENT_URL = "http://127.0.0.1:8000/api"
BANK_URL = "http://127.0.0.1:8001/api"
BANK_CUSTOMER = "u_wealth"  # backend 演示用户 user_0001 绑定的银行客户

# 沙箱把 http_proxy 指向了本机代理，本地请求也会被打过去，从而出现莫名的 404。
# 只信任本机地址，其余保持环境原样。
NO_PROXY = "127.0.0.1,localhost"


def rule(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def parse_sse(lines) -> list[tuple[str, dict[str, Any]]]:
    """把 SSE 文本解析成 (事件名, 载荷) 列表。

    事件名在 `event:` 行上，不在 JSON 里——所以必须成对读：先记住上一个 `event:`，
    再把它套到紧跟着的 `data:` 上。
    """

    events: list[tuple[str, dict[str, Any]]] = []
    name = ""
    for line in lines:
        if line.startswith("event:"):
            name = line[6:].strip()
        elif line.startswith("data:"):
            payload = line[5:].strip()
            if payload and payload != "[DONE]":
                # 只留业务载荷：外层还有 request_id / error 这些信封字段。
                envelope = json.loads(payload)
                events.append((name, envelope.get("data") or envelope))
    return events


def show(name: str, data: dict[str, Any]) -> None:
    if name == "subtask_end":
        print(
            f"  子 Agent[{data.get('agent_id')}] {data.get('turn_status')}: {data.get('summary')}"
        )
    elif name == "interrupt":
        card = data.get("confirmation") or {}
        print(f"  确认卡片：{data.get('display')}")
        print(f"     operation={card.get('operation')} pending_id={card.get('pending_id')}")
        print(
            f"     question_id={data.get('question_id')} 按钮={[f.get('options') for f in data.get('fields', [])]}"
        )
    elif name == "final":
        print(f"  助手：{str(data.get('text'))[:300]}")
        if data.get("business_ids"):
            print(f"     业务编号：{data.get('business_ids')}")
    elif name in ("tool_result", "receipt", "error", "agent_start"):
        print(f"  {name}: {str(data)[:200]}")


class Conversation:
    def __init__(self, agent: str) -> None:
        self.agent = agent.rstrip("/")
        self.client = httpx.Client(timeout=120, trust_env=False)

    def open(self) -> None:
        session = self.client.post(f"{self.agent}/session").json()["data"]
        print(f"会话：{session['login_name'] or session['user_id']}（{session['user_id']}）")
        created = self.client.post(
            f"{self.agent}/conversations", json={"title": "订阅联调"}
        ).json()["data"]
        self.conversation_id = created["conversation_id"]
        print(f"对话：{self.conversation_id}")

    def say(self, text: str) -> dict[str, Any]:
        """发一句话，返回这一轮的最后一个事件。"""

        print(f"\n你：{text}")
        # 幂等键走请求头，不进 body：body 里多一个字段会被判成参数错误（422）。
        headers = {"Idempotency-Key": str(uuid.uuid4())}
        with self.client.stream(
            "POST",
            f"{self.agent}/conversations/{self.conversation_id}/messages",
            json={"message": text, "client_context": {}},
            headers=headers,
        ) as response:
            response.raise_for_status()
            incoming = parse_sse(response.iter_lines())
        last: dict[str, Any] = {}
        for name, data in incoming:
            show(name, data)
            last = {"event": name, "data": data}
        return last

    def click(self, event: dict[str, Any], decision: str) -> dict[str, Any]:
        """点确认卡片上的按钮——这是唯一被认作授权的入口。"""

        payload = event.get("data") or event
        question_id = payload.get("question_id")
        pending_id = (payload.get("confirmation") or {}).get("pending_id")
        print(f"\n点击按钮：{decision}")
        response = self.client.post(
            f"{self.agent}/conversations/{self.conversation_id}"
            f"/pending-actions/{pending_id}/decide",
            json={"question_id": question_id, "decision": decision},
            headers={"Idempotency-Key": str(uuid.uuid4())},
        )
        print(f"  HTTP {response.status_code}")
        if response.status_code >= 400:
            print(f"  {response.text[:300]}")
            return event
        incoming = parse_sse(response.text.splitlines())
        tail = event
        for name, data in incoming:
            show(name, data)
            tail = {"event": name, "data": data}
        return tail

    def close(self) -> None:
        self.client.close()


def last_event(response: httpx.Response) -> dict[str, Any]:
    """非流式的 decide 接口返回的是一整串 SSE 文本。"""

    tail: dict[str, Any] = {}
    for line in response.text.splitlines():
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload in ("", "[DONE]"):
            continue
        event = json.loads(payload)
        Conversation._show(event)
        tail = event
    return tail


def bank_state(base: str) -> str:
    rows = (
        httpx.Client(timeout=20, trust_env=False)
        .get(f"{base}/subscriptions", params={"user_id": BANK_CUSTOMER})
        .json()["data"]
    )
    return ", ".join(f"{row['merchant_name']}={row['status']}" for row in rows) or "（无）"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--agent-url", default=AGENT_URL)
    parser.add_argument("--bank-url", default=BANK_URL)
    args = parser.parse_args()

    try:
        httpx.Client(timeout=8, trust_env=False).get(f"{args.agent_url}/health").raise_for_status()
    except Exception as exc:  # noqa: BLE001
        print(f"连不上主 Agent 服务 {args.agent_url}：{type(exc).__name__}")
        return 2

    print(f"银行真实状态（开始前）：{bank_state(args.bank_url)}")
    chat = Conversation(args.agent_url)
    chat.open()

    rule("① 订阅识别（只读）")
    chat.say("我有哪些自动扣费的订阅？")
    print(f"  银行状态：{bank_state(args.bank_url)}")

    rule("② 续费提醒（只读）")
    chat.say("最近有什么订阅要续费？")

    rule("③ 取消订阅 —— 应停在黄色确认卡片")
    event = chat.say("帮我取消 Netflix 的订阅")
    print(f"  银行状态（卡片弹出时仍应为 active）：{bank_state(args.bank_url)}")

    rule("④ 点击「确认」—— 才真正写库")
    chat.click(event, "confirm")
    print(f"  银行状态（确认后应为 cancelled）：{bank_state(args.bank_url)}")

    rule("⑤ 目标不明确 —— 应追问，不猜")
    chat.say("取消订阅")

    rule("⑥ 再发起一次并点「取消」—— 放弃操作，银行不变")
    event = chat.say("取消 Spotify 订阅")
    chat.click(event, "cancel")
    print(f"  银行状态（点取消后 Spotify 仍应为 active）：{bank_state(args.bank_url)}")

    rule("冒烟结束")
    chat.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
