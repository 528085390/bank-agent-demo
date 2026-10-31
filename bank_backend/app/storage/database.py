from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


class Database:
    """PostgreSQL 连接持有者。

    资金写操作依赖 PostgreSQL 的事务与行锁语义，因此显式拒绝 SQLite，
    避免在测试中用 SQLite 替代后丢失并发与 CHECK 约束行为。
    """

    def __init__(self, url: str):
        if not url.startswith("postgresql"):
            raise ValueError("运行时需要 PostgreSQL；不以 SQLite 替代资金行锁语义")
        self.engine = create_engine(
            url, pool_pre_ping=True, hide_parameters=True, connect_args={"connect_timeout": 10}
        )
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)

    def dispose(self):
        self.engine.dispose()
