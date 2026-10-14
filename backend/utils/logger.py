import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from backend.config.settings import settings

_LOG_FORMAT = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'


def setup_logging():
    """配置日志：控制台 + （默认开启的）轮转文件日志。

    P-3：文件日志是运维排障的前提（此前 LOG_FILE 默认为 None，服务默认不留任何日志文件）。
    - 文件路径默认 `<项目根>/logs/app.log`（绝对路径，与启动时的 CWD 无关）；
    - 通过 `.env` 的 `LOG_FILE=`（置空）可关闭文件日志；
    - 采用 RotatingFileHandler 轮转（LOG_MAX_BYTES × LOG_BACKUP_COUNT），避免无限增长；
    - 文件处理器创建失败**不影响启动**（降级为仅控制台并告警）。
    """
    log_level = getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO)

    # 根日志配置
    logging.basicConfig(
        level=log_level,
        format=_LOG_FORMAT,
        handlers=[logging.StreamHandler(sys.stdout)],   # 输出到控制台
    )

    # 如果配置了日志文件，添加轮转文件处理器
    if settings.LOG_FILE:
        try:
            log_path = Path(settings.LOG_FILE)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            file_handler = RotatingFileHandler(
                log_path,
                maxBytes=int(getattr(settings, 'LOG_MAX_BYTES', 5 * 1024 * 1024)),
                backupCount=int(getattr(settings, 'LOG_BACKUP_COUNT', 5)),
                encoding='utf-8',
            )
            file_handler.setFormatter(logging.Formatter(_LOG_FORMAT))
            logging.getLogger().addHandler(file_handler)
        except Exception as e:  # noqa: BLE001 - 日志落盘失败绝不能阻断服务启动
            logging.getLogger(__name__).warning('文件日志初始化失败（降级为仅控制台）：%s', e)

    # 调整第三方库的日志级别
    logging.getLogger("uvicorn").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
