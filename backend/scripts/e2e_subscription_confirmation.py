"""Manual end-to-end check: browser HTTP -> main agent -> subscription -> bank.

This is the *manual* harness, not a pytest suite. The automated suites stop at their own
boundaries (the offline subscription tests use an HTTP double; the live-bank test drives the
module in-process). The thing none of them can prove is that the two real processes plus a
real model agree. This script drives that chain over HTTP and prints what each hop actually
answered:

1. the user's own words, through the ordinary message route;
2. the confirmation card the page renders, and the same card read back from the checkpoint
   after a "reload" -- including after the agent process itself is restarted;
3. a chat message that says 确认 -- not an authorization;
4. the authenticated button event, and a repeat of it (which must be one submission);
5. the bank's own rows before and after a confirmed pause/resume, and after a preview that
   was never confirmed.

Prerequisites (see docs/subscription-agent.md): a bank process whose schema carries the
subscription demo seed (bank_backend/scripts/seed_subscription_demo.py), and the agent
pointed at it. Then:

    cd backend
    uv run python scripts/e2e_subscription_confirmation.py \
        --agent-url http://127.0.0.1:8010/api --bank-url http://127.0.0.1:8011/api

Modes:
  (default)        full chain in one process: list, pause Spotify, prove the card parks and
                   the bank is untouched, reload the card, prove chat 确认 is not an
                   authorization, click once, repeat the click, then resume Spotify.
  --setup          stop at the raised card and print CONVERSATION_ID / PENDING_ID /
                   QUESTION_ID (used to restart the process and then --from-card).
  --from-card ID   read the card back from the checkpoint (after a restart) and finish the
                   chain: reload, chat 确认, click once, repeat click, resume.
  --capture ID     write that conversation to frontend/.e2e/capture.json and exit.
  --preview        raise a charge preview and never confirm it: assert the bank did not move.

Pause/resume is used by default because cancel is a terminal state in the bank and would
poison the demo rows after one run; reset between cases with
bank_backend/scripts/seed_subscription_demo.py --reset.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

DEFAULT_AGENT = "http://127.0.0.1:8000/api"
DEFAULT_BANK = "http://127.0.0.1:8001/api"
CAPTURE_PATH = Path(__file__).resolve().parents[2] / "frontend" / ".e2e" / "capture.json"
CUSTOMER = "u_wealth"
DEFAULT_MERCHANT = "Spotify"
LIST_REQUEST = "我有哪些自动扣费的订阅？"
PAUSE_REQUEST = "帮我把 Spotify 停一停"
RESUME_REQUEST = "把 Spotify 恢复"
CHARGE_REQUEST = "现在就扣 Netflix 这一期"


def log_request(request: httpx.Request) -> None:
    print(f"HTTP -> {request.method} {request.url}")


def log_response(response: httpx.Response) -> None:
    print(
        f"HTTP <- {response.status_code} {response.request.method} {response.url} "
        f"({response.headers.get('content-type')})"
    )


def frames(text: str) -> list[tuple[str | None, dict[str, Any]]]:
    """Parse an SSE body into (event_name, data) pairs; None means an unnamed frame."""

    parsed: list[tuple[str | None, dict[str, Any]]] = []
    for block in text.split("\n\n"):
        if not block.strip():
            continue
        lines = block.split("\n")
        name = next((line[6:].strip() for line in lines if line.startswith("event:")), None)
        payload = next((line[5:].strip() for line in lines if line.startswith("data:")), None)
        if payload is None or payload == "[DONE]":
            continue
        envelope = json.loads(payload)
        if envelope.get("error"):
            parsed.append((name, {"__error__": envelope["error"]}))
            continue
        parsed.append((name, envelope["data"]))
    return parsed


def events(response: httpx.Response) -> list[tuple[str | None, dict[str, Any]]]:
    """The frames of a response, whether it is an SSE stream or a JSON replay receipt."""

    content_type = response.headers.get("content-type", "")
    if "application/json" in content_type:
        envelope = response.json()
        if envelope.get("error"):
            return [("json", {"__error__": envelope["error"]})]
        return [("json", envelope["data"])]
    return frames(response.text)


def show(title: str, value: Any) -> None:
    print(f"\n=== {title} ===")
    print(json.dumps(value, ensure_ascii=False, indent=2)[:8000])


def post_message(
    client: httpx.Client, agent: str, conversation_id: str, message: str
) -> httpx.Response:
    return client.post(
        f"{agent}/conversations/{conversation_id}/messages",
        json={"message": message},
        headers={"Idempotency-Key": f"e2e-{uuid4().hex}"},
    )


def say(
    client: httpx.Client, agent: str, conversation_id: str, message: str
) -> list[tuple[str | None, dict[str, Any]]]:
    response = post_message(client, agent, conversation_id, message)
    parsed = events(response)
    print(
        f"\n--- POST /conversations/{conversation_id}/messages  "
        f"message={message!r} -> HTTP {response.status_code}"
    )
    print("    events:", [name for name, _ in parsed])
    for name, data in parsed:
        if name == "final":
            print("    final:", str(data.get("text"))[:400])
        elif name == "interrupt":
            print("    interrupt:", json.dumps(data, ensure_ascii=False)[:1500])
        elif name == "subtask_end":
            print(
                f"    subtask_end[{data.get('agent_id')}] "
                f"{data.get('turn_status')}: {str(data.get('summary'))[:200]}"
            )
        elif "__error__" in data:
            print("    error:", json.dumps(data["__error__"], ensure_ascii=False)[:600])
    return parsed


def card_of(parsed: list[tuple[str | None, dict[str, Any]]]) -> dict[str, Any] | None:
    return next(
        (
            data
            for name, data in parsed
            if name == "interrupt"
            and isinstance(data, dict)
            and isinstance(data.get("confirmation"), dict)
        ),
        None,
    )


def bank_rows(
    client: httpx.Client, bank: str, merchant: str | None = None
) -> list[dict[str, Any]]:
    response = client.get(f"{bank}/subscriptions", params={"user_id": CUSTOMER})
    response.raise_for_status()
    rows = response.json()["data"]
    if merchant:
        rows = [row for row in rows if row.get("merchant_name") == merchant]
    return rows


def show_bank(label: str, rows: list[dict[str, Any]]) -> None:
    print(f"\n--- bank rows {label} ---")
    if not rows:
        print("    (none)")
    for row in rows:
        print(
            f"    {row['subscription_id']} {row['merchant_name']}: "
            f"status={row['status']} amount_minor={row['amount_minor']} "
            f"last_charge_id={row.get('last_charge_id')} next_due_date={row.get('next_due_date')}"
        )


def status_of(rows: list[dict[str, Any]], merchant: str) -> str | None:
    return next((row["status"] for row in rows if row["merchant_name"] == merchant), None)


def read_card(
    client: httpx.Client, agent: str, conversation_id: str
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    detail = client.get(f"{agent}/conversations/{conversation_id}").json()["data"]
    return detail, detail.get("pending_interrupt")


def open_conversation(client: httpx.Client, agent: str) -> str:
    created = client.post(f"{agent}/conversations", json={}).json()["data"]
    conversation_id = created["conversation_id"]
    print(f"\nconversation: {conversation_id} (serial #{created.get('serial_no')})")
    return conversation_id


def drive_to_card(
    client: httpx.Client, agent: str, conversation_id: str, request: str
) -> dict[str, Any]:
    card = card_of(say(client, agent, conversation_id, request))
    if card is None:
        raise SystemExit(f"the write request never raised a confirmation card: {request!r}")
    show("interrupt (the card the page renders)", card)
    return card


def decide(
    client: httpx.Client,
    agent: str,
    conversation_id: str,
    question_id: str,
    pending_id: str,
    decision: str,
    key: str,
) -> httpx.Response:
    return client.post(
        f"{agent}/conversations/{conversation_id}/pending-actions/{pending_id}/decide",
        json={"question_id": question_id, "decision": decision},
        headers={"Idempotency-Key": key},
    )


def print_decision(response: httpx.Response) -> None:
    print(
        f"\nPOST decide -> HTTP {response.status_code} "
        f"content-type={response.headers.get('content-type')}"
    )
    for name, data in events(response):
        if name == "final":
            print("    final:", str(data.get("text"))[:500])
        elif name == "json":
            print("    receipt:", json.dumps(data, ensure_ascii=False)[:500])
        else:
            print(f"    {name}:", json.dumps(data, ensure_ascii=False)[:600])


def confirm_open_card(
    client: httpx.Client,
    agent: str,
    bank: str,
    conversation_id: str,
    merchant: str,
    *,
    chat_first: bool,
) -> None:
    """Read the open card, (optionally) prove chat 确认 is not authorization, then click twice."""

    detail, pending = read_card(client, agent, conversation_id)
    show("GET /conversations/{id} (the card read back from the checkpoint)", pending)
    if not pending:
        raise SystemExit("no open card in the checkpoint")
    question_id = pending["question_id"]
    pending_id = pending["confirmation"]["pending_id"]
    confirmation = pending["confirmation"]
    print(
        f"\ncard: pending_id={pending_id} question_id={question_id} "
        f"operation={confirmation.get('operation')} requires_code={confirmation.get('requires_code')}"
    )

    if chat_first:
        say(client, agent, conversation_id, "确认")
        show_bank(
            "after the chat message 确认 (must be unchanged: a chat message is not a click)",
            bank_rows(client, bank, merchant),
        )
        _, reopened = read_card(client, agent, conversation_id)
        if reopened:
            question_id = reopened["question_id"]
            pending_id = reopened["confirmation"]["pending_id"]
            print(f"\ncard still open after the chat message: pending_id={pending_id} question_id={question_id}")
        else:
            print("\nno open card after the chat message; using the ids read before it")

    key = uuid4().hex
    print(f"\nclick Idempotency-Key={key}")
    print_decision(
        decide(client, agent, conversation_id, question_id, pending_id, "confirm", key)
    )
    show_bank(
        "after the confirmed click (must show the new state)",
        bank_rows(client, bank, merchant),
    )

    again = decide(client, agent, conversation_id, question_id, pending_id, "confirm", key)
    print(
        f"\nrepeat click (same Idempotency-Key={key}) -> HTTP {again.status_code} "
        f"content-type={again.headers.get('content-type')}"
    )
    print("    body:", again.text[:400])
    show_bank(
        "after the repeat click (must be exactly one submission)",
        bank_rows(client, bank, merchant),
    )


def capture(client: httpx.Client, agent: str, conversation_id: str) -> None:
    detail = client.get(f"{agent}/conversations/{conversation_id}").json()["data"]
    messages = client.get(f"{agent}/conversations/{conversation_id}/messages").json()["data"]
    CAPTURE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CAPTURE_PATH.write_text(
        json.dumps(
            {
                "conversation": detail["conversation"],
                "pending_interrupt": detail["pending_interrupt"],
                "messages": messages["items"],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\nwrote {CAPTURE_PATH}")


def park(
    client: httpx.Client, agent: str, bank: str, args: argparse.Namespace
) -> tuple[str, dict[str, Any]]:
    show_bank("before", bank_rows(client, bank))
    conversation_id = open_conversation(client, agent)
    say(client, agent, conversation_id, LIST_REQUEST)
    show_bank("after the read (must be unchanged)", bank_rows(client, bank, args.merchant))
    request = args.request or PAUSE_REQUEST
    card = drive_to_card(client, agent, conversation_id, request)
    show_bank(
        "after the card (must still be active: a preview writes nothing)",
        bank_rows(client, bank, args.merchant),
    )
    return conversation_id, card


def resume(client: httpx.Client, agent: str, bank: str, conversation_id: str, merchant: str) -> None:
    print("\n########## resume ##########")
    drive_to_card(client, agent, conversation_id, RESUME_REQUEST)
    confirm_open_card(
        client, agent, bank, conversation_id, merchant, chat_first=False
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent-url", default=DEFAULT_AGENT)
    parser.add_argument("--bank-url", default=DEFAULT_BANK)
    parser.add_argument(
        "--setup",
        action="store_true",
        help="stop at the raised card and print the ids (used before a process restart)",
    )
    parser.add_argument(
        "--from-card",
        metavar="CONVERSATION_ID",
        help="read the card back from the checkpoint (after a restart) and finish the chain",
    )
    parser.add_argument(
        "--capture",
        metavar="CONVERSATION_ID",
        help="write that conversation to frontend/.e2e/capture.json and exit",
    )
    parser.add_argument(
        "--preview",
        action="store_true",
        help="raise a charge preview and never confirm it (assert the bank did not move)",
    )
    parser.add_argument("--merchant", default=DEFAULT_MERCHANT)
    parser.add_argument("--request", default=None, help="override the write request text")
    parser.add_argument("--skip-resume", action="store_true")
    args = parser.parse_args()

    agent = args.agent_url.rstrip("/")
    bank = args.bank_url.rstrip("/")

    with httpx.Client(
        timeout=180.0,
        trust_env=False,
        event_hooks={"request": [log_request], "response": [log_response]},
    ) as client:
        client.post(f"{agent}/session")
        show("agent health", client.get(f"{agent}/health").json()["data"])

        if args.capture:
            capture(client, agent, args.capture)
            return 0

        if args.preview:
            conversation_id, card = park(client, agent, bank, args)
            show_bank(
                "at the end of the never-confirmed preview (must be unchanged)",
                bank_rows(client, bank, args.merchant),
            )
            print(f"\nCONVERSATION_ID={conversation_id}")
            print(f"PENDING_ID={card['confirmation']['pending_id']}")
            return 0

        if args.from_card:
            confirm_open_card(
                client, agent, bank, args.from_card, args.merchant, chat_first=True
            )
            if not args.skip_resume:
                resume(client, agent, bank, args.from_card, args.merchant)
            print(f"\nCONVERSATION_ID={args.from_card}")
            return 0

        conversation_id, card = park(client, agent, bank, args)
        print(f"\nPENDING_ID={card['confirmation']['pending_id']}")
        print(f"QUESTION_ID={card['question_id']}")
        if args.setup:
            print(f"\nCONVERSATION_ID={conversation_id}")
            return 0
        confirm_open_card(
            client, agent, bank, conversation_id, args.merchant, chat_first=True
        )
        if not args.skip_resume:
            resume(client, agent, bank, conversation_id, args.merchant)
        print(f"\nCONVERSATION_ID={conversation_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
