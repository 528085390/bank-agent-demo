"""账单任务流水线的配置项。

独立于 ``app.core.config.Settings``：账单相关配置不进基底设置类，只在这里集中定义，
避免为账单功能改动共享配置。默认值即可让克隆后开箱即用，覆盖方式与主配置一致
（读 backend/.env）。
"""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

#: 仓库根下的临时目录，与附件处理器沙箱（.tmp/billing）并列，均在 .gitignore 内。
BILL_UPLOAD_DIR = Path(__file__).resolve().parents[3] / ".tmp" / "bill_uploads"


class BillSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    bill_upload_dir: Path = BILL_UPLOAD_DIR
    bill_max_file_size_mb: int = 50
    bill_password_ttl_seconds: int = 600
    bill_max_password_attempts: int = 3


@lru_cache
def get_bill_settings() -> BillSettings:
    return BillSettings()
