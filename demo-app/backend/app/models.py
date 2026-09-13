from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(120))
    role: Mapped[str] = mapped_column(String(20), default="quote")
    password_hash: Mapped[str] = mapped_column(String(512))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuthSession(Base):
    __tablename__ = "auth_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    user: Mapped[User] = relationship()


class Customer(Base):
    __tablename__ = "customers"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    customer_type: Mapped[str] = mapped_column(String(20), default="ordinary")
    discount_percent: Mapped[float] = mapped_column(Float, default=0)
    minimum_margin_percent: Mapped[float] = mapped_column(Float, default=0)
    preferred_manufacturers: Mapped[list] = mapped_column(JSON, default=list)
    notes: Mapped[str] = mapped_column(Text, default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    requirements: Mapped[list[CustomerRequirement]] = relationship(
        back_populates="customer", cascade="all, delete-orphan"
    )


class CustomerRequirement(Base):
    __tablename__ = "customer_requirements"

    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id", ondelete="CASCADE"), index=True)
    attribute_name: Mapped[str] = mapped_column(String(120))
    operator: Mapped[str] = mapped_column(String(20), default="contains")
    value: Mapped[str] = mapped_column(String(200))
    unit: Mapped[str] = mapped_column(String(40), default="")
    required: Mapped[bool] = mapped_column(Boolean, default=True)
    notes: Mapped[str] = mapped_column(Text, default="")
    customer: Mapped[Customer] = relationship(back_populates="requirements")


class HistoryQuote(Base):
    __tablename__ = "history_quotes"

    id: Mapped[str] = mapped_column(String(180), primary_key=True)
    source_file: Mapped[str] = mapped_column(String(300), index=True)
    source_sheet: Mapped[str] = mapped_column(String(200), default="")
    source_row: Mapped[int] = mapped_column(Integer, default=0)
    name: Mapped[str] = mapped_column(String(500), index=True)
    normalized_name: Mapped[str] = mapped_column(String(500), index=True)
    spec: Mapped[str] = mapped_column(Text, default="")
    normalized_spec: Mapped[str] = mapped_column(Text, default="")
    product_code: Mapped[str] = mapped_column(String(200), default="", index=True)
    normalized_product_code: Mapped[str] = mapped_column(String(200), default="", index=True)
    model: Mapped[str] = mapped_column(String(300), default="")
    normalized_model: Mapped[str] = mapped_column(String(300), default="")
    brand: Mapped[str] = mapped_column(String(300), default="")
    manufacturer: Mapped[str] = mapped_column(String(500), default="", index=True)
    unit: Mapped[str] = mapped_column(String(80), default="")
    normalized_unit: Mapped[str] = mapped_column(String(80), default="")
    quantity: Mapped[float | None] = mapped_column(Float, nullable=True)
    price: Mapped[float] = mapped_column(Float)
    quote_date: Mapped[str] = mapped_column(String(40), default="")
    source_priority: Mapped[int] = mapped_column(Integer, default=0)
    data_quality: Mapped[float] = mapped_column(Float, default=0)
    # NULL = 公共历史库；非空 = 该客户的专属报价单行（仅对所属客户的任务可见）
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id"), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class QuoteJob(Base):
    __tablename__ = "quote_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id"), index=True, nullable=True)
    created_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    file_name: Mapped[str] = mapped_column(String(300))
    display_name: Mapped[str | None] = mapped_column(String(300), nullable=True)
    source_file_path: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(30), default="queued", index=True)
    progress: Mapped[int] = mapped_column(Integer, default=0)
    requested_option_count: Mapped[int] = mapped_column(Integer, default=3)
    tax_rate: Mapped[float] = mapped_column(Float, default=0.10)
    total_lines: Mapped[int] = mapped_column(Integer, default=0)
    matched_lines: Mapped[int] = mapped_column(Integer, default=0)
    review_lines: Mapped[int] = mapped_column(Integer, default=0)
    unmatched_lines: Mapped[int] = mapped_column(Integer, default=0)
    confirmed_lines: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    customer: Mapped[Customer] = relationship()
    created_by: Mapped[User] = relationship()
    lines: Mapped[list[QuoteLine]] = relationship(back_populates="job", cascade="all, delete-orphan")


class QuoteLine(Base):
    __tablename__ = "quote_lines"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("quote_jobs.id", ondelete="CASCADE"), index=True)
    sheet_name: Mapped[str] = mapped_column(String(200))
    source_row: Mapped[int] = mapped_column(Integer, index=True)
    name: Mapped[str] = mapped_column(String(500), index=True)
    spec: Mapped[str] = mapped_column(Text, default="")
    product_code: Mapped[str] = mapped_column(String(200), default="")
    model: Mapped[str] = mapped_column(String(300), default="")
    brand: Mapped[str] = mapped_column(String(300), default="")
    manufacturer: Mapped[str] = mapped_column(String(500), default="")
    unit: Mapped[str] = mapped_column(String(80), default="")
    quantity: Mapped[float | None] = mapped_column(Float, nullable=True)
    pricing_quantity: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="pending", index=True)
    confidence: Mapped[str] = mapped_column(String(20), default="unreliable", index=True)
    recommended_score: Mapped[float] = mapped_column(Float, default=0)
    warnings: Mapped[list] = mapped_column(JSON, default=list)
    confirmed: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    confirmed_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    override_reason: Mapped[str] = mapped_column(Text, default="")
    job: Mapped[QuoteJob] = relationship(back_populates="lines")
    options: Mapped[list[QuoteOption]] = relationship(
        back_populates="line", cascade="all, delete-orphan", order_by="QuoteOption.rank"
    )

    __table_args__ = (Index("ix_quote_lines_job_status", "job_id", "status"),)


class QuoteOption(Base):
    __tablename__ = "quote_options"

    id: Mapped[int] = mapped_column(primary_key=True)
    line_id: Mapped[int] = mapped_column(ForeignKey("quote_lines.id", ondelete="CASCADE"), index=True)
    history_quote_id: Mapped[str | None] = mapped_column(ForeignKey("history_quotes.id"), index=True, nullable=True)
    rank: Mapped[int] = mapped_column(Integer)
    score: Mapped[float] = mapped_column(Float)
    confidence: Mapped[str] = mapped_column(String(20))
    component_scores: Mapped[dict] = mapped_column(JSON, default=dict)
    reasons: Mapped[list] = mapped_column(JSON, default=list)
    warnings: Mapped[list] = mapped_column(JSON, default=list)
    unit_status: Mapped[str] = mapped_column(String(30), default="exact")
    normalized_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    selected: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    final_price: Mapped[float] = mapped_column(Float)
    manual_note: Mapped[str] = mapped_column(Text, default="")
    manual_brand: Mapped[str | None] = mapped_column(String(300), nullable=True)
    manual_manufacturer: Mapped[str | None] = mapped_column(String(500), nullable=True)
    manual_model: Mapped[str | None] = mapped_column(String(300), nullable=True)
    manual_spec: Mapped[str | None] = mapped_column(Text, nullable=True)
    manual_unit: Mapped[str | None] = mapped_column(String(80), nullable=True)
    # 回写历史库时保留当时的确认价（避免后续多次覆盖）
    recorded_final_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    line: Mapped[QuoteLine] = relationship(back_populates="options")
    history_quote: Mapped[HistoryQuote | None] = relationship()


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    action: Mapped[str] = mapped_column(String(80), index=True)
    entity_type: Mapped[str] = mapped_column(String(80))
    entity_id: Mapped[str] = mapped_column(String(80), index=True)
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    user: Mapped[User] = relationship()


class SystemMeta(Base):
    """System-wide metadata for cache invalidation: history_version bumps when
    HistoryQuote content changes, letting process_job skip recompute when nothing
    relevant changed."""
    __tablename__ = "system_meta"

    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="0")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class ConfirmedPriceDraft(Base):
    """人工确认行回写的历史报价草稿：经管理员审核后才进入正式 HistoryQuote。

    用户在 confirm_line / update_line 改 final_price 时自动写入，避免真实成交
    价流失——下次匹配可用作真实样本。
    """
    __tablename__ = "confirmed_price_drafts"

    id: Mapped[int] = mapped_column(primary_key=True)
    line_id: Mapped[int] = mapped_column(ForeignKey("quote_lines.id", ondelete="CASCADE"), index=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("quote_jobs.id", ondelete="CASCADE"), index=True)
    # 快照
    name: Mapped[str] = mapped_column(String(500), default="")
    spec: Mapped[str] = mapped_column(Text, default="")
    model: Mapped[str] = mapped_column(String(300), default="")
    brand: Mapped[str] = mapped_column(String(300), default="")
    manufacturer: Mapped[str] = mapped_column(String(500), default="")
    unit: Mapped[str] = mapped_column(String(80), default="")
    product_code: Mapped[str] = mapped_column(String(200), default="")
    # 人工确认价
    confirmed_price: Mapped[float] = mapped_column(Float)
    # 系统原始推荐（供审计/差异分析）
    suggested_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    source: Mapped[str] = mapped_column(String(80), default="人工确认")
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    # pending / approved / rejected
    reviewed_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)

