"""模型测试平台 - 阶段 2 数据层（SQLite）。"""
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    Text,
    UniqueConstraint,
    create_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker

BASE_DIR = Path(__file__).resolve().parents[2]  # workbuddy/
DATA_DIR = Path(os.environ.get("MT_DATA_DIR", BASE_DIR / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = DATA_DIR / "app.db"

engine = create_engine(f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def _now():
    return dt.datetime.now()


class Provider(Base):
    """中转站 / 官方接口 / 自定义 HTTP 服务。"""

    __tablename__ = "providers"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(unique=True)
    base_url: Mapped[str] = mapped_column(default="")
    protocol: Mapped[str] = mapped_column(default="openai")  # openai/anthropic/gemini/custom
    description: Mapped[str] = mapped_column(Text, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[str] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[str] = mapped_column(DateTime, default=_now, onupdate=_now)

    models: Mapped[list["ModelEntity"]] = relationship(back_populates="provider", cascade="all, delete-orphan")
    credentials: Mapped[list["Credential"]] = relationship(back_populates="provider", cascade="all, delete-orphan")


class Credential(Base):
    """加密保存的 API Key，列表中只显示掩码。"""

    __tablename__ = "credentials"

    id: Mapped[int] = mapped_column(primary_key=True)
    provider_id: Mapped[int] = mapped_column(ForeignKey("providers.id", ondelete="CASCADE"))
    label: Mapped[str] = mapped_column(default="默认")
    encrypted_key: Mapped[str] = mapped_column(Text)
    key_hint: Mapped[str] = mapped_column(default="")  # 掩码，如 sk-****abcd
    created_at: Mapped[str] = mapped_column(DateTime, default=_now)

    provider: Mapped[Provider] = relationship(back_populates="credentials")


class ModelEntity(Base):
    """Provider 下的模型。同一模型名可登记在不同中转站（用于跨中转站对比）。"""

    __tablename__ = "model_entities"
    __table_args__ = (UniqueConstraint("provider_id", "model_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    provider_id: Mapped[int] = mapped_column(ForeignKey("providers.id", ondelete="CASCADE"))
    model_name: Mapped[str] = mapped_column(default="")
    display_name: Mapped[str] = mapped_column(default="")
    model_url: Mapped[str] = mapped_column(default="")  # 官网/文档地址，随时查看最新规格
    context_length: Mapped[int] = mapped_column(default=0)
    input_price: Mapped[float] = mapped_column(default=0.0)
    output_price: Mapped[float] = mapped_column(default=0.0)
    capabilities_json: Mapped[str] = mapped_column(Text, default="{}")
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[str] = mapped_column(DateTime, default=_now)

    provider: Mapped[Provider] = relationship(back_populates="models")

    @property
    def capabilities(self) -> dict:
        try:
            return json.loads(self.capabilities_json or "{}")
        except json.JSONDecodeError:
            return {}


class TestProject(Base):
    """可复用测试项目（阶段 2 只登记，不执行）。"""

    __tablename__ = "test_projects"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(unique=True)
    description: Mapped[str] = mapped_column(Text, default="")
    category: Mapped[str] = mapped_column(default="通用")  # chat/stream/json/tools/vision/导入...
    provider_id: Mapped[int | None] = mapped_column(ForeignKey("providers.id", ondelete="SET NULL"), nullable=True)
    model_id: Mapped[int | None] = mapped_column(ForeignKey("model_entities.id", ondelete="SET NULL"), nullable=True)
    script_path: Mapped[str] = mapped_column(default="")  # 关联脚本（只记录路径，不复制）
    command: Mapped[str] = mapped_column(default="")  # 预留：执行命令
    script_dir: Mapped[str] = mapped_column(default="")  # 脚本文件夹（记录路径，不复制）；执行模式=script 时使用
    exec_mode: Mapped[str] = mapped_column(default="builtin")  # builtin（标准四类测试）/ script（运行脚本文件夹）
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    source: Mapped[str] = mapped_column(default="manual")  # manual / imported
    created_at: Mapped[str] = mapped_column(DateTime, default=_now)

    assets: Mapped[list["ImportedAsset"]] = relationship(back_populates="project", cascade="all, delete-orphan")
    provider_ref: Mapped["Provider | None"] = relationship()
    model_ref: Mapped["ModelEntity | None"] = relationship()


class ImportedAsset(Base):
    """从旧目录登记的脚本 / 结果 / 报告文件（只读引用，不移动不复制）。"""

    __tablename__ = "imported_assets"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int | None] = mapped_column(ForeignKey("test_projects.id", ondelete="CASCADE"), nullable=True)
    rel_path: Mapped[str] = mapped_column(Text)  # 相对旧资料根目录
    file_kind: Mapped[str] = mapped_column(default="other")  # script/json/table/html/doc/other
    size_bytes: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[str] = mapped_column(DateTime, default=_now)

    project: Mapped[TestProject] = relationship(back_populates="assets")


class Setting(Base):
    """平台级设置（主密码哈希等）。"""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(primary_key=True)
    value: Mapped[str] = mapped_column(Text)


class MatrixReview(Base):
    """兼容性矩阵单元格的人工复核结论（阶段 4）。

    覆盖自动聚合结论；note 为人工备注。同一 (provider, model, category) 唯一。
    """

    __tablename__ = "matrix_reviews"

    id: Mapped[int] = mapped_column(primary_key=True)
    provider_id: Mapped[int] = mapped_column(default=0)
    model_name: Mapped[str] = mapped_column(default="")
    category: Mapped[str] = mapped_column(default="")
    override_status: Mapped[str] = mapped_column(default="")  # compatible/partial/unsupported/unknown/manual_review，空=不覆盖
    note: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[dt.datetime] = mapped_column(default=_now, onupdate=_now)


class Run(Base):
    """一次测试项目的实际执行记录（阶段 3）。

    保存 Provider/模型快照，项目删除后运行历史仍可追溯。
    """

    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int | None] = mapped_column(ForeignKey("test_projects.id", ondelete="SET NULL"), nullable=True)
    project_name: Mapped[str] = mapped_column(default="")
    provider_id: Mapped[int] = mapped_column(default=0)
    provider_name: Mapped[str] = mapped_column(default="")
    model_name: Mapped[str] = mapped_column(default="")
    job_id: Mapped[str] = mapped_column(default="")
    status: Mapped[str] = mapped_column(default="running")  # running/passed/failed/cancelled/error
    http_status: Mapped[int] = mapped_column(default=0)
    passed: Mapped[bool] = mapped_column(Boolean, default=False)
    duration_ms: Mapped[int] = mapped_column(default=0)
    ttft_ms: Mapped[int] = mapped_column(default=0)
    error_class: Mapped[str] = mapped_column(default="")
    verdict: Mapped[str] = mapped_column(Text, default="")
    summary_json: Mapped[str] = mapped_column(Text, default="{}")
    evidence_dir: Mapped[str] = mapped_column(Text, default="")
    started_at: Mapped[str] = mapped_column(DateTime, default=_now)
    finished_at: Mapped[str | None] = mapped_column(DateTime, nullable=True)


class SuiteReport(Base):
    __tablename__ = "suite_reports"
    id: Mapped[int] = mapped_column(primary_key=True)
    scope_key: Mapped[str] = mapped_column(unique=True)
    source_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(default=_now)


class SuiteReportVersion(Base):
    __tablename__ = "suite_report_versions"
    __table_args__ = (UniqueConstraint("report_id", "version"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    report_id: Mapped[int] = mapped_column(ForeignKey("suite_reports.id"))
    version: Mapped[int] = mapped_column()
    content_json: Mapped[str] = mapped_column(Text)
    editor: Mapped[str] = mapped_column(default="本机用户")
    change_summary: Mapped[str] = mapped_column(default="")
    created_at: Mapped[dt.datetime] = mapped_column(default=_now)


def init_db() -> None:
    import re

    from sqlalchemy import text

    Base.metadata.create_all(engine)
    # 轻量迁移：为旧库补充后续新增的列（SQLite 的 create_all 不会更新已有表）
    with engine.begin() as conn:
        cols = [row[1] for row in conn.execute(text("PRAGMA table_info(model_entities)"))]
        if "model_url" not in cols:
            conn.execute(text("ALTER TABLE model_entities ADD COLUMN model_url TEXT NOT NULL DEFAULT ''"))
        # 阶段 4 扩展迁移：测试项目支持「脚本文件夹」+「执行模式」
        pcols = [row[1] for row in conn.execute(text("PRAGMA table_info(test_projects)"))]
        if "script_dir" not in pcols:
            conn.execute(text("ALTER TABLE test_projects ADD COLUMN script_dir TEXT NOT NULL DEFAULT ''"))
        if "exec_mode" not in pcols:
            conn.execute(text("ALTER TABLE test_projects ADD COLUMN exec_mode TEXT NOT NULL DEFAULT 'builtin'"))
        # 阶段 4 迁移：model_name 全局唯一 → (provider_id, model_name) 组合唯一。
        # 旧表带有 UNIQUE(model_name) 自约束，需重建表；legacy_alter_table=ON 保证
        # RENAME 时其他表（test_projects）的外键引用不被改写。
        ddl = conn.execute(
            text("SELECT sql FROM sqlite_master WHERE type='table' AND name='model_entities'")
        ).scalar()
        if ddl and re.search(r"UNIQUE\s*\(\s*model_name\s*\)", ddl):
            conn.exec_driver_sql("PRAGMA legacy_alter_table=ON")
            conn.execute(text("ALTER TABLE model_entities RENAME TO _mig_models"))
    # 重建 model_entities（含组合唯一约束），再迁回数据
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        exists = conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' AND name='_mig_models'")
        ).scalar()
        if exists:
            conn.execute(text(
                "INSERT INTO model_entities (provider_id, model_name, display_name, model_url, "
                "context_length, input_price, output_price, capabilities_json, notes, created_at) "
                "SELECT provider_id, model_name, display_name, model_url, "
                "context_length, input_price, output_price, capabilities_json, notes, created_at "
                "FROM _mig_models"
            ))
            conn.execute(text("DROP TABLE _mig_models"))
