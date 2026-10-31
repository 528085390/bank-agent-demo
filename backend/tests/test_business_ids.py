"""Business ids a tool returned must reach the client's final event.

Only the values are published. The keys (`account_id`, `transfer_id`) are internal names,
and putting them in front of a user is the same mistake as leaking an agent id.
"""

import asyncio

from langgraph.checkpoint.memory import InMemorySaver

from app.agents.declaration import DomainDeclaration
from app.agents.domain import build_agent_graph
from app.agents.runtime import AgentRuntime
from app.agents.supervisor import _collect_business_ids
from app.tools.catalog import ToolCatalog
from app.tools.gateway import ToolGateway
from app.tools.protocol import ToolSpec, TrustedContext
from app.tools.recording import RecordingAdapter
from tests.support.scripted_model import ScriptedModel
from tests.support.tool_domain_graph import tool_domain_graph_builder

ALPHA = DomainDeclaration(
    agent_id="alpha",
    display="Alpha",
    purpose="测试领域",
    graph_builder=tool_domain_graph_builder,
)
ECHO = ToolSpec(name="echo", description="Return what it was given")
CONTEXT = TrustedContext(user_id="user_0001", session_id="sess_1", bank_customer_id="u_zhang")

# One tool-calling turn: the Supervisor routes, the domain calls its tool, feeds the result
# back and reports, then the Supervisor writes the reply.
SCRIPT = [
    ("call", "handoff", '{"agent_id":"alpha","instruction":"做点什么"}'),
    ("call", "echo", "{}"),
    "已完成",
    "完成了",
]


def collect(replies, *, replies_for=None):
    recorder = RecordingAdapter(replies_for or {})
    gateway = ToolGateway(ToolCatalog.build([ECHO], {"alpha": ["echo"]}), recorder)
    model = ScriptedModel(replies)
    graph = build_agent_graph(model, gateway, CONTEXT, roster=(ALPHA,)).compile(
        checkpointer=InMemorySaver()
    )
    runtime = AgentRuntime(graph=graph)

    async def run():
        return [event async for event in runtime.invoke("sess_1", "user_0001", "帮我", {})]

    return asyncio.run(run())


def final_of(events):
    return next(event for event in events if event.event == "final").data


def test_business_ids_from_a_tool_result_reach_the_final_event():
    events = collect(
        SCRIPT,
        replies_for={
            "echo": {
                "summary": "ok",
                "display": {},
                "facts": {},
                "business_ids": {"echo_id": "e1"},
            }
        },
    )

    assert final_of(events)["business_ids"] == ["e1"]


def test_a_tool_result_without_business_ids_contributes_nothing():
    events = collect(SCRIPT, replies_for={"echo": {"summary": "ok", "display": {}, "facts": {}}})

    assert final_of(events)["business_ids"] == []


def test_a_turn_with_no_domain_publishes_an_empty_list():
    events = collect(["你好"])

    assert final_of(events)["business_ids"] == []


def test_only_the_values_are_published_never_the_internal_keys():
    events = collect(
        SCRIPT,
        replies_for={"echo": {"business_ids": {"account_id": "acc_1", "transfer_id": "tr_2"}}},
    )

    published = final_of(events)["business_ids"]

    assert published == ["acc_1", "tr_2"]
    assert not any("account_id" in item or "transfer_id" in item for item in published)


def test_collect_business_ids_dedupes_and_keeps_first_seen_order():
    results = [
        {"business_ids": {"a": "id_1", "b": "id_2"}},
        {"business_ids": {"c": "id_1"}},
        {"business_ids": {}},
        {},
    ]

    assert _collect_business_ids(results) == ["id_1", "id_2"]


def test_collect_business_ids_ignores_a_malformed_channel():
    assert _collect_business_ids([{"business_ids": "not-a-dict"}, {"business_ids": {"a": 5}}]) == []
