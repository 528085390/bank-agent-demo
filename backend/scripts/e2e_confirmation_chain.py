"""Manual end-to-end check: browser HTTP -> main agent -> wealth -> bank.

This is the *manual* harness, not a pytest suite. The automated suites stop at their own
boundaries (the offline wealth tests use an HTTP double; the API tests use a scripted model),
and the one thing none of them can prove is that the two real processes plus a real model
agree. This script drives that chain over HTTP and prints what each hop actually answered:

1. the user's own words, through the ordinary message route;
2. the confirmation card the page renders, and the same card read back from the checkpoint
   after a "reload" (the `GET /api/conversations/{id}` path);
3. a chat message that says 确认 -- refused, because it is not an authorization;
4. the authenticated button event, and a repeat of it (which must be one submission);
5. the ledger, and what the bank's own tables say was and was not submitted.

Prerequisites (see `app/modules/wealth/README.md` for the full sequence): a bank process on
:8001 whose schema is freshly seeded, and the agent on :8000 pointed at it. Then:

    cd backend
    uv run python scripts/e2e_confirmation_chain.py

`--capture <conversation_id>` writes the conversation to `frontend/.e2e/capture.json`, which
`frontend/src/e2ePreview.test.ts` renders for a screenshot. `--setup` runs only the turns that
produce a card and prints the conversation id, so a screenshot can be taken of a *live* pause.

`--code` is what the script types into the box. It defaults to the bank's configured demo code;
it is never read out of the bank's configuration, because a run that fetched the code itself
would not be proving that a *user* supplied it.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

AGENT = "http://127.0.0.1:8000/api"
CAPTURE_PATH = Path(__file__).resolve().parents[2] / "frontend" / ".e2e" / "capture.json"

# The bank's wealth demo seed creates the customer without a risk assessment, so the first
# thing the agent asks for is the questionnaire. These are the answers the bank scores as a
# high-risk profile, which is what lets a purchase preview be produced at all.
QUESTIONNAIRE_ANSWER = "风险承受能力 2，投资经验 2，收入稳定性 2，计划投资期限 2，可用资金比例 2"
PURCHASE_REQUEST = "买 2000 元的演示灵活理财，用主账户付款"
# A read question first, so the run also exercises a real Markdown reply from the model.
READ_REQUEST = "用 Markdown 表格列出演示理财产品（产品名、风险等级、预期年化、起购金额、赎回规则）"
MAX_TURNS = 6


def frames(text: str) -> list[tuple[str | None, dict[str, Any]]]:
    """Parse an SSE body into `(event_name, data)` pairs; `None` means an unnamed frame."""

    parsed: list[tuple[str | None, dict[str, Any]]] = []
    for block in text.split("\n\n"):
        if not block.strip():
            continue
        lines = block.split("\n")
        name = next((line[6:].strip() for line in lines if line.startswith("event:")), None)
        payload = next((line[5:].strip() for line in lines if line.startswith("data:")), None)
        if payload is None:
            continue
        envelope = json.loads(payload)
        if envelope.get("error"):
            parsed.append((name, {"__error__": envelope["error"]}))
            continue
        parsed.append((name, envelope["data"]))
    return parsed


def show(title: str, value: Any) -> None:
    print(f"\n=== {title} ===")
    print(json.dumps(value, ensure_ascii=False, indent=2)[:6000])


def post_message(client: httpx.Client, conversation_id: str, message: str, context=None):
    body: dict[str, Any] = {"message": message}
    if context is not None:
        body["client_context"] = context
    return client.post(
        f"{AGENT}/conversations/{conversation_id}/messages",
        json=body,
        headers={"Idempotency-Key": f"e2e-{uuid4().hex}"},
    )


def reach_confirmation(client: httpx.Client) -> tuple[str, dict[str, Any]]:
    """Drive one conversation until it pauses on a confirmation card, and return that card."""

    created = client.post(f"{AGENT}/conversations", json={}).json()["data"]
    conversation_id = created["conversation_id"]
    print(f"\nconversation: {conversation_id} (serial #{created['serial_no']})")

    for message in (READ_REQUEST, PURCHASE_REQUEST, QUESTIONNAIRE_ANSWER):
        response = post_message(client, conversation_id, message)
        events = frames(response.text)
        print(f"\n--- POST /messages -> {response.status_code} events={[n for n, _ in events]}")
        final = next((data for name, data in events if name == "final"), None)
        if final:
            print("final:", final["text"][:400])
        card = next(
            (data for name, data in events if name == "interrupt" and data.get("confirmation")),
            None,
        )
        if card:
            return conversation_id, card

    raise SystemExit("the chain never reached a confirmation card")


def capture(client: httpx.Client, conversation_id: str) -> None:
    detail = client.get(f"{AGENT}/conversations/{conversation_id}").json()["data"]
    ledger = client.get(f"{AGENT}/conversations/{conversation_id}/messages").json()["data"]
    CAPTURE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CAPTURE_PATH.write_text(
        json.dumps(
            {
                "conversation": detail["conversation"],
                "pending_interrupt": detail["pending_interrupt"],
                "messages": ledger["items"],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\nwrote {CAPTURE_PATH}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--setup",
        action="store_true",
        help="stop at the confirmation card (used to screenshot a live pause)",
    )
    parser.add_argument(
        "--capture",
        metavar="CONVERSATION_ID",
        help="write that conversation to frontend/.e2e/capture.json and exit",
    )
    parser.add_argument(
        "--code",
        default="123456",
        help="the code to type into the box (the bank's configured demo code by default)",
    )
    parser.add_argument(
        "--wrong-code",
        default="000000",
        help="a code the bank will reject, used to show the box re-opens for a retry",
    )
    args = parser.parse_args()

    with httpx.Client(timeout=180.0) as client:
        client.post(f"{AGENT}/session")
        show("health", client.get(f"{AGENT}/health").json()["data"])

        if args.capture:
            capture(client, args.capture)
            return 0

        conversation_id, card = reach_confirmation(client)
        show("interrupt (the card the page renders)", card)

        if args.setup:
            print(f"\nCONVERSATION_ID={conversation_id}")
            return 0

        # 2. The reload path: the same card must come back from the checkpoint.
        detail = client.get(f"{AGENT}/conversations/{conversation_id}").json()["data"]
        show("GET /conversations/{id} (reload)", detail["pending_interrupt"])

        # 3. A chat message that says 确认 is not an authorization.
        chat = post_message(client, conversation_id, "确认")
        print(f"\nPOST /messages {{'确认'}} -> {chat.status_code} {chat.text[:300]}")

        confirmation = card["confirmation"]
        pending_id = confirmation["pending_id"]
        decision_url = (
            f"{AGENT}/conversations/{conversation_id}/pending-actions/{pending_id}/decide"
        )

        def click(body, key):
            """One click. The key is the client's: a repeat reuses it, a retry does not."""

            return client.post(decision_url, json=body, headers={"Idempotency-Key": key})

        # 4. The card says whether the bank wants a code; the box must ask exactly then.
        print(f"\nrequires_code = {confirmation.get('requires_code')}")
        if confirmation.get("requires_code"):
            # 4a. A click with no code at all: refused before anything is written.
            without = click(
                {"question_id": card["question_id"], "decision": "confirm"}, uuid4().hex
            )
            print(f"click without a code -> {without.status_code} {without.text[:200]}")

            # 4b. A wrong code: the bank rejects it, and the box comes back for a retry.
            wrong = click(
                {
                    "question_id": card["question_id"],
                    "decision": "confirm",
                    "verification_code": args.wrong_code,
                },
                uuid4().hex,
            )
            print(
                f"\nclick with a wrong code -> {wrong.status_code} "
                f"events={[name for name, _ in frames(wrong.text)]}"
            )
            reopened = next(
                (data for name, data in frames(wrong.text) if name == "interrupt"), None
            )
            print(
                "the box re-opened:",
                json.dumps(reopened, ensure_ascii=False)[:300] if reopened else "NO",
            )
            if reopened:
                card = reopened
                confirmation = card["confirmation"]

        # 5. The real click: confirm + the code the user typed.
        body = {
            "question_id": card["question_id"],
            "decision": "confirm",
            "verification_code": args.code,
        }
        key = uuid4().hex
        decided = click(body, key)
        print(f"\nPOST /pending-actions/{pending_id}/decide -> {decided.status_code}")
        outcome = frames(decided.text)
        print("events:", [name for name, _ in outcome])
        show("decision outcome", outcome)

        # 6. The same click again with the same key: one submission, answered with its receipt.
        again = click(body, key)
        print(
            f"\nrepeat click (same key) -> {again.status_code} "
            f"{again.headers.get('Content-Type')} {again.text[:300]}"
        )

        ledger = client.get(f"{AGENT}/conversations/{conversation_id}/messages").json()["data"]
        show(
            "ledger",
            [
                {
                    "seq": item["sequence"],
                    "role": item["role"],
                    "kind": item["message_kind"],
                    "content": item["content"][:300],
                }
                for item in ledger["items"]
            ],
        )
        print(f"\nCONVERSATION_ID={conversation_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
