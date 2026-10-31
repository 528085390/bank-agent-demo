"""A question's fields carry what the client needs to render and validate them."""

from app.agents.declaration import DomainDeclaration, FieldMeta, resolve_fields
from app.agents.state import PendingField, PendingQuestion


def test_a_pending_field_carries_its_label_and_type():
    field: PendingField = {
        "name": "amount_minor",
        "label": "转账金额（元）",
        "type": "number",
        "required": True,
        "options": [],
    }

    assert field["label"] == "转账金额（元）"


def test_a_question_is_a_list_of_fields_not_a_list_of_strings():
    question: PendingQuestion = {
        "kind": "input",
        "question_id": "q_1",
        "agent_id": "transfer",
        "fields": [
            {"name": "payee", "label": "收款人", "type": "text", "required": True, "options": []}
        ],
        "display": "请补充收款人",
        "attempt": 1,
    }

    assert question["fields"][0]["name"] == "payee"


def declaration_with(**fields) -> DomainDeclaration:
    return DomainDeclaration(
        agent_id="transfer", display="转账 Agent", purpose="测试", fields=fields
    )


def test_a_declared_field_carries_its_label_type_and_requirement():
    declaration = declaration_with(
        amount=FieldMeta(label="转账金额（元）", type="number"),
        account_type=FieldMeta(
            label="账户类型", type="select", options=("活期", "定期"), required=False
        ),
    )

    resolved = resolve_fields(declaration, ["amount", "account_type"])

    assert resolved == [
        {
            "name": "amount",
            "label": "转账金额（元）",
            "type": "number",
            "required": True,
            "options": [],
        },
        {
            "name": "account_type",
            "label": "账户类型",
            "type": "select",
            "required": False,
            "options": ["活期", "定期"],
        },
    ]


def test_an_undeclared_field_falls_back_to_its_own_name():
    resolved = resolve_fields(declaration_with(), ["mystery"])

    assert resolved == [
        {"name": "mystery", "label": "mystery", "type": "text", "required": True, "options": []}
    ]


def test_the_order_the_model_asked_in_is_preserved():
    declaration = declaration_with(b=FieldMeta(label="乙"), a=FieldMeta(label="甲"))

    assert [field["name"] for field in resolve_fields(declaration, ["b", "a"])] == ["b", "a"]


def test_a_blank_field_name_is_dropped_rather_than_rendered():
    assert resolve_fields(declaration_with(), ["  ", "ok"]) == [
        {"name": "ok", "label": "ok", "type": "text", "required": True, "options": []}
    ]
