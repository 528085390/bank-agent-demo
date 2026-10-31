"""A malformed NO_PROXY must not take the model boundary down with it.

The Windows harness this project runs under carries
NO_PROXY=localhost,127.0.0.1,::1,[::1]. httpx builds one URL pattern per entry, and the
bracketed literal raises InvalidURL: Invalid port *while constructing the transport*, so
every httpx client in the process fails to build and the settings panel reports a bogus
"cannot reach the provider".
"""

from __future__ import annotations

import os

from app.core.proxy_env import sanitize_no_proxy


def test_a_bracketed_ipv6_entry_is_dropped_and_the_rest_survive():
    environ = {"NO_PROXY": "localhost,127.0.0.1,::1,[::1]"}

    dropped = sanitize_no_proxy(environ)

    assert dropped == ("[::1]",)
    assert environ["NO_PROXY"] == "localhost,127.0.0.1,::1"


def test_a_bracketed_ipv6_with_a_port_is_dropped_too():
    environ = {"no_proxy": "localhost,[::1]:8080"}

    dropped = sanitize_no_proxy(environ)

    assert dropped == ("[::1]:8080",)
    assert environ["no_proxy"] == "localhost"


def test_a_bare_ipv6_entry_is_left_alone():
    environ = {"NO_PROXY": "localhost,::1"}

    dropped = sanitize_no_proxy(environ)

    assert dropped == ()
    assert environ["NO_PROXY"] == "localhost,::1"


def test_an_ordinary_list_is_untouched():
    environ = {"NO_PROXY": "localhost,127.0.0.1,.internal,192.168.*"}

    assert sanitize_no_proxy(environ) == ()

    assert environ["NO_PROXY"] == "localhost,127.0.0.1,.internal,192.168.*"


def test_a_list_of_nothing_but_bad_entries_becomes_empty_not_stale():
    environ = {"NO_PROXY": "[::1],[::2]"}

    dropped = sanitize_no_proxy(environ)

    assert dropped == ("[::1]", "[::2]")
    assert environ["NO_PROXY"] == ""


def test_both_spellings_are_cleaned_in_one_pass():
    environ = {"NO_PROXY": "[::1],localhost", "no_proxy": "[::1],localhost"}

    dropped = sanitize_no_proxy(environ)

    assert dropped == ("[::1]", "[::1]")
    assert environ["NO_PROXY"] == "localhost"
    assert environ["no_proxy"] == "localhost"


def test_an_absent_variable_is_not_created():
    environ: dict[str, str] = {}

    assert sanitize_no_proxy(environ) == ()

    assert "NO_PROXY" not in environ and "no_proxy" not in environ


def test_the_real_process_environment_is_what_gets_cleaned(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "localhost,[::1]")

    sanitize_no_proxy()

    assert os.environ["NO_PROXY"] == "localhost"
