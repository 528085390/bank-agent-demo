"""邮件通知层：内存附件、通用发信与邮箱解绑。

字节附件必须能活过队列的 JSON 序列化往返 —— 队列载荷是 JSON，裸 `bytes` 会在
`json.dumps` 处直接炸掉，所以发送前要编码成文本、消费时再解回来。
"""

from fastapi.testclient import TestClient

from app.main import create_app
from app.notification.attachments import Attachment


def client_for(rt, session):
    return TestClient(
        create_app(rt.settings, runtime=rt), headers={"Authorization": "Bearer " + session["token"]}
    )


def test_a_bytes_attachment_survives_the_queue_round_trip():
    from app.notification.service import _deserialize_attachments, _serialize_attachments

    original = Attachment("a.pdf", b"%PDF-1.4")

    payload = _serialize_attachments([original])

    assert payload == [{"filename": "a.pdf", "content": "JVBERi0xLjQ="}]
    assert _deserialize_attachments(payload) == [original]


def test_send_email_reports_failed_when_smtp_is_not_configured(rt):
    # 仓库现在带着一份共享 SMTP 配置（backend/.env 已入库，为的是克隆后能直接发信），
    # 所以"未配置"这条分支不能靠环境碰运气：显式构造一个 SMTP 全空的 Settings。
    from app.notification.service import NotificationService

    unconfigured = rt.settings.model_copy(
        update={"smtp_host": "", "smtp_user": "", "smtp_password": ""}
    )
    service = NotificationService(rt.database, unconfigured)
    result = service.send_email(recipient="a@b.com", subject="s", html="h")

    assert result["status"] == "FAILED"
    assert result["error"] == "SMTP 未配置"


def test_the_email_binding_can_be_cleared(rt, session):
    rt.notification.set_user_email(session["user_id"], "a@b.com")

    rt.notification.clear_user_email(session["user_id"])

    assert rt.notification.get_user_email(session["user_id"]) is None


def test_the_email_endpoints_report_and_clear_the_binding(rt, session):
    client = client_for(rt, session)

    assert client.get("/api/settings/email").json()["data"]["email"] is None

    bound = client.put("/api/settings/email", json={"email": "a@b.com"})
    assert bound.status_code == 200
    assert client.get("/api/settings/email").json()["data"]["email"] == "a@b.com"

    cleared = client.delete("/api/settings/email")
    assert cleared.status_code == 200
    assert cleared.json()["data"]["email"] is None
    assert client.get("/api/settings/email").json()["data"]["email"] is None


def test_a_notification_accepts_the_long_analysis_task_id(rt):
    """真实账号的 user_id 有 37 字符，分析报告的通知关联号约 70 字符。

    迁移 0021 之前这一行写不进去（varchar(64)），邮件因此发不出去。
    """

    from app.core.types import utcnow
    from app.notification.service import NotificationService

    task_id = f"analysis_{'u' * 37}_2026-05-02 ~ 2026-05-29"
    assert len(task_id) > 64

    service = NotificationService(rt.database, rt.settings)
    notification_id = service._record(
        task_id,
        "someone@example.com",
        "REPORT",
        "subject",
        status="PENDING",
        now=utcnow(),
    )

    assert notification_id
