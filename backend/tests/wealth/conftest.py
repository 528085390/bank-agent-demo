"""离线理财测试的公共夹具：这个目录里只有 HTTP 传输替身，没有真实银行。

`tests/wealth_integration/` 才允许真实网络；这里禁掉所有出站连接，让“不小心真的连了银行”
变成一条明确的失败，而不是一个依赖环境的偶然后果。
"""

from __future__ import annotations

import socket

import pytest

from app.modules.wealth.infrastructure.bank_adapter import HttpWealthBank
from app.tools.protocol import TrustedContext
from tests.wealth import support

# 回环地址要放行：事件循环自己会用一对回环套接字做唤醒管道（Windows 上 socketpair 就是连
# 127.0.0.1），把它一起拦住，整条异步路径都跑不起来。银行替身不在这几个地址上。
LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})


@pytest.fixture(autouse=True)
def no_outbound_network(monkeypatch):
    """任何真实出站连接都是测试写错了：离线用例只走 httpx.MockTransport。"""

    def guard(original):
        def blocked(sock, address, *args, **kwargs):
            host = address[0] if isinstance(address, tuple) and address else address
            if host in LOOPBACK:
                return original(sock, address, *args, **kwargs)
            raise AssertionError("离线理财测试不得建立网络连接，请使用 support.BankDouble")

        return blocked

    monkeypatch.setattr(socket.socket, "connect", guard(socket.socket.connect))
    monkeypatch.setattr(socket.socket, "connect_ex", guard(socket.socket.connect_ex))


@pytest.fixture
def double() -> support.BankDouble:
    return support.BankDouble()


@pytest.fixture
def bank(double: support.BankDouble) -> HttpWealthBank:
    return HttpWealthBank(double.client())


@pytest.fixture
def context() -> TrustedContext:
    return support.bound_context()


@pytest.fixture
def clock() -> support.TestClock:
    return support.TestClock()
