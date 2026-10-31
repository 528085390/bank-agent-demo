from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


class Database:
    def __init__(self, url: str):
        if not url.startswith("postgresql"):
            raise ValueError("运行时需要 PostgreSQL；不以 SQLite 替代资金行锁语义")
        # pool_pre_ping 每次 checkout 都多一次 SELECT 1 往返，公网 RDS 上约 45ms，关掉它。
        # 代价是 RDS 掐掉的空闲连接会以 OperationalError 冒出来，用 pool_recycle 兜底：
        # 1800 秒先于常见空闲回收窗口主动换连接，比等第一次请求失败再重试便宜。
        self.engine = create_engine(
            url,
            pool_pre_ping=False,
            pool_recycle=1800,
            connect_args={"connect_timeout": 5},
        )
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)

    def dispose(self):
        self.engine.dispose()
