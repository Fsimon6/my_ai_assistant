import logging
from typing import Callable
from functools import wraps
import time

logger = logging.getLogger(__name__)


class RetryConfig:
    """重新配置"""

    def __init__(
        self,
        max_retries: int = 3,
        delay: float = 1.0,
        backoff: float = 2.0,
        exceptions: tuple = (Exception,),
        logger: logging.Logger = None,
    ):
        self.max_retries = max_retries
        self.delay = delay
        self.backoff = backoff
        self.exceptions = exceptions
        self.logger = logger or logging.getLogger(__name__)


def retry(config: RetryConfig = None):
    """重试装饰器"""
    if config is None:
        config = RetryConfig()

    def decorator(func: Callable):
        if asyncio.iscoroutinefunction(func):
            @wraps(func)
            async def async_wrapper(*args, **kwargs):
                last_exception = None
                current_delay = config.delay

                for attempt in range(config.max_retries + 1):
                    try:
                        if attempt > 0:
                            config.logger.warning(
                                f'重试{func.__name__}，第{attempt}次尝试,'
                                f'延迟{current_delay:.2f}秒'
                            )
                            await asyncio.sleep(current_delay)
                            current_delay *= config.backoff
                        return await func(*args, **kwargs)
                    except config.exceptions as e:
                        last_exception = e
                        config.logger.warning(f'{func.__name__} 第{attempt + 1}次失败：{str(e)[:100]}')

                        if attempt == config.max_retries:
                            config.logger.error(f'{func.__name__} 达到最大重试次数({config.max_retries})')
                            raise
                raise last_exception

            return async_wrapper

        else:
            @wraps(func)
            def sync_wrapper(*args, **kwargs):
                last_exception = None
                current_delay = config.delay

                for attempt in range(config.max_retries + 1):
                    try:
                        if attempt > 0:
                            config.logger.warning(
                                f'重试{func.__name__}，第{attempt}次尝试,'
                                f'延迟{current_delay:.2f}秒'
                            )
                            time.sleep(current_delay)
                            current_delay *= config.backoff

                        return func(*args, **kwargs)

                    except config.exceptions as e:
                        last_exception = e
                        config.logger.warning(f'{func.__name__} 第{attempt + 1}次失败：{str(e)[:100]}')
                        if attempt == config.max_retries:
                            config.logger.error(f'{func.__name__} 达到最大重试次数({config.max_retries})')
                            raise

                raise last_exception

            return sync_wrapper

    return decorator


# 预定义的重试配置
llm_retry_config = RetryConfig(
    max_retries=3,
    delay=2.0,
    backoff=1.5,
    exceptions=(Exception,),
)

logger1 = logging.getLogger('llm_retry')


api_retry_config = RetryConfig(
    max_retries=2,
    delay=1.0,
    backoff=2.0,
    exceptions=(ConnectionError, TimeoutError),

    logger=logging.getLogger('api_retry')
)


# 使用示例
if __name__ == '__main__':
    @retry(llm_retry_config)
    async def test_async_function():
        """测试异步重试函数"""
        raise ConnectionError('测试连接错误')

    @retry(api_retry_config)
    def test_sync_function():
        """测试同步重试函数"""
        raise TimeoutError('测试超时错误')

    # 测试
    import asyncio

    async def test():
        try:
            await test_async_function()
        except Exception as e:
            print(f'异步函数最终失败：{e}')

        try:
            await test_sync_function()
        except Exception as e:
            print(f'同步函数最终失败：{e}')

    asyncio.run(test())
