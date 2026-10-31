import logging
from functools import lru_cache
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.context.budget import Budget
from app.core.database_file import sqlalchemy_url

# Development and test share this database on the cloud instance. They can: the tests never
# touch its `public` schema, because each test creates a schema of its own.
DATABASE_NAME = "test"

logger = logging.getLogger(__name__)

#: 手机打不开的短链主机：飞书卡片上的核验链接只能在同一台机器上解析。
_LOCAL_MFA_HOSTS = frozenset({"127.0.0.1", "localhost", "0.0.0.0", "::1"})


class Settings(BaseSettings):
    # Three files, later wins, and every one of them is git-ignored (only `.env.example` is
    # committed). `.env` holds how this machine runs (SMTP account, the email queue switch);
    # `.env.local` holds this machine's credentials (the model key, the service token);
    # `.env.channels` is written by the Feishu scan-to-connect flow -- it carries APP_SECRET, so
    # it must never be committed nor edited by hand. It comes last on purpose: credentials that
    # were just scanned have to override whatever `.env` said before.
    # A missing file is not an error: pydantic-settings skips it.
    model_config = SettingsConfigDict(
        env_file=(".env", ".env.local", ".env.channels"), extra="ignore"
    )

    # Defaults to the committed `database.txt`, so a fresh clone runs without a `.env`.
    # Set `DATABASE_URL` only to point somewhere else (another database, another instance).
    database_url: str = Field(default_factory=lambda: sqlalchemy_url(DATABASE_NAME))
    demo_mode: bool = True
    cookie_secure: bool = False
    session_hours: int = 24
    # The credential-free demo entry (`POST /api/session`) is off by default. Turning it on is
    # a deliberate act for a single-user demo box, because an open entry hands every visitor
    # the same account and, with it, the same bank customer. `demo_mode` still governs the
    # clock control plane; the two are separate switches on purpose -- a demo that has real
    # accounts may still want the clock bar, and a demo with an open entry may still want it
    # hidden.
    allow_demo_login: bool = False
    # Idle timeout is measured from the last request; the absolute ceiling from login. Both
    # are enforced on every request, and neither can be extended by using the app.
    auth_session_idle_minutes: int = 30
    # Failures are counted per account and per client IP, in the database, because a
    # memory-only counter gives an attacker one budget per worker.
    auth_login_max_failures: int = 5
    auth_login_window_minutes: int = 15
    auth_login_lock_minutes: int = 15
    # Pre-provisioned bank customers (`u_pool_01`...) that a registration claims one of. The
    # count must match what `bank_backend/scripts/seed_customer_pool.py` created.
    bank_pool_size: int = 20
    # Shared secret for bank_backend's internal provisioning surface. Empty means the
    # endpoint refuses every call, which is the safe default for a checkout with no `.env`.
    bank_service_token: str = ""
    # bank_backend runs on 8001: its own README uses 8000, which is where this app runs.
    # The `/api` suffix belongs here, not in every module's path: bank_backend mounts its
    # whole router under `/api`, so modules write the resource path alone ("/accounts") and
    # the prefix lives in exactly one place.
    bank_backend_base_url: str = "http://127.0.0.1:8001/api"
    bank_backend_connect_timeout: float = 2.0
    bank_backend_read_timeout: float = 10.0
    # The model the settings panel would otherwise have to be told by hand, every restart.
    # When all three of base URL, key and model name are present the process starts already
    # configured; the panel still overrides them at runtime. An empty key leaves the runtime
    # unconfigured, exactly as before, so a checkout with no `.env` is unchanged. The key is
    # a secret and belongs only in the git-ignored `backend/.env`.
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""
    llm_response_format: Literal["json_schema", "json_object"] = "json_schema"
    llm_timeout_seconds: float = Field(default=30.0, gt=0, le=120)
    smtp_host: str = "smtp.qq.com"
    smtp_port: int = 465
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_use_ssl: bool = True
    smtp_from_name: str = "银行AI智能体"
    email_queue_enabled: bool = False
    email_queue_redis_url: str = "redis://127.0.0.1:6379/1"
    email_queue_name: str = "email_queue"
    email_queue_dlq: str = "email_dlq"
    email_queue_retry_queue: str = "email_retry_queue"
    email_queue_max_retries: int = 3
    email_queue_retry_delays: list[int] = [60, 300, 900]
    email_queue_poll_timeout: int = 5
    # The IM channel layer signs its own artifacts: the MFA short link a Feishu card points
    # at is an HMAC over the pending action, so it needs a key a client cannot mint. The demo
    # value is committed on purpose -- a checkout with no .env must still start -- and is meant
    # to be overridden wherever the bridge is actually exposed.
    secret_key: str = "bank-agent-secret-key-2026-demo-change-in-prod"
    # What the Feishu card's button targets. It is the address the *phone* resolves, not the
    # address this process listens on, which is why it is a setting and not a constant.
    channel_mfa_base_url: str = "http://127.0.0.1:8000"
    # The Feishu bridge is off unless a deployment says otherwise: feishu_enabled is what
    # starts the outbound WebSocket task, and the four credentials below are what the Lark
    # SDK authenticates with. An empty string means "not configured", never "use a default".
    feishu_enabled: bool = False
    feishu_app_id: str = ""
    feishu_app_secret: str = ""
    feishu_encrypt_key: str = ""
    feishu_verification_token: str = ""
    # Access control for the channel. feishu_allow_from is a comma-separated list of Feishu
    # open_ids, and the whitelist is enforced by default: an unconfigured list admits nobody
    # rather than everybody. A whitelisted sender with no binding yet is bound to
    # feishu_default_bound_user only when feishu_auto_bind is on -- that keeps the demo path
    # one message long, and turning it off leaves the explicit /bind flow to decide.
    feishu_allow_from: str = ""
    feishu_whitelist_enabled: bool = True
    feishu_default_bound_user: str = "user_0001"
    feishu_auto_bind: bool = True
    # There is no planner mode: the model is the only natural-language planner, and an
    # unconfigured runtime reports MODEL_NOT_CONFIGURED instead of running rule scripts.
    # There is no scheduler, no challenge lifetime and no failure lockout either: those
    # belong to the deferred governance extension modules.
    #
    # The prompt-layer budget. These six values only ever interact through the derived
    # numbers in `app.context.budget`, and a combination that contradicts itself cannot be
    # repaired at run time -- the view would summarize on every decision and never shrink --
    # so the check below refuses it at construction, which is process start.
    model_context_limit: int = Field(default=128000, gt=0)
    reserved_output_tokens: int = Field(default=4096, ge=0)
    safety_margin: int = Field(default=2048, ge=0)
    summary_trigger_ratio: float = Field(default=0.7, gt=0, lt=1)
    recent_message_budget: int = Field(default=32768, gt=0)
    summary_max_tokens: int = Field(default=1024, gt=0)

    @model_validator(mode="after")
    def validate_context_budget(self) -> "Settings":
        """Fail at startup when the budget contradicts itself, not on a long conversation.

        `Budget.validate` raises `ValueError`, which pydantic turns into a `ValidationError`
        naming this model.
        """

        Budget.from_settings(self).validate()
        return self


def warn_if_mfa_base_url_is_local(settings: Settings) -> bool:
    """飞书开着、而 MFA 短链基址还指着本机时记一条 warning；返回是否告了警（便于测试）。

    channel_mfa_base_url 要填**手机**能解析的地址：进程监听在 127.0.0.1 只说明本机能访问，
    用户拿手机点卡片上那条链接时，127.0.0.1 指的是手机自己。默认值就是本机地址（一个没有
    .env 的检出必须能起来），所以这条提醒只能落在配置这一层，而不是把它做成启动失败。
    """

    if not getattr(settings, "feishu_enabled", False):
        return False
    base = str(getattr(settings, "channel_mfa_base_url", "") or "").strip()
    host = (urlsplit(base).hostname or "").lower()
    if host not in _LOCAL_MFA_HOSTS:
        return False
    logger.warning(
        "channel_mfa_base_url_is_local feishu_enabled 为真，但 channel_mfa_base_url=%s 指向本机："
        "飞书卡片上的核验链接在手机上打不开。请改成手机能访问的地址（内网 IP 或域名）。",
        base,
    )
    return True


@lru_cache
def get_settings() -> Settings:
    return Settings()
