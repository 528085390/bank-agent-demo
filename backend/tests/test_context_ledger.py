from app.context.ledger import LedgerMessage, drop_superseded_answers, read_ledger


def message(sequence, role, content, kind="message", question_id=None):
    return LedgerMessage(sequence, role, content, kind, question_id)


def answer(sequence, question_id, content):
    return message(sequence, "user", content, kind="interrupt_answer", question_id=question_id)


def test_a_superseded_answer_is_dropped_and_the_last_one_kept():
    """The persistence spec's promise: the old answer stays in the ledger, not in the prompt."""

    rows = [
        message(1, "user", "我要转账"),
        answer(2, "q_1", "从 A 到 B"),
        answer(3, "q_1", "从 A 到 C"),
    ]

    kept = drop_superseded_answers(rows)

    assert [row.sequence for row in kept] == [1, 3]
    assert kept[-1].content == "从 A 到 C"


def test_answers_to_different_questions_are_all_kept():
    rows = [answer(1, "q_1", "甲"), answer(2, "q_2", "乙")]

    assert [row.sequence for row in drop_superseded_answers(rows)] == [1, 2]


def test_ordinary_messages_are_untouched():
    rows = [message(1, "user", "你好"), message(2, "assistant", "您好")]

    assert drop_superseded_answers(rows) == rows


def test_an_answer_without_a_question_id_is_kept():
    """A row that cannot be attributed to a question cannot be proven superseded."""

    rows = [answer(1, None, "甲"), answer(2, None, "乙")]

    assert [row.sequence for row in drop_superseded_answers(rows)] == [1, 2]


def test_the_kept_rows_stay_in_sequence_order():
    rows = [answer(1, "q_1", "旧"), message(2, "assistant", "好"), answer(3, "q_1", "新")]

    assert [row.sequence for row in drop_superseded_answers(rows)] == [2, 3]


def test_reading_the_ledger_filters_by_conversation(rt, session):
    """`read_ledger` is the one place the view and the summary both read history from."""

    first = rt.conversations.create(session["user_id"], "甲")
    second = rt.conversations.create(session["user_id"], "乙")
    rt.conversations.append_user_message(
        session["user_id"],
        first.conversation_id,
        idempotency_key="k1",
        expected_hash="h1",
        content="第一句",
    )
    rt.conversations.append_assistant_message(
        session["user_id"],
        first.conversation_id,
        run_id=None,
        content="第一答",
    )
    rt.conversations.append_user_message(
        session["user_id"],
        second.conversation_id,
        idempotency_key="k2",
        expected_hash="h2",
        content="别处的句子",
    )

    rows = read_ledger(
        rt.database, first.conversation_id, after_sequence=0, user_id=session["user_id"]
    )

    assert [row.content for row in rows] == ["第一句", "第一答"]
    assert [row.sequence for row in rows] == [1, 2]


def test_reading_after_a_sequence_skips_the_summarized_prefix(rt, session):
    created = rt.conversations.create(session["user_id"], "甲")
    rt.conversations.append_user_message(
        session["user_id"],
        created.conversation_id,
        idempotency_key="k1",
        expected_hash="h1",
        content="旧",
    )
    rt.conversations.append_assistant_message(
        session["user_id"],
        created.conversation_id,
        run_id=None,
        content="旧答",
    )

    assert (
        read_ledger(
            rt.database,
            created.conversation_id,
            after_sequence=2,
            user_id=session["user_id"],
        )
        == []
    )


def test_the_stale_answer_filter_applies_to_what_is_read(rt, session):
    """Both reading rules are one operation, not a step callers may forget."""

    created = rt.conversations.create(session["user_id"], "甲")
    rt.conversations.append_user_message(
        session["user_id"],
        created.conversation_id,
        idempotency_key="k1",
        expected_hash="h1",
        content="旧的补充",
        message_kind="interrupt_answer",
        question_id="q_1",
    )
    rt.conversations.append_user_message(
        session["user_id"],
        created.conversation_id,
        idempotency_key="k2",
        expected_hash="h2",
        content="新的补充",
        message_kind="interrupt_answer",
        question_id="q_1",
    )

    rows = read_ledger(
        rt.database, created.conversation_id, after_sequence=0, user_id=session["user_id"]
    )

    assert [row.content for row in rows] == ["新的补充"]
