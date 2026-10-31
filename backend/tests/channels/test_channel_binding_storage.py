"""`channel_binding` 的存储契约：迁移建表、落库、唯一绑定、外键与时间戳。

每个用例都跑在 `rt` 造出来的独立 schema 上——那个 fixture 会先把全部迁移 replay 到
head，所以这里的建表来自 0018 迁移本身，不是 `Base.metadata.create_all`。
"""

from __future__ import annotations

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from app.storage.models import ChannelBinding

# `seed()` 在每个测试 schema 里写入的演示用户；外键必须指向一个真实存在的 app_user。
BOUND_USER = "user_0001"

#: 渠道绑定迁移自己。它是本仓最新的一档，所以链的 head 就是它。
OUR_REVISION = "0022_channel_binding"
#: 我们这条迁移挂在其后；downgrade 一步应当只拆掉渠道表，不碰别人的表。
PARENT_REVISION = "0021_email_notification_task_id"


def binding(**overrides) -> ChannelBinding:
    values: dict = {
        "channel": "feishu",
        "platform_user_id": "ou_test123",
        "platform_username": "测试用户",
        "user_id": BOUND_USER,
    }
    values.update(overrides)
    return ChannelBinding(**values)


def open_conversation(rt, user_id: str = BOUND_USER) -> str:
    """开一条真的会话，返回它的 id：`conversation_id` 现在是有外键的列。"""

    return rt.conversations.create(user_id, "渠道私聊").conversation_id


def db_head(rt) -> str:
    """库里当前落在哪个迁移版本上（`rt` 的 schema 里真的有 `alembic_version` 表）。"""

    with rt.database.sessions() as db:
        return str(db.scalar(text("SELECT version_num FROM alembic_version")))


def test_the_binding_migration_sits_on_the_single_head(rt):
    """链上只有一个 head，且那就是渠道绑定这一档；降级/升级可逆。

    它真正想钉的是「链是单头、渠道绑定这一档在最上面、且降级一档再升级能回来」，这三件事不随
    以后新增迁移而变化——所以只写自己的 revision，不写别人的。
    """

    script = ScriptDirectory.from_config(Config("alembic.ini"))

    heads = script.get_heads()
    assert heads == [OUR_REVISION]
    parent = script.get_revision(OUR_REVISION).down_revision
    assert parent is not None and script.get_revision(parent) is not None

    # 库里落在 head 上，且降级一档正好落到这一档的父节点、再升级又回到 head。
    assert db_head(rt) == OUR_REVISION

    command.downgrade(Config("alembic.ini"), parent)
    assert db_head(rt) == parent

    command.upgrade(Config("alembic.ini"), "head")
    assert db_head(rt) == OUR_REVISION


def test_a_binding_round_trips_through_the_migrated_schema(rt):
    """插进迁移建出来的表，再按平台 ID 查回来，字段一个不少。"""

    conversation = open_conversation(rt)

    with rt.database.sessions.begin() as db:
        db.add(binding(conversation_id=conversation))

    with rt.database.sessions() as db:
        saved = db.scalar(
            select(ChannelBinding).where(ChannelBinding.platform_user_id == "ou_test123")
        )

    assert saved is not None
    assert saved.channel == "feishu"
    assert saved.platform_user_id == "ou_test123"
    assert saved.platform_username == "测试用户"
    assert saved.user_id == BOUND_USER
    assert saved.conversation_id == conversation
    assert saved.id is not None
    assert saved.created_at is not None


def test_the_table_comes_from_the_migration_with_its_constraints(rt):
    """表、列、唯一约束、两条外键、索引都是迁移里的 DDL，而不是模型侧的推测。"""

    with rt.database.sessions() as db:
        assert db.scalar(text("SELECT current_schema()")).startswith("test_runtime_")

        columns = {
            row[0]
            for row in db.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = current_schema() AND table_name = 'channel_binding'"
                )
            )
        }
        assert columns == {
            "id",
            "channel",
            "platform_user_id",
            "platform_username",
            "user_id",
            "conversation_id",
            "created_at",
            "updated_at",
        }

        unique = db.scalar(
            text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conrelid = 'channel_binding'::regclass AND contype = 'u'"
            )
        )
        assert unique == "UNIQUE (channel, platform_user_id)"

        # 两条外键：user_id → app_user，conversation_id → conversation。
        referenced = {
            row[0]
            for row in db.execute(
                text(
                    "SELECT confrelid::regclass::text FROM pg_constraint "
                    "WHERE conrelid = 'channel_binding'::regclass AND contype = 'f'"
                )
            )
        }
        assert referenced == {"app_user", "conversation"}

        indexes = {
            row[0]
            for row in db.execute(
                text(
                    "SELECT indexname FROM pg_indexes "
                    "WHERE schemaname = current_schema() AND tablename = 'channel_binding'"
                )
            )
        }
        assert "ix_channel_binding_user_id" in indexes


def test_the_same_platform_user_cannot_be_bound_twice(rt):
    """同 channel + platform_user_id 的第二次插入被数据库拒绝，且只留下第一行。"""

    conversation = open_conversation(rt)

    with rt.database.sessions.begin() as db:
        db.add(binding(conversation_id=conversation))

    with pytest.raises(IntegrityError):
        with rt.database.sessions.begin() as db:
            db.add(binding(platform_username="冒名顶替", conversation_id=conversation))

    with rt.database.sessions() as db:
        assert db.scalar(select(func.count()).select_from(ChannelBinding)) == 1


def test_the_same_platform_user_can_be_bound_in_another_channel(rt):
    """唯一键是 (channel, platform_user_id) 组合：换了 channel 就是另一件事。"""

    conversation = open_conversation(rt)

    with rt.database.sessions.begin() as db:
        db.add(binding(conversation_id=conversation))
        db.add(binding(channel="wecom", platform_user_id="ou_test123", conversation_id=None))

    with rt.database.sessions() as db:
        rows = db.scalars(
            select(ChannelBinding).where(ChannelBinding.platform_user_id == "ou_test123")
        ).all()

    assert {row.channel for row in rows} == {"feishu", "wecom"}


def test_the_channel_column_defaults_to_feishu(rt):
    """渠道列的默认值在模型侧（`default="feishu"`），不传 channel 落库就是 feishu。"""

    with rt.database.sessions.begin() as db:
        db.add(
            ChannelBinding(
                platform_user_id="ou_no_channel",
                user_id=BOUND_USER,
            )
        )

    with rt.database.sessions() as db:
        saved = db.scalar(
            select(ChannelBinding).where(ChannelBinding.platform_user_id == "ou_no_channel")
        )

    assert saved is not None
    assert saved.channel == "feishu"


def test_a_binding_may_have_no_conversation_yet(rt):
    """`conversation_id` 可空：绑定先于会话存在，会话由 `get_or_create_conversation` 后补。"""

    with rt.database.sessions.begin() as db:
        db.add(binding(conversation_id=None))

    with rt.database.sessions() as db:
        saved = db.scalar(
            select(ChannelBinding).where(ChannelBinding.platform_user_id == "ou_test123")
        )

    assert saved is not None
    assert saved.conversation_id is None


def test_a_binding_must_point_at_an_existing_app_user(rt):
    """外键落在 app_user.user_id 上：绑定一个不存在的用户插不进去。"""

    conversation = open_conversation(rt)

    with pytest.raises(IntegrityError):
        with rt.database.sessions.begin() as db:
            db.add(binding(user_id="user_not_in_the_database", conversation_id=conversation))

    with rt.database.sessions() as db:
        assert db.scalar(select(func.count()).select_from(ChannelBinding)) == 0


def test_a_binding_must_point_at_an_existing_conversation(rt):
    """conversation_id 也是外键：指一个不存在的会话绑不上，与同库其它类列一致。"""

    with pytest.raises(IntegrityError):
        with rt.database.sessions.begin() as db:
            db.add(binding(conversation_id="conv_that_does_not_exist"))

    with rt.database.sessions() as db:
        assert db.scalar(select(func.count()).select_from(ChannelBinding)) == 0


def test_the_orm_fills_both_timestamps_on_insert(rt):
    """created_at / updated_at 由 ORM 的 `default=func.now()` 填，插入时不必手工赋时间。

    DDL 上两列是 `nullable=False` 且没有 server default——所以「ORM 侧默认值确实生效」值得
    单独钉一条：它一旦失效，插入会直接违反非空约束，而不是悄悄落个空值。
    """

    conversation = open_conversation(rt)

    with rt.database.sessions.begin() as db:
        db.add(binding(conversation_id=conversation))

    with rt.database.sessions() as db:
        saved = db.scalar(
            select(ChannelBinding).where(ChannelBinding.platform_user_id == "ou_test123")
        )

    assert saved is not None
    assert saved.created_at is not None
    assert saved.updated_at is not None
    # 带时区：库里是 `timestamptz`，取回来不该是个裸 naive 时间。
    assert saved.created_at.tzinfo is not None
    assert saved.updated_at.tzinfo is not None
    assert saved.updated_at >= saved.created_at


def test_updating_a_binding_moves_updated_at_forward(rt):
    """`onupdate=func.now()` 生效：改一行之后 updated_at 往前走，created_at 不动。

    两次写入必须落在不同事务里——PG 的 `now()` 是事务开始时间，同一事务内的两次写入拿到
    的是同一个值，那样这条用例就测不出 `onupdate` 到底有没有挂上去。
    """

    conversation = open_conversation(rt)

    with rt.database.sessions.begin() as db:
        db.add(binding(conversation_id=conversation))

    with rt.database.sessions() as db:
        before = db.scalar(
            select(ChannelBinding).where(ChannelBinding.platform_user_id == "ou_test123")
        )

    with rt.database.sessions.begin() as db:
        row = db.scalar(
            select(ChannelBinding).where(ChannelBinding.platform_user_id == "ou_test123")
        )
        row.platform_username = "改过的名字"

    with rt.database.sessions() as db:
        after = db.scalar(
            select(ChannelBinding).where(ChannelBinding.platform_user_id == "ou_test123")
        )

    assert after is not None and before is not None
    assert after.platform_username == "改过的名字"
    assert after.created_at == before.created_at
    assert after.updated_at > before.updated_at


def test_downgrading_drops_the_table_and_upgrading_brings_it_back(rt):
    """downgrade 反向删干净（索引先于表），再 upgrade 能原样回来。"""

    config = Config("alembic.ini")

    command.downgrade(config, PARENT_REVISION)
    with rt.database.sessions() as db:
        assert db.scalar(text("SELECT to_regclass('channel_binding') IS NULL")) is True
        assert db.scalar(text("SELECT to_regclass('app_user')")) is not None

    command.upgrade(config, "head")
    with rt.database.sessions() as db:
        assert db.scalar(text("SELECT to_regclass('channel_binding')")) is not None


def channel_binding_foreign_keys(rt) -> set[str]:
    """`channel_binding` 上全部外键的定义（PG 自己的写法），用来比对降级/升级前后。"""

    with rt.database.sessions() as db:
        rows = db.execute(
            text(
                """
                SELECT pg_get_constraintdef(con.oid)
                FROM pg_constraint con
                JOIN pg_class rel ON rel.oid = con.conrelid
                WHERE rel.relname = 'channel_binding'
                  AND rel.relnamespace = current_schema()::regnamespace
                  AND con.contype = 'f'
                """
            )
        ).all()

    return {row[0] for row in rows}


def test_the_conversation_foreign_key_survives_a_downgrade_and_upgrade(rt):
    """「downgrade 可逆」不只是表回来了：外键也得跟着回来，而且真的还在拦住坏数据。

    0018 是分支里直接改的一版（未部署），所以它必须自己说清降级再升级之后形状不变；
    否则某天真的回滚再前滚，表回来了却少了这道防线，错就错在没人看出来的地方。
    """

    config = Config("alembic.ini")
    expected = {
        "FOREIGN KEY (conversation_id) REFERENCES conversation(conversation_id)",
        "FOREIGN KEY (user_id) REFERENCES app_user(user_id)",
    }

    assert channel_binding_foreign_keys(rt) == expected

    command.downgrade(config, PARENT_REVISION)
    command.upgrade(config, "head")

    assert channel_binding_foreign_keys(rt) == expected

    # 约束不是在目录里躺着而已：指一个不存在的会话仍然插不进去。
    with pytest.raises(IntegrityError):
        with rt.database.sessions.begin() as db:
            db.add(binding(conversation_id="conv_that_does_not_exist"))
