"""The registry contract: what a module must declare, and what it may not."""

import pytest

from app.agents.declaration import DomainDeclaration
from app.modules.base import ModuleSpec, ToolData, ToolFailure, as_envelope
from app.modules.registry import build_registry
from app.tools.protocol import ErrorCode, ToolSpec

ECHO = ToolSpec(name="echo", description="Return what it was given")


def handler(invocation, client):
    return ToolData(summary="ok", display={}, facts={})


def test_a_module_must_implement_every_tool_it_declares():
    with pytest.raises(ValueError, match="without handlers: echo"):
        ModuleSpec(name="broken", tools={"echo": ECHO}, handlers={}, grants={"a": ("echo",)})


def test_a_module_may_not_have_a_handler_for_an_undeclared_tool():
    with pytest.raises(ValueError, match="without tools: echo"):
        ModuleSpec(name="broken", tools={}, handlers={"echo": handler})


def test_a_tool_nobody_can_reach_is_a_declaration_error():
    with pytest.raises(ValueError, match="nobody can reach"):
        ModuleSpec(name="broken", tools={"echo": ECHO}, handlers={"echo": handler})


def test_a_grant_that_names_no_tools_is_not_a_grant():
    """`grants={"a": ()}` is a non-empty mapping that still reaches nothing."""

    with pytest.raises(ValueError, match="nobody can reach"):
        ModuleSpec(
            name="broken", tools={"echo": ECHO}, handlers={"echo": handler}, grants={"a": ()}
        )


def test_a_tool_may_not_be_filed_under_a_name_it_does_not_carry():
    """The key is the name in `handlers`; `ToolSpec.name` is the name everywhere else."""

    declaration = DomainDeclaration(agent_id="alpha", display="Alpha", purpose="领域")

    with pytest.raises(ValueError, match="files a tool under a name it does not carry: echo"):
        ModuleSpec(
            name="broken",
            declaration=declaration,
            tools={"echo": ToolSpec(name="ping")},
            handlers={"echo": handler},
        )


def test_sharing_a_tool_does_not_cost_the_module_its_own_tools():
    """`grants` expresses sharing, so declaring one adds a caller rather than replacing."""

    declaration = DomainDeclaration(agent_id="alpha", display="Alpha", purpose="领域")
    spec = ModuleSpec(
        name="a",
        declaration=declaration,
        tools={"echo": ECHO},
        handlers={"echo": handler},
        grants={"beta": ("echo",)},
    )

    assert spec.effective_grants == {"beta": ("echo",), "alpha": ("echo",)}


def test_grants_default_to_the_modules_own_agent():
    declaration = DomainDeclaration(agent_id="account", display="账户", purpose="看账户")
    spec = ModuleSpec(
        name="account",
        declaration=declaration,
        tools={"echo": ECHO},
        handlers={"echo": handler},
    )

    assert spec.effective_grants == {"account": ("echo",)}


def test_an_envelope_requires_a_non_empty_summary():
    with pytest.raises(ValueError, match="summary"):
        ToolData(summary="  ", display={}, facts={})


@pytest.mark.parametrize("field", ["display", "facts", "business_ids"])
def test_an_envelope_requires_dicts(field):
    values = {"summary": "ok", "display": {}, "facts": {}, "business_ids": {}}
    values[field] = "not-a-dict"

    with pytest.raises(ValueError, match=field):
        ToolData(**values)


def test_a_handler_that_returns_a_bare_dict_is_rejected_loudly():
    with pytest.raises(TypeError, match="tool echo returned dict, expected ToolData"):
        as_envelope({"summary": "ok"}, tool_name="echo")


def test_an_envelope_round_trips_to_a_plain_dict():
    envelope = ToolData(
        summary="活期余额 8,320.00 元",
        display={"balance": "8,320.00 元"},
        facts={"balance_minor": 832_000},
        business_ids={"account_id": "acc_1"},
    )

    assert as_envelope(envelope, tool_name="echo") == {
        "summary": "活期余额 8,320.00 元",
        "display": {"balance": "8,320.00 元"},
        "facts": {"balance_minor": 832_000},
        "business_ids": {"account_id": "acc_1"},
    }


def test_tool_failure_defaults_to_not_retryable():
    failure = ToolFailure(ErrorCode.INTERNAL, "工具暂时不可用")

    assert failure.code == "INTERNAL"
    assert failure.message == "工具暂时不可用"
    assert failure.retryable is False


ALPHA = DomainDeclaration(agent_id="alpha", display="Alpha", purpose="第一个领域")
BETA = DomainDeclaration(agent_id="beta", display="Beta", purpose="第二个领域")


def module(name, declaration, tool_name, *, grants=None):
    spec = ToolSpec(name=tool_name, description=f"{tool_name} tool")
    return ModuleSpec(
        name=name,
        declaration=declaration,
        tools={tool_name: spec},
        handlers={tool_name: handler},
        grants=grants or {},
    )


def test_the_roster_and_the_catalog_come_from_the_same_declaration():
    registry = build_registry([module("a", ALPHA, "echo"), module("b", BETA, "ping")])

    assert [item.agent_id for item in registry.roster] == ["alpha", "beta"]
    assert registry.catalog.names() == ("echo", "ping")
    assert registry.catalog.for_agent("alpha").names == ("echo",)
    assert registry.catalog.for_agent("beta").names == ("ping",)
    assert set(registry.handlers) == {"echo", "ping"}


def test_a_module_grants_its_own_tools_by_default():
    registry = build_registry([module("a", ALPHA, "echo")])

    assert registry.catalog.for_agent("alpha").names == ("echo",)


def test_two_modules_may_not_declare_the_same_agent():
    with pytest.raises(ValueError, match="agent is declared twice: alpha"):
        build_registry([module("a", ALPHA, "echo"), module("b", ALPHA, "ping")])


def test_two_modules_may_not_declare_the_same_tool():
    other = DomainDeclaration(agent_id="beta", display="Beta", purpose="第二个领域")
    with pytest.raises(ValueError, match="tool is declared twice: echo"):
        build_registry([module("a", ALPHA, "echo"), module("b", other, "echo")])


def test_two_modules_may_not_share_a_name():
    with pytest.raises(ValueError, match="module is declared twice: a"):
        build_registry([module("a", ALPHA, "echo"), module("a", BETA, "ping")])


def test_a_grant_may_not_name_an_undeclared_agent():
    with pytest.raises(ValueError, match="undeclared agents: ghost"):
        build_registry([module("a", ALPHA, "echo", grants={"ghost": ("echo",)})])


def test_a_shared_tool_is_declared_once_and_granted_to_another_agent():
    shared = ModuleSpec(
        name="shared",
        declaration=None,
        tools={"ping": ToolSpec(name="ping")},
        handlers={"ping": handler},
        grants={"alpha": ("ping",), "beta": ("ping",)},
    )
    registry = build_registry([module("a", ALPHA, "echo"), module("b", BETA, "other"), shared])

    assert registry.catalog.for_agent("alpha").names == ("echo", "ping")
    assert registry.catalog.for_agent("beta").names == ("other", "ping")


def test_an_empty_module_list_is_a_declaration_error():
    with pytest.raises(ValueError, match="at least one module"):
        build_registry([])


def test_a_redundant_grant_does_not_duplicate_a_tool_for_the_agent():
    # alpha already reaches "echo" by default from its own module; the shared module
    # re-grants that same tool alongside the one it declares.
    shared = ModuleSpec(
        name="shared",
        declaration=None,
        tools={"ping": ToolSpec(name="ping")},
        handlers={"ping": handler},
        grants={"alpha": ("echo", "ping")},
    )
    registry = build_registry([module("a", ALPHA, "echo"), shared])

    assert registry.catalog.for_agent("alpha").names == ("echo", "ping")


def test_the_production_registry_is_the_six_domains_in_their_fixed_order():
    from app.modules import ALL_MODULES
    from app.modules.base import ModuleDeps
    from app.modules.registry import build_registry

    registry = build_registry()

    assert [item.agent_id for item in registry.roster] == [
        "transfer",
        "bill",
        "wealth",
        "card",
        "subscription",
        "life_event",
    ]

    # 要运行时依赖的域自己声明 binder（modules/base.py 的 `ModuleSpec.binder`）：组合根只
    # 提供一把 `ModuleDeps`，不再认识哪一个模块。账单、转账、理财、订阅、生活事件是今天需要
    # 平台服务的五家：订阅与生活事件的确认卡都要读当日累计（database），扣款点击与下单、撤销
    # 都要走资金写门面（writes）。
    bound_modules = {module.name for module in ALL_MODULES if module.binder}
    assert bound_modules == {"bill", "transfer", "wealth", "subscription", "life_event"}

    # binder 只换 handler 与图构建器，绝不改名：roster、提示词里的 agent 名与检查点里的键
    # 都是这个名字，而装配发生在 import 之后，改名会让这三处悄悄对不上。
    deps = ModuleDeps(notification_service=object(), database=object(), writes=object())
    for module in ALL_MODULES:
        if module.binder is None:
            continue
        bound = module.binder(module, deps)
        assert isinstance(bound, ModuleSpec)
        assert bound.name == module.name


def test_the_production_catalog_is_the_tools_of_card_wealth_transfer_and_subscription():
    """Card, wealth, transfer, subscription and life_event declare tools of their own.

    A module answers with a tool menu and, when it wants to, a `graph_builder`. There is no
    second execution path left to declare. 订阅已经切到同样的形状：两个只读工具、四个冻结
    快照的写工具、一个对模型隐藏的执行工具，加一张自己的图。生活事件这一批有三个只读
    工具、一个取消写工具和一个隐藏的下单工具，同样带一张自己的图。转账另有
    四个 AA 收款工具和一个定时转账解析工具，确认后只创建任务单、不移动资金。
    """

    from app.modules import ALL_MODULES

    registry = build_registry()

    card = next(module for module in ALL_MODULES if module.name == "card")
    wealth = next(module for module in ALL_MODULES if module.name == "wealth")
    transfer = next(module for module in ALL_MODULES if module.name == "transfer")
    subscription = next(module for module in ALL_MODULES if module.name == "subscription")
    life_event = next(module for module in ALL_MODULES if module.name == "life_event")
    assert set(registry.catalog.names()) == (
        set(card.tools)
        | set(wealth.tools)
        | set(transfer.tools)
        | set(subscription.tools)
        | set(life_event.tools)
    )
    assert set(registry.handlers) == set(registry.catalog.names())
    assert len(card.tools) == 13
    assert len(wealth.tools) == 7
    assert len(transfer.tools) == 10
    assert len(subscription.tools) == 7
    assert len(life_event.tools) == 5
    assert all(
        module.tools == {}
        for module in ALL_MODULES
        if module.name not in {"card", "wealth", "transfer", "subscription", "life_event"}
    )


def test_every_domain_declares_a_display_name_and_a_purpose():
    from app.modules import ALL_MODULES

    for module in ALL_MODULES:
        assert module.declaration is not None
        assert module.declaration.display.strip()
        assert module.declaration.purpose.strip()


def test_production_card_password_capability_reaches_roster_and_native_tool_menu():
    from app.modules import production_modules
    from app.modules.card import MODULE
    from app.tools.schema import function_schema

    registry = build_registry(
        production_modules(notification_service=object(), database=object(), writes=object())
    )
    card = next(module for module in registry.modules if module.name == "card")
    assert card.declaration.graph_builder is MODULE.declaration.graph_builder
    assert (
        "交易密码修改" in next(item for item in registry.roster if item.agent_id == "card").purpose
    )
    assert "card_change_password" in registry.catalog.for_agent("card").names
    schema = function_schema(card.tools["card_change_password"])
    assert set(schema["function"]["parameters"]["properties"]) == {"card_suffix", "card_type"}


def registry_with_spec(spec):
    return build_registry(
        [
            ModuleSpec(
                name="alpha",
                declaration=ALPHA,
                tools={spec.name: spec},
                handlers={spec.name: handler},
            )
        ]
    )


def test_module_spec_has_no_turn_field():
    """R-1: the second execution path is gone, not merely unused.

    A module answers with a tool menu and, optionally, its own `graph_builder`. Re-introducing
    a whole-turn handler must fail loudly at construction instead of adding a path back.
    """

    with pytest.raises(TypeError, match="turn"):
        ModuleSpec(name="x", turn=lambda client: None)


@pytest.mark.parametrize("reserved", ["confirmed", "idempotency_key", "call_id", "agent_id"])
def test_a_tool_may_not_declare_a_name_the_base_injects(reserved):
    """Contract 3.3 check 9: the base injects these, so a model must never be able to set one.

    A tool declaring `idempotency_key` would put a model-chosen key beside the minted one,
    and a handler reading the wrong copy is how one timeout becomes two transfers.
    """

    spec = ToolSpec(
        name="echo",
        description="e",
        parameters={"type": "object", "properties": {reserved: {"type": "string"}}},
    )

    with pytest.raises(ValueError, match="reserved parameter"):
        registry_with_spec(spec)


def test_the_reserved_name_refusal_names_the_tool_and_the_property():
    spec = ToolSpec(
        name="echo",
        description="e",
        parameters={"type": "object", "properties": {"call_id": {"type": "string"}}},
    )

    with pytest.raises(ValueError, match="echo.*call_id"):
        registry_with_spec(spec)


def test_an_ordinary_argument_is_not_mistaken_for_a_reserved_one():
    """The guard must not be so broad that it refuses a legitimate argument schema."""

    spec = ToolSpec(
        name="echo",
        description="e",
        parameters={"type": "object", "properties": {"amount_minor": {"type": "integer"}}},
    )

    assert registry_with_spec(spec).catalog.names() == ("echo",)
