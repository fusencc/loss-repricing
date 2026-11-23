"""只从独立环境配置读取连接和凭据，绝不复用预算程序的账号。"""
from pathlib import Path
from pydantic import Field, SecretStr, ValidationError, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="LR_", env_file=(str(PROJECT_ROOT.parent / ".env"), str(PROJECT_ROOT / ".env")),
        extra="ignore",
    )
    environment: str = "development"
    adapter_mode: str = "demo"
    database_url: str = "sqlite:///runtime/loss-repricing.db"
    # 只读真实执行/保护档案，可独立于当前demo/file写入模式绑定。
    live_history_database_url: SecretStr = Field(default_factory=lambda: SecretStr(""))
    runtime_dir: Path = PROJECT_ROOT / "runtime"
    admin_username: str = "admin"
    admin_password: str = ""
    session_secret: str = ""
    secure_cookie: bool = False
    live_submit_enabled: bool = False
    live_contract_path: Path = PROJECT_ROOT / "deploy" / "live-contract.json"
    snapshot_path: Path = PROJECT_ROOT / "runtime" / "snapshot.json"
    vendor_api_url: str = "http://121.46.237.218:8071"
    vendor_username: str = Field(default="", repr=False)
    vendor_password: SecretStr = Field(default_factory=lambda: SecretStr(""))
    erp_token: str = ""
    erp_open_api_url: str = "http://103.198.125.2:16777/open"
    erp_username: str = Field(default="", repr=False)
    erp_password: SecretStr = Field(default_factory=lambda: SecretStr(""))
    erp_capture_platform: str = "Tiktok"
    erp_capture_platform_id: str = "9B583641B0CA11EDAAB1AC1F6B9B9987"
    erp_capture_bloc: str = "集团二部"
    erp_capture_request_interval_seconds: float = Field(default=16, ge=0, le=300)
    # 网页后端暂无批量销量开放合同；串行读取并限制请求频率，后续改用批量/增量来源。
    sales_request_interval_seconds: float = Field(default=2, ge=0, le=30)
    # 日志只保存脱敏技术事件，独立于业务证据；DEBUG也不记录接口正文或凭据。
    log_level: str = Field(default="INFO", pattern=r"^(DEBUG|INFO|WARNING|ERROR|CRITICAL)$")
    log_max_bytes: int = Field(default=20 * 1024 * 1024, ge=4096, le=100 * 1024 * 1024)
    log_retention_days: int = Field(default=14, ge=1, le=90)
    log_total_bytes: int = Field(default=1024**3, ge=1024 * 1024, le=10 * 1024**3)
    worker_poll_seconds: int = 5
    coordination_required: bool = True
    live_writer_exclusive: bool = False
    ding_alert_userid: str = ""  # 管理员告警的独立机器人私聊目标，不借用销售员归属。
    ding_enabled: bool = False
    ding_corp_id: str = ""
    ding_client_id: str = ""
    ding_client_secret: str = ""
    ding_agent_id: str = ""
    public_base_url: str = ""
    notification_mode: str = "demo"

    @model_validator(mode="after")
    def production_settings(self):
        archive_url=self.live_history_database_url.get_secret_value().strip()
        if self.adapter_mode=='live' and archive_url and archive_url!=self.database_url:
            raise ValueError('真实运行数据库必须与已绑定的真实提价档案一致')
        if self.adapter_mode not in {"demo", "file", "live"}:
            raise ValueError("LR_ADAPTER_MODE必须为demo/file/live")
        if self.notification_mode not in {"demo", "live"}:
            raise ValueError("notification_mode必须为demo/live")
        if self.environment == "production":
            if not self.coordination_required:
                raise ValueError("生产环境必须开启协作权限")
            if len(self.admin_password) < 14 or len(self.session_secret) < 32:
                raise ValueError("生产环境需配置至少14位管理员密码及32位独立会话密钥")
            if not self.secure_cookie:
                raise ValueError("生产环境必须使用HTTPS secure cookie")
            if not self.database_url.startswith("mysql+"):
                raise ValueError("生产环境必须使用独立MySQL数据库")
        return self


def get_settings() -> Settings:
    try:
        return Settings()
    except ValidationError as exc:
        # 日志组件尚未初始化；Pydantic原始文案可能包含整份配置输入和凭据。
        names = sorted({str(part) for error in exc.errors(include_input=False, include_url=False)
                        for part in error['loc'] if part in Settings.model_fields})
        location = '、'.join(names[:10]) or '运行模式与安全约束'
        raise RuntimeError(f'项目配置加载失败，请检查：{location}；配置值未输出') from None
    except Exception:
        raise RuntimeError('项目配置加载失败，请检查独立LR_环境配置；配置值未输出') from None
