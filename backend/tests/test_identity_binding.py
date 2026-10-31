"""The one place a project user is mapped to the identity the bank knows."""

import asyncio

from sqlalchemy import select

from app.agents.runtime import AgentRuntime
from app.storage.models import AppUser
from app.storage.seed import seed


def test_the_seeded_demo_user_is_bound_to_the_banks_demo_customer(rt):
    assert rt.identity.bank_customer_id("user_0001") == "u_wealth"


def test_an_unknown_user_has_no_bank_binding_rather_than_a_default_one(rt):
    assert rt.identity.bank_customer_id("user_9999") is None


def test_the_column_is_nullable_so_a_user_may_exist_without_a_binding(rt):
    with rt.database.sessions.begin() as db:
        db.add(AppUser(user_id="user_0002"))

    assert rt.identity.bank_customer_id("user_0002") is None


def test_reseeding_binds_a_demo_user_that_predates_the_binding(rt):
    """The column arrives nullable and unbackfilled, so a `user_0001` created before this
    feature existed would stay unbound -- and every bank tool fails closed without a
    binding, so the app would look wired up while answering nothing."""

    with rt.database.sessions.begin() as db:
        user = db.scalar(select(AppUser).where(AppUser.user_id == "user_0001"))
        user.bank_customer_id = None

    seed(rt.database, None)

    assert rt.identity.bank_customer_id("user_0001") == "u_wealth"


def test_reseeding_leaves_an_already_bound_user_alone(rt):
    seed(rt.database, None)

    assert rt.identity.bank_customer_id("user_0001") == "u_wealth"


def test_the_production_runtime_wires_the_resolver_into_the_agent(rt):
    """Without this wiring every user resolves to None and every bank call is refused, while
    the rest of the suite stays green -- the failure looks like "the bank is down"."""

    assert asyncio.run(rt.agent._resolve_customer("user_0001")) == "u_wealth"


def test_a_failing_resolver_degrades_to_no_binding_never_to_a_guess():
    """The direction that matters: an error must not become a customer id."""

    def explode(user_id):
        raise RuntimeError("identity store is down")

    runtime = AgentRuntime(
        graph_factory=lambda model, context, assembler=None, turn=None: None,
        customer_resolver=explode,
    )

    assert asyncio.run(runtime._resolve_customer("user_0001")) is None
