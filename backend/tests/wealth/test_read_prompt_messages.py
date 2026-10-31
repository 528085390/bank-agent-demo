"""提示词消息结构：工具事实必须放在 user 消息里。

回放实验发现，把工具事实放在 system 提示里时，多个模型会忽略事实并反复调用同一个
只读工具；把同一段事实放进 user 消息后，模型才会用它收尾。这个测试锁定消息结构，
避免以后为了“整洁”又把事实挪回 system。
"""

from app.modules.wealth.llm.prompts import read_messages


def test_tool_facts_go_into_user_message_not_system() -> None:
    system, user = read_messages(
        task="我活期账户里还有多少钱？",
        observations=[
            {
                "tool": "wealth_accounts",
                "status": "ok",
                "facts": {"accounts": [{"account_id": "a_demo", "balance_minor": 1500000}]},
            }
        ],
    )

    assert "已经取得的工具事实" not in system["content"]
    assert "已经取得的工具事实（唯一可信的数据来源）：" in user["content"]
    assert "wealth_accounts" in user["content"]
    assert "我活期账户里还有多少钱？" in user["content"]
