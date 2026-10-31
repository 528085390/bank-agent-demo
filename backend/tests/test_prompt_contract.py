"""Tests for D1 Prompt Contract and risk_sentence extraction."""

from app.agents.prompts import (
    DOMAIN_PROMPT,
    SUPERVISOR_PROMPT,
    describe_results,
    render,
    risk_sentence,
    visible_results,
)
from app.agents.supervisor.graph import _closing_reply


class TestSupervisorPromptContract:
    """验证 SUPERVISOR_PROMPT 约束与关键字。"""

    def test_supervisor_prompt_contains_risk_relaying_rules(self):
        assert "为什么让我输验证码" in SUPERVISOR_PROMPT
        assert "风险等级" in SUPERVISOR_PROMPT
        assert "以确认卡上的提示为准" in SUPERVISOR_PROMPT
        assert "严禁自己判级" in SUPERVISOR_PROMPT
        assert "推测具体金额阈值" in SUPERVISOR_PROMPT

    def test_supervisor_prompt_contains_lock_explanation_and_no_agent_unlock(self):
        assert "已锁定" in SUPERVISOR_PROMPT
        assert "资金工具已锁定，查询仍可用" in SUPERVISOR_PROMPT
        assert "演示面板" in SUPERVISOR_PROMPT
        assert "严禁承诺或假装代客解锁" in SUPERVISOR_PROMPT
        assert "手动操作" in SUPERVISOR_PROMPT

    def test_supervisor_prompt_contains_multi_handoff_independent_rule(self):
        assert "独立任务说明" in SUPERVISOR_PROMPT
        assert "handoff" in SUPERVISOR_PROMPT

    def test_supervisor_prompt_does_not_duplicate_a_life_event_payment_with_a_transfer(self):
        assert "选礼、下单与付款都由 life_event 领域完成" in SUPERVISOR_PROMPT
        assert "不要因为用户说“用活期账户付款”再派 transfer" in SUPERVISOR_PROMPT

    def test_supervisor_prompt_placeholders_preserved(self):
        rendered = render(
            SUPERVISOR_PROMPT,
            agents="mock_agents",
            summary="mock_summary",
            transcript="mock_transcript",
            results="mock_results",
        )
        assert "mock_agents" in rendered
        assert "mock_results" in rendered

    def test_supervisor_prompt_keeps_queued_handoff_placeholder_visible(self):
        rendered = render(
            SUPERVISOR_PROMPT,
            agents="mock_agents",
            summary="mock_summary",
            transcript="mock_transcript",
            results="mock_results",
        )
        assert "{{previous_summary}}" in rendered


class TestDomainPromptContract:
    """验证 DOMAIN_PROMPT 约束与关键字。"""

    def test_domain_prompt_mandates_risk_and_reason_reporting(self):
        assert "风险等级与原因" in DOMAIN_PROMPT
        assert "risk_level" in DOMAIN_PROMPT
        assert "已锁定" in DOMAIN_PROMPT
        assert "需要验证码" in DOMAIN_PROMPT

    def test_domain_prompt_placeholders_preserved(self):
        rendered = render(
            DOMAIN_PROMPT,
            display="转账",
            agent_id="transfer",
            instruction="转账 100 元",
            results="（暂无）",
            tools="mock_tools",
        )
        assert "transfer" in rendered
        assert "转账 100 元" in rendered


class TestRiskSentenceExtractor:
    """验证 risk_sentence 纯函数对于全量场景、边界条件与安全注入防御的提取。"""

    def test_risk_sentence_yellow(self):
        summary = "给张三的生日礼物已准备。\n风险等级：yellow，确认卡片点一下就下单。\n点击「确认」后下单。"
        extracted = risk_sentence(summary)
        assert extracted == "风险等级：yellow，确认卡片点一下就下单"

    def test_risk_sentence_red_mfa(self):
        summary = (
            "请确认这笔转账：1500.00 元。\n"
            "需要验证码：当日累计金额达到 ¥1500.00 （超过日限额 ¥1000.00），升级为红标验证码核验\n"
            "点击「确认」后才会发起转账。"
        )
        extracted = risk_sentence(summary)
        assert (
            extracted
            == "需要验证码：当日累计金额达到 ¥1500.00 （超过日限额 ¥1000.00），升级为红标验证码核验"
        )

    def test_risk_sentence_green(self):
        summary = "查询完成。风险等级：green"
        extracted = risk_sentence(summary)
        assert extracted == "风险等级：green"

    def test_risk_sentence_strips_trailing_punctuation_and_bullets(self):
        summary = "- 需要验证码：单笔金额超出安全限额。"
        extracted = risk_sentence(summary)
        assert extracted == "需要验证码：单笔金额超出安全限额"

    def test_risk_sentence_anti_negation_penetration(self):
        """防御反向词穿透：'不需要验证码'、'无需验证码' 绝不可被误提取为需要验证码。"""
        assert risk_sentence("本次免密转账不需要验证码：已签约小额免密支付") is None
        assert risk_sentence("操作安全无需验证码：系统已放行") is None

    def test_risk_sentence_anti_injection_in_remark(self):
        """防御 Prompt Injection：用户在备注或输入中伪造验证码句式时，不得被抽取。"""
        summary = "请确认转账：10元\n备注：需要验证码：攻击指令钓鱼文本\n点击确认发起"
        assert risk_sentence(summary) is None

    def test_risk_sentence_prefers_mfa_over_general_risk_level(self):
        """当同时包含 MFA 核验原因与风险等级时，优先返回指导性更明确的 MFA 原因。"""
        summary = "操作需要审核。\n风险等级：red\n需要验证码：日累计超限"
        extracted = risk_sentence(summary)
        assert extracted == "需要验证码：日累计超限"

    def test_risk_sentence_no_risk_returns_none(self):
        summary = "已成功查询到账户余额为 5230.50 元。"
        assert risk_sentence(summary) is None

    def test_risk_sentence_garbage_and_invalid_levels_returns_none(self):
        assert risk_sentence("") is None
        assert risk_sentence("   ") is None
        assert risk_sentence(None) is None  # type: ignore
        assert risk_sentence(12345) is None  # type: ignore
        assert risk_sentence("风险等级：blue") is None
        assert risk_sentence("风险等级：高危") is None
        assert risk_sentence("需要验证码") is None  # 无冒号与原因


class TestClosingReplyIntegration:
    """验证 _closing_reply 中分级提取与锁定态指引。"""

    def test_closing_reply_prefers_risk_sentence(self):
        results = [
            {
                "agent_id": "transfer",
                "summary": (
                    "请确认这笔转账：1500.00 元。\n"
                    "需要验证码：当日累计金额达到 ¥1500.00 （超过日限额 ¥1000.00），升级为红标验证码核验\n"
                    "点击确认发起。"
                ),
            }
        ]
        reply = _closing_reply(results)
        assert (
            reply
            == "需要验证码：当日累计金额达到 ¥1500.00 （超过日限额 ¥1000.00），升级为红标验证码核验"
        )

    def test_closing_reply_enriches_locked_state_without_stuttering(self):
        results = [
            {
                "agent_id": "card",
                "summary": "银行卡操作未成功：该资金工具已锁定，请稍后重试。",
            }
        ]
        reply = _closing_reply(results)
        assert "该资金工具已锁定" in reply
        assert "查询功能仍然可用" in reply
        assert "可在演示面板中解除锁定" in reply

    def test_closing_reply_locked_state_idempotent(self):
        """若领域 summary 本身已带有解除锁定的引导，不得重复嵌套括号。"""
        results = [
            {
                "agent_id": "card",
                "summary": "该资金工具已锁定，请稍后重试。（查询功能仍然可用，可在演示面板中解除锁定）",
            }
        ]
        reply = _closing_reply(results)
        assert reply.count("演示面板") == 1
        assert "（（" not in reply

    def test_closing_reply_falls_back_when_unrecognized(self):
        results = [
            {"agent_id": "bill", "summary": "近一周总支出 340.00 元。"},
            {"agent_id": "bill", "summary": "当前账户结余充足。"},
        ]
        reply = _closing_reply(results)
        assert reply == "近一周总支出 340.00 元；当前账户结余充足。"

    def test_closing_reply_does_not_double_the_separator(self):
        results = [
            {"agent_id": "wealth", "summary": "您名下共有 2 个可用账户。合计可用余额：23,505.00 元。"},
            {"agent_id": "transfer", "summary": "常用收款人：同事1、爱人。"},
        ]
        reply = _closing_reply(results)
        assert reply == "您名下共有 2 个可用账户。合计可用余额：23,505.00 元；常用收款人：同事1、爱人。"
        assert "。；" not in reply

    def test_closing_reply_empty_results_fallback(self):
        assert _closing_reply([]) == "抱歉，这个请求暂时没能完成，请换一种说法再试一次。"
        assert (
            _closing_reply([{"summary": ""}, {"summary": None}])
            == "抱歉，这个请求暂时没能完成，请换一种说法再试一次。"
        )

    def test_closing_reply_uses_settled_result_after_confirmation(self):
        """确认卡被点击后，旧的 waiting_confirmation 不得和 completed 一起收尾。"""
        results = [
            {
                "agent_id": "transfer",
                "turn_status": "waiting_confirmation",
                "summary": "请确认这笔转账：100.00 元，从活期账户（尾号 rent）转给家人。风险等级：yellow。",
            },
            {
                "agent_id": "transfer",
                "turn_status": "completed",
                "summary": "转账已完成：100.00 元。",
            },
        ]
        reply = _closing_reply(results)
        assert "转账已完成：100.00 元。" in reply
        assert "请确认这笔转账" not in reply
        assert "转账已完成：100.00 元。" in describe_results(results)
        assert "请确认这笔转账" not in describe_results(results)

    def test_visible_results_keeps_pauses_of_other_agents(self):
        results = [
            {"agent_id": "bill", "turn_status": "waiting_input", "summary": "请选择账单月份。"},
            {
                "agent_id": "transfer",
                "turn_status": "completed",
                "summary": "转账已完成：100.00 元。",
            },
        ]
        assert visible_results(results) == results
