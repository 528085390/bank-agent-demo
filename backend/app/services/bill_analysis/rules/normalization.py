"""对手名称归一化规则。"""

import re


def normalize_counterparty(name: str) -> str:
    """归一化对手名称。

    规则：
    1. 去除空格
    2. 去除括号内容
    3. 去除常见后缀
    4. 支付宝/微信特殊处理

    Args:
        name: 原始对手名称

    Returns:
        归一化后的名称
    """
    if not name:
        return ""

    # 1. 去除空格
    name = name.strip()

    # 2. 去除括号内容
    name = re.sub(r"\([^)]*\)", "", name)
    name = re.sub(r"（[^）]*）", "", name)

    # 3. 去除常见后缀
    suffixes = [
        "有限公司",
        "股份有限公司",
        "有限责任公司",
        "分公司",
        "支行",
        "分行",
        "支店",
        "股份公司",
        "集团",
    ]
    for suffix in suffixes:
        if name.endswith(suffix):
            name = name[: -len(suffix)]

    # 4. 支付宝/微信特殊处理
    if name.startswith("支付宝-"):
        name = name.replace("支付宝-", "")
    elif name.startswith("支付宝（"):
        name = name.replace("支付宝（", "").replace("）", "")

    if name.startswith("微信-"):
        name = name.replace("微信-", "")
    elif name.startswith("微信（"):
        name = name.replace("微信（", "").replace("）", "")

    # 5. 去除多余空格
    name = re.sub(r"\s+", "", name)

    return name.strip()


def extract_merchant_name(summary: str, counterparty: str) -> str:
    """从摘要和对手中提取真实商户名。

    某些情况下，真实商户名在摘要中，对手只是平台名。
    例如：
    - 摘要: "支付宝-星巴克消费"
    - 对手: "支付宝"
    - 提取: "星巴克"

    Args:
        summary: 交易摘要
        counterparty: 对手名称

    Returns:
        提取的商户名，若无法提取则返回原对手名
    """
    # 规则1: 支付宝-XXX 格式
    if "支付宝-" in summary:
        parts = summary.split("支付宝-", 1)
        if len(parts) > 1:
            merchant = parts[1].split("消费")[0].split("付款")[0].strip()
            if merchant:
                return merchant

    # 规则2: 微信-XXX 格式
    if "微信-" in summary:
        parts = summary.split("微信-", 1)
        if len(parts) > 1:
            merchant = parts[1].split("消费")[0].split("付款")[0].strip()
            if merchant:
                return merchant

    # 规则3: XXX消费 格式
    if "消费" in summary:
        merchant = summary.split("消费")[0].strip()
        if merchant and not any(
            kw in merchant for kw in ["支付宝", "微信", "POS", "快捷"]
        ):
            return merchant

    # 无法提取，返回原对手名
    return counterparty
