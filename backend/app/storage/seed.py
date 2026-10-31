from datetime import date
from decimal import Decimal

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core.config import get_settings
from app.storage.database import Database
from app.storage.models import Account, AppUser, BankCustomerPool, Contact

# The bank customer the demo user acts as. `u_wealth` is bank_backend's wealth demo customer:
# it carries the sample holding, the two current accounts and the valid risk assessment the
# wealth domain needs, so the frontend tells the wealth story instead of a money-market-fund-only
# one. `u_zhang` would also be indistinguishable from the bank's own default customer, which is
# exactly the fallback every bank tool is supposed to fail closed against.
DEMO_BANK_CUSTOMER = "u_wealth"

# The pool of pre-provisioned customers a registration claims one of. The ids are derived
# rather than stored because both sides must agree on them without a shared table:
# `bank_backend/scripts/seed_customer_pool.py` creates exactly this range, and the count here
# comes from the same setting. A pool row with no customer behind it is harmless -- the claim
# succeeds and the bank answers 404, which the admin refill endpoint then fixes.
POOL_CUSTOMER_PREFIX = "u_pool_"
# 没有 settings 时（测试里直接 `seed(database, None)`）用的池子大小，与 `Settings.bank_pool_size`
# 的默认值保持一致。
DEFAULT_POOL_SIZE = 20


def pool_customer_id(index: int) -> str:
    return f"{POOL_CUSTOMER_PREFIX}{index:02d}"


def seed(database, settings=None):
    pool_size = settings.bank_pool_size if settings is not None else DEFAULT_POOL_SIZE
    with database.sessions.begin() as db:
        db.execute(text("SELECT pg_advisory_xact_lock(1709202601)"))
        existing = db.scalar(select(AppUser).where(AppUser.user_id == "user_0001"))
        if existing is None:
            db.add(
                AppUser(
                    user_id="user_0001",
                    bank_customer_id=DEMO_BANK_CUSTOMER,
                )
            )
            db.flush()
            db.add(
                Account(
                    user_id="user_0001",
                    account_type="CURRENT",
                    currency="CNY",
                    balance=Decimal("8320.00"),
                    locked_amount=Decimal("0.00"),
                )
            )
            db.add(
                Contact(
                    user_id="user_0001",
                    contact_id="contact_spouse",
                    alias="爱人",
                    birthday=date(1998, 10, 20),
                    account_ref="6222000000001234",
                )
            )
        elif existing.bank_customer_id is None:
            # A demo user created before the binding existed would otherwise stay unbound
            # forever, and every bank tool fails closed without a customer id -- so the app
            # would look wired up while answering nothing. The binding is derived data, so
            # re-seeding reconciles it rather than requiring a manual UPDATE.
            existing.bank_customer_id = DEMO_BANK_CUSTOMER

        _seed_pool(db, pool_size)


def _seed_pool(db, size: int) -> None:
    """Add any missing pool rows, and never touch a row someone already claimed.

    `ON CONFLICT DO NOTHING` is what makes this safe to run on every start: raising the pool
    size later adds the new slots, and re-running never frees a claimed one. Lowering the size
    leaves the surplus rows alone on purpose -- deleting a row would orphan the user bound to
    it, and an unused slot costs nothing.
    """

    if size <= 0:
        return
    rows = [{"customer_id": pool_customer_id(index)} for index in range(1, size + 1)]
    db.execute(
        pg_insert(BankCustomerPool).values(rows).on_conflict_do_nothing(index_elements=["customer_id"])
    )



if __name__ == "__main__":
    settings = get_settings()
    database = Database(settings.database_url)
    seed(database, settings)
    database.dispose()
