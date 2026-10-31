"""邮件发送功能独立测试脚本。

运行方式（在仓库根目录）：
    python backend/tests/test_email_send.py

SMTP 凭据从 .env / 环境变量读取，未配置时仅验证配置加载与模板渲染。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.notification.config import EmailConfig  # noqa: E402
from app.notification.email_service import EmailService  # noqa: E402
from app.notification.templates import render_bill_summary  # noqa: E402


def _load_env() -> None:
    """加载 backend/.env 与仓库根 .env（pydantic-settings 已间接依赖 python-dotenv）。"""
    candidates = [
        Path(__file__).resolve().parent.parent / ".env",  # backend/.env
        Path(__file__).resolve().parents[2] / ".env",  # 仓库根 .env
    ]
    for env_file in candidates:
        if not env_file.exists():
            continue
        try:
            from dotenv import load_dotenv

            load_dotenv(env_file)
        except Exception:
            pass


def test_config() -> bool:
    print("=" * 60)
    print("测试1: 配置加载")
    print("=" * 60)
    try:
        config = EmailConfig.from_env()
        print(f"✅ SMTP Host: {config.smtp_host}")
        print(f"✅ SMTP Port: {config.smtp_port}")
        print(f"✅ SMTP User: {config.smtp_user or '(未配置)'}")
        print(f"✅ Use SSL: {config.smtp_use_ssl}")
        print(f"✅ From Name: {config.from_name}")
        if not config.validate_config():
            print("⚠️  SMTP 未配置（缺少 SMTP_USER / SMTP_PASSWORD），邮件发送将被跳过\n")
            return True
        print("✅ 配置验证通过\n")
        return True
    except Exception as e:
        print(f"❌ 配置加载失败: {str(e)}\n")
        return False


def test_template_render() -> bool:
    print("=" * 60)
    print("测试2: 模板渲染")
    print("=" * 60)
    try:
        context = {
            "period_start": "2026-01-01",
            "period_end": "2026-01-31",
            "credit_total": "5000.00",
            "debit_total": "3210.55",
            "net_total": "1789.45",
            "record_count": 42,
            "categories": [
                {"category": "餐饮", "amount": "820.30", "count": 11},
                {"category": "交通", "amount": "450.00", "count": 8},
                {"category": "购物", "amount": "1200.25", "count": 15},
            ],
            "quality_issues": [],
        }
        html = render_bill_summary(context)
        print(f"✅ 模板渲染成功，长度: {len(html)} 字符")
        print(f"✅ 包含关键词: {'总收入' in html and '总支出' in html}\n")
        return True
    except Exception as e:
        print(f"❌ 模板渲染失败: {str(e)}\n")
        return False


def test_send_email() -> bool:
    print("=" * 60)
    print("测试3: 邮件发送")
    print("=" * 60)
    config = EmailConfig.from_env()
    if not config.validate_config():
        print("⚠️  未配置 SMTP，跳过真实发送测试。请设置 SMTP_USER / SMTP_PASSWORD 后重试。\n")
        return True

    test_email = input("请输入接收测试邮件的邮箱地址: ").strip()
    if not test_email:
        print("❌ 未输入测试邮箱，跳过邮件发送测试\n")
        return False

    try:
        email_service = EmailService(config)
        context = {
            "period_start": "2026-01-01",
            "period_end": "2026-01-31",
            "credit_total": "5000.00",
            "debit_total": "3210.55",
            "net_total": "1789.45",
            "record_count": 42,
            "categories": [
                {"category": "餐饮", "amount": "820.30", "count": 11},
                {"category": "交通", "amount": "450.00", "count": 8},
                {"category": "购物", "amount": "1200.25", "count": 15},
                {"category": "娱乐", "amount": "340.00", "count": 5},
                {"category": "未分类", "amount": "400.00", "count": 3},
            ],
            "quality_issues": [],
        }
        html_content = render_bill_summary(context)
        print(f"正在发送测试邮件到: {test_email}")
        success = email_service.send_email(
            to_email=test_email,
            subject="【测试】账单分析摘要 - 2026年1月",
            html_content=html_content,
        )
        if success:
            print(f"✅ 邮件发送成功！请检查邮箱: {test_email}")
            print("   提示: 如果未收到，请检查垃圾邮件箱\n")
            return True
        print("❌ 邮件发送失败\n")
        return False
    except Exception as e:
        print(f"❌ 邮件发送测试失败: {str(e)}\n")
        return False


def main() -> bool:
    _load_env()
    print("\n" + "=" * 60)
    print("账单分析邮件发送功能测试")
    print("=" * 60 + "\n")

    results = [
        ("配置加载", test_config()),
        ("模板渲染", test_template_render()),
        ("邮件发送", test_send_email()),
    ]

    print("=" * 60)
    print("测试结果汇总")
    print("=" * 60)
    for name, result in results:
        print(f"{name}: {'✅ 通过' if result else '❌ 失败'}")
    passed = sum(1 for _, r in results if r)
    total = len(results)
    print(f"\n总计: {passed}/{total} 项测试通过")
    print("=" * 60 + "\n")
    return passed == total


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
