from functools import lru_cache

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.database_file import sqlalchemy_url

# 本子项目自己的主库，属主角色 bank。
BANK_DATABASE = "bank_db"
# 集成测试库：与主项目共用云实例上的 test 库。测试会清空并重建业务表，
# 所以绝不能指向 bank_db。
TEST_DATABASE = "test"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)

    # 默认取仓库根目录已提交的 database.txt，克隆后无需任何配置即可运行。
    # 只有要指向别处时才需要设置 DATABASE_URL。
    database_url: str = Field(default_factory=lambda: sqlalchemy_url(BANK_DATABASE))
    # 集成测试库。默认同样是共享凭据，因此测试开箱即可运行。
    test_database_url: str | None = Field(default_factory=lambda: sqlalchemy_url(TEST_DATABASE))
    # 本 mock 不做用户登录：调用方必须明确说出它以谁的身份操作，服务端再校验资源归属。
    # 这里没有默认客户——上一版缺省会回落 u_zhang，任何能连到 8001 的进程都能读到演示
    # 客户的余额；现在缺身份直接拒绝。保留这个字段只是为了给运维脚本/测试一个显式的
    # 名字，路由不会再用它兜底。
    default_user_id: str = "u_zhang"
    seed_version: str = "1.2"
    # 服务间凭证：只有持有它的进程（app 后端）能调客户级接口。空值表示拒绝一切调用
    # ——克隆下来不配 .env 的默认状态，宁可用不了也不要裸奔。
    service_token: str = ""
    # 运维口令：清库、推时钟、跑批这几个全局动作的额外一把钥匙。和服务凭证分开，是为了
    # 让 app 进程即使被利用也清不掉全库——它没有这把钥匙。
    ops_token: str = ""
    # 资金写操作的基础风控阈值（整数分），仅用于 mock 规则，不接入真实风控模型。
    large_amount_threshold_minor: int = 500000
    frequent_small_amount_minor: int = 10000
    # 演示用模拟 MFA：任意非空数字码均可通过校验。
    mock_mfa_code: str = "123456"
    demo_card_password: SecretStr = Field(default=SecretStr("246810"), repr=False)

    @field_validator("demo_card_password")
    @classmethod
    def validate_card_password(cls, value):
        from app.domain.card_password import valid_password

        if not valid_password(value.get_secret_value()):
            raise ValueError("DEMO_CARD_PASSWORD 必须为六位数字")
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
