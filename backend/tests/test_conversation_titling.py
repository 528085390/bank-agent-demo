"""A conversation's name is the user's own first words, derived locally.

It used to be a model call. These tests pin what replaced it: the message itself, made one line
and never reworded, cut only when it would not fit in the row at all -- and never the default
title written back as if it were a name. The reason the model call is gone is also a contract
worth keeping visible: a label must not depend on a provider being reachable, must not be able to
fail a turn, and must not need an exemption from the audit rule.
"""

from __future__ import annotations

from app.conversation.service import DEFAULT_TITLE, MAX_TITLE_LENGTH
from app.conversation.titling import derive_title, is_usable_title


def test_a_short_first_message_is_the_title_unchanged():
    assert derive_title("我要转账给张三") == "我要转账给张三"


def test_the_title_is_one_line_even_when_the_message_is_not():
    assert derive_title("帮我\n看看\t这张账单") == "帮我 看看 这张账单"


def test_leading_and_trailing_whitespace_is_dropped():
    assert derive_title("   查一下余额   ") == "查一下余额"


def test_a_long_first_message_is_kept_whole():
    """The name is the sentence, not a shorter version of it."""

    text = "我想知道我这个月到底花在了什么地方，能不能按类别给我排个序"

    assert derive_title(text) == text


def test_a_comma_nowhere_near_the_limit_does_not_shorten_the_name():
    text = "你好，我想问一下我这个月的信用卡账单在哪里能看到明细，最好能按类别分开"

    assert derive_title(text) == text


def test_a_message_that_fits_the_row_is_not_cut():
    text = "好" * MAX_TITLE_LENGTH

    assert derive_title(text) == text


def test_only_the_row_limit_cuts_a_message():
    """Beyond what the row holds there is no choice; the ellipsis is the only thing to add."""

    text = "好" * 500
    title = derive_title(text)

    assert title is not None
    assert len(title) == MAX_TITLE_LENGTH
    assert title.endswith("…")
    assert text.startswith(title[:-1])


def test_nothing_to_name_is_not_a_name():
    assert derive_title(None) is None
    assert derive_title("") is None
    assert derive_title("   \n\t ") is None


def test_the_default_title_is_not_written_back():
    """`apply_auto_title` treats the default as "nothing was named", so it must not be produced."""

    assert is_usable_title(DEFAULT_TITLE) is False
    assert is_usable_title(None) is False
    assert is_usable_title("") is False
    assert is_usable_title("我要转账") is True
