from app.modules.card.tools import SELECTOR, SUFFIX_SELECTOR, matched_cards

CARDS = [
    {"card_number": "6222000000003878", "card_type": "virtual", "status": "active"},
    {"card_number": "6222000000005751", "card_type": "debit", "status": "active"},
]


def test_a_wrong_type_never_vetoes_a_unique_suffix():
    hits = matched_cards(CARDS, {"card_suffix": "3878", "card_type": "debit"})
    assert [card["card_type"] for card in hits] == ["virtual"]


def test_type_narrows_only_when_the_suffix_is_ambiguous():
    cards = CARDS + [{"card_number": "6222000000003878", "card_type": "debit", "status": "active"}]
    assert len(matched_cards(cards, {"card_suffix": "3878"})) == 2
    narrowed = matched_cards(cards, {"card_suffix": "3878", "card_type": "debit"})
    assert [card["card_type"] for card in narrowed] == ["debit"]


def test_an_unknown_suffix_is_still_empty():
    assert matched_cards(CARDS, {"card_suffix": "8801"}) == []


def test_only_the_selector_copy_carries_the_disambiguation_note():
    assert "消歧" in SUFFIX_SELECTOR["card_type"]["description"]
    assert "description" not in SELECTOR["card_type"]


def test_the_generated_schemas_carry_the_note_only_where_card_type_selects():
    """R-7 的钉子落在**生成后**的 schema 上，而不只是那个常量。"""

    from app.modules.card.tools import TOOLS
    from app.tools.schema import function_schema

    def card_type_of(name):
        return function_schema(TOOLS[name])["function"]["parameters"]["properties"]["card_type"]

    for name in (
        "card_detail",
        "card_restrictions",
        "card_update_limit",
        "card_update_restrictions",
        "card_freeze",
        "card_unfreeze",
        "card_report_loss",
        "card_release_loss",
    ):
        assert "消歧" in card_type_of(name)["description"], name
    # card_apply 的 card_type 是「要开的卡类型」，不是选择器：它的文字不得被改。
    assert "description" not in card_type_of("card_apply")
