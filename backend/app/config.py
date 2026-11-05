"""只从独立环境配置读取连接和凭据，绝不复用预算程序的账号。"""
from pathlib import Path
from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="LR_", env_file=str(PROJECT_ROOT / ".env"), extra="ignore")
    environment: str = "development"
    adapter_mode: str = "demo"
    database_url: str = "sqlite:///runtime/loss-repricing.db"
    runtime_dir: Path = PROJECT_ROOT / "runtime"
    admin_username: str = "admin"
    admin_password: str = ""
    session_secret: str = ""
    secure_cookie: bool = False
    live_submit_enabled: bool = False
    live_contract_path: Path = PROJECT_ROOT / "deploy" / "live-contract.json"
    snapshot_path: Path = PROJECT_ROOT / "runtime" / "snapshot.json"
    vendor_token: str = ""
    erp_token: str = ""
    worker_poll_seconds: int = 5

    @model_validator(mode="after")
    def production_settings(self):
        if self.adapter_mode not in {"demo", "file", "live"}:
            raise ValueError("LR_ADAPTER_MODE必须为demo/file/live")
        if self.environment == "production":
            if len(self.admin_password) < 14 or len(self.session_secret) < 32:
                raise ValueError("生产环境需配置至少14位管理员密码及32位独立会话密钥")
            if not self.secure_cookie:
                raise ValueError("生产环境必须使用HTTPS secure cookie")
            if not self.database_url.startswith("mysql+"):
                raise ValueError("生产环境必须使用独立MySQL数据库")
        return self


def get_settings() -> Settings:
    return Settings()
