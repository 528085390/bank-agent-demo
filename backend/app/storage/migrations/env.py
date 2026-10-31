from alembic import context
from sqlalchemy import create_engine

import app.storage.bill_models  # noqa: F401  (registers the bill tables on Base.metadata)
from app.core.config import get_settings
from app.storage.models import Base

config = context.config
url = get_settings().database_url

if context.is_offline_mode():
    context.configure(url=url, target_metadata=Base.metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    engine = create_engine(url, connect_args={"connect_timeout": 5})
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=Base.metadata)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()
