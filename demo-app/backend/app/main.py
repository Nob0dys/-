from __future__ import annotations

import json
import os
import re
import shutil
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi import BackgroundTasks, Cookie, Depends, FastAPI, File, Form, HTTPException, Query, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import String, delete, func, or_, select, update
from sqlalchemy.orm import Session, selectinload

from . import database as dbmod
from .database import get_db, switch_engine
from .excel_service import create_export, parse_history_workbook
from .matching import normalize_text
from .models import (
    AuditEvent,
    AuthSession,
    Customer,
    CustomerRequirement,
    HistoryQuote,
    QuoteJob,
    QuoteLine,
    QuoteOption,
    User,
    ConfirmedPriceDraft,
)
from .security import (
    SESSION_COOKIE,
    create_session,
    get_current_user,
    hash_password,
    require_admin,
    token_hash,
    verify_password,
)
from .services import audit, bump_history_version, history_dedup_key, process_job, seed_database


DATA_DIR = Path(os.getenv("QUOTE_DATA_DIR", "./data")).resolve()
UPLOAD_DIR = DATA_DIR / "uploads"
EXPORT_DIR = DATA_DIR / "exports"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
EXPORT_DIR.mkdir(parents=True, exist_ok=True)
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_MB", "80")) * 1024 * 1024
RUN_INLINE_JOBS = os.getenv("QUOTE_RUN_INLINE_JOBS", "true").lower() == "true"

@asynccontextmanager
async def lifespan(_app: FastAPI):
    seed_database()
    yield


app = FastAPI(title="智能报价系统 API", version="1.0.0", lifespan=lifespan)
origins = [item.strip() for item in os.getenv("CORS_ORIGINS", "http://localhost:3000").split(",") if item.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class LoginInput(BaseModel):
    username: str
    password: str


class RequirementInput(BaseModel):
    attribute_name: str
    operator: str = "contains"
    value: str
    unit: str = ""
    required: bool = True
    notes: str = ""


class CustomerInput(BaseModel):
    name: str
    customer_type: Literal["ordinary", "special", "vip"] = "ordinary"
    discount_percent: float = Field(default=0, ge=0, le=100)
    minimum_margin_percent: float = Field(default=0, ge=0, le=100)
    preferred_manufacturers: list[str] = Field(default_factory=list)
    notes: str = ""
    requirements: list[RequirementInput] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_customer_policy(self):
        if self.customer_type == "special" and not self.requirements:
            raise ValueError("特殊要求客户至少需要一条结构化要求")
        return self


class LineUpdate(BaseModel):
    selected_option_ids: list[int] = Field(min_length=1, max_length=5)
    final_prices: dict[str, float] = Field(default_factory=dict)
    manual_note: str = ""


class ConfirmInput(BaseModel):
    override_reason: str = ""


class CustomerUpdate(BaseModel):
    name: str | None = None
    customer_type: Literal["ordinary", "special", "vip"] | None = None
    discount_percent: float | None = Field(default=None, ge=0, le=100)
    minimum_margin_percent: float | None = Field(default=None, ge=0, le=100)
    preferred_manufacturers: list[str] | None = None
    notes: str | None = None
    requirements: list[RequirementInput] | None = None


class JobRenameInput(BaseModel):
    display_name: str = ""
    tax_rate: float | None = Field(default=None, ge=0.0, le=1.0)


class ChangePasswordInput(BaseModel):
    old_password: str
    new_password: str = Field(min_length=6)


class UserCreateInput(BaseModel):
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=6)
    display_name: str = ""
    role: Literal["admin", "quote"] = "quote"


class UserUpdateInput(BaseModel):
    password: str | None = Field(default=None, min_length=6)
    active: bool | None = None
    display_name: str | None = None
    role: Literal["admin", "quote"] | None = None


class HistoryQuoteInput(BaseModel):
    name: str = Field(min_length=1)
    spec: str = ""
    unit: str = ""
    manufacturer: str = ""
    price: float = Field(gt=0)
    brand: str = ""
    model: str = ""
    quote_date: str = ""
    source_file: str = "手工录入"


class ManualOptionInput(BaseModel):
    manufacturer: str = ""
    brand: str = ""
    model: str = ""
    spec: str = ""
    unit: str = ""
    price: float = Field(gt=0)
    history_quote_id: str | None = None


class BatchConfirmInput(BaseModel):
    scope: Literal["current", "filtered", "ids"] = "filtered"
    line_ids: list[int] = Field(default_factory=list)
    page: int = 1
    page_size: int = 50
    row_filter: str = "all"
    query: str = ""


def user_payload(user: User) -> dict:
    return {"id": user.id, "username": user.username, "display_name": user.display_name, "role": user.role}


def managed_user_payload(user: User) -> dict:
    return {
        "id": user.id,
        "username": user.username,
        "display_name": user.display_name,
        "role": user.role,
        "active": user.active,
        "created_at": user.created_at.isoformat(),
    }


def customer_payload(customer: Customer, price_sheet_count: int = 0) -> dict:
    return {
        "id": customer.id,
        "name": customer.name,
        "customer_type": customer.customer_type,
        "discount_percent": customer.discount_percent,
        "minimum_margin_percent": customer.minimum_margin_percent,
        "preferred_manufacturers": customer.preferred_manufacturers or [],
        "notes": customer.notes,
        "price_sheet_count": price_sheet_count,
        "created_at": customer.created_at.isoformat(),
        "requirements": [
            {
                "id": item.id,
                "attribute_name": item.attribute_name,
                "operator": item.operator,
                "value": item.value,
                "unit": item.unit,
                "required": item.required,
                "notes": item.notes,
            }
            for item in customer.requirements
        ],
    }


def job_payload(job: QuoteJob) -> dict:
    return {
        "id": job.id,
        "file_name": job.file_name,
        "display_name": job.display_name,
        "status": job.status,
        "progress": job.progress,
        "requested_option_count": job.requested_option_count,
        "tax_rate": job.tax_rate,
        "total_lines": job.total_lines,
        "matched_lines": job.matched_lines,
        "review_lines": job.review_lines,
        "unmatched_lines": job.unmatched_lines,
        "confirmed_lines": job.confirmed_lines,
        "error_message": job.error_message,
        "created_at": job.created_at.isoformat(),
        "updated_at": job.updated_at.isoformat(),
        "customer": customer_payload(job.customer) if job.customer else None,
        "created_by": user_payload(job.created_by),
    }


def option_identity(option: QuoteOption, price_override: float | None = None) -> tuple:
    """Duplicate-selection key: manufacturer/brand + price + spec + model.

    Works for both history-linked and manual (history-free) options."""
    history = option.history_quote
    if history is not None:
        maker = history.manufacturer or history.brand
        spec, model = history.spec, history.model
    else:
        maker = option.manual_manufacturer or option.manual_brand
        spec, model = option.manual_spec, option.manual_model
    price = price_override if price_override is not None else option.final_price
    return (
        normalize_text(maker) or f"unknown:{option.id}",
        round(price, 2),
        normalize_text(spec),
        normalize_text(model),
    )


def option_payload(option: QuoteOption, internal: bool = True) -> dict:
    history = option.history_quote
    if history is not None:
        record = {
            "id": history.id,
            "name": history.name,
            "spec": history.spec,
            "model": history.model,
            "brand": history.brand,
            "manufacturer": history.manufacturer,
            "unit": history.unit,
            "price": history.price,
            "quote_date": history.quote_date,
        }
        if internal:
            record.update(
                {
                    "source_file": history.source_file,
                    "source_sheet": history.source_sheet,
                    "source_row": history.source_row,
                }
            )
    else:
        record = {
            "id": f"manual:{option.id}",
            "name": "",
            "spec": option.manual_spec or "",
            "model": option.manual_model or "",
            "brand": option.manual_brand or "",
            "manufacturer": option.manual_manufacturer or "",
            "unit": option.manual_unit or "",
            "price": option.final_price,
            "quote_date": "",
        }
        if internal:
            record.update({"source_file": "手工方案", "source_sheet": "", "source_row": 0})
    return {
        "id": option.id,
        "rank": option.rank,
        "score": option.score,
        "confidence": option.confidence,
        "component_scores": option.component_scores,
        "reasons": option.reasons,
        "warnings": option.warnings,
        "unit_status": option.unit_status,
        "normalized_price": option.normalized_price,
        "selected": option.selected,
        "final_price": option.final_price,
        "manual_note": option.manual_note,
        "record": record,
    }


def line_payload(line: QuoteLine, include_options: bool = False) -> dict:
    selected = [item for item in line.options if item.selected]
    result = {
        "id": line.id,
        "source_row": line.source_row,
        "sheet_name": line.sheet_name,
        "name": line.name,
        "spec": line.spec,
        "product_code": line.product_code,
        "model": line.model,
        "brand": line.brand,
        "manufacturer": line.manufacturer,
        "unit": line.unit,
        "quantity": line.quantity,
        "pricing_quantity": line.pricing_quantity,
        "status": line.status,
        "confidence": line.confidence,
        "recommended_score": line.recommended_score,
        "warnings": line.warnings,
        "confirmed": line.confirmed,
        "selected_option_count": len(selected),
        "primary_option": option_payload(sorted(selected, key=lambda item: item.rank)[0]) if selected else None,
    }
    if include_options:
        result["options"] = [option_payload(item) for item in line.options]
    result["suggested_action"] = _suggest_action(line)
    return result


def _suggest_action(line: QuoteLine) -> dict:
    """基于行状态给出推荐的人工操作。前端可直接显示给用户。"""
    options = sorted(line.options, key=lambda item: item.rank)
    selected = [item for item in options if item.selected]
    blocking = any(
        str(warning).startswith("BLOCK:")
        for item in options for warning in (item.warnings or [])
    )
    estimated = any(
        any("估算价" in str(w) for w in (item.warnings or []))
        for item in selected
    )
    if line.confirmed:
        return {"action": "none", "label": "已确认", "reason": ""}
    if blocking:
        return {
            "action": "must_review",
            "label": "阻断复核",
            "reason": "存在 BLOCK 警告（需求/单位/毛利），不可自动确认",
        }
    if not options:
        return {"action": "manual_quote", "label": "手工建方案", "reason": "无任何候选"}
    if not selected:
        return {"action": "pick_one", "label": "需人工选择", "reason": "有候选但无默认选中"}
    top = options[0]
    runner_up = options[1] if len(options) > 1 else None
    if estimated:
        return {
            "action": "review_estimate",
            "label": "复核估算价",
            "reason": "系统按类目低位分位估算，建议人工确认或调整",
        }
    if (
        line.confidence == "high"
        and line.recommended_score >= 80
        and runner_up is None
        or (runner_up is not None and top.score - runner_up.score >= 15)
    ):
        return {
            "action": "auto_confirm_ok",
            "label": "可直接确认",
            "reason": f"高分({line.recommended_score:.0f})且无并列候选",
        }
    if line.confidence in ("low", "unreliable") or line.recommended_score < 55:
        return {
            "action": "review_carefully",
            "label": "低置信复核",
            "reason": f"匹配置信度低({line.confidence})，建议查竞品/手工询价",
        }
    return {
        "action": "confirm_or_pick",
        "label": "可确认或换方案",
        "reason": f"中等置信({line.confidence})，建议人工最终把关",
    }


@app.get("/api/health")
def health(db: Session = Depends(get_db)):
    db.scalar(select(func.count(User.id)))
    return {"status": "ok", "service": "quote-api"}


@app.post("/api/auth/login")
def login(payload: LoginInput, response: Response, db: Session = Depends(get_db)):
    user = db.scalar(select(User).where(User.username == payload.username))
    if not user or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    token = create_session(db, user)
    response.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        samesite="lax",
        secure=os.getenv("COOKIE_SECURE", "false").lower() == "true",
        max_age=int(os.getenv("SESSION_HOURS", "12")) * 3600,
        path="/",
    )
    return user_payload(user)


@app.get("/api/auth/me")
def me(user: User = Depends(get_current_user)):
    return user_payload(user)


@app.post("/api/auth/logout")
def logout(
    response: Response,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    quote_session: str | None = Cookie(default=None, alias=SESSION_COOKIE),
):
    if quote_session:
        auth = db.scalar(select(AuthSession).where(AuthSession.token_hash == token_hash(quote_session)))
        if auth:
            db.delete(auth)
            db.commit()
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True, "user": user.username}


@app.post("/api/auth/change-password")
def change_password(
    payload: ChangePasswordInput,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not verify_password(payload.old_password, user.password_hash):
        raise HTTPException(status_code=400, detail="原密码错误")
    user.password_hash = hash_password(payload.new_password)
    audit(db, user.id, "user.change_password", "user", user.id)
    db.commit()
    return {"ok": True}


@app.get("/api/users")
def list_users(
    user: User = Depends(require_admin), db: Session = Depends(get_db)
):
    users = db.scalars(select(User).order_by(User.id)).all()
    return [managed_user_payload(item) for item in users]


@app.post("/api/users", status_code=201)
def create_user(
    payload: UserCreateInput,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    username = payload.username.strip()
    if db.scalar(select(User).where(User.username == username)):
        raise HTTPException(status_code=409, detail="用户名已存在")
    new_user = User(
        username=username,
        display_name=payload.display_name.strip() or username,
        role=payload.role,
        password_hash=hash_password(payload.password),
    )
    db.add(new_user)
    db.flush()
    audit(db, user.id, "user.create", "user", new_user.id, {"username": new_user.username, "role": new_user.role})
    db.commit()
    return managed_user_payload(new_user)


@app.patch("/api/users/{user_id}")
def update_user(
    user_id: int,
    payload: UserUpdateInput,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    target = db.get(User, user_id)
    if not target:
        raise HTTPException(status_code=404, detail="用户不存在")
    if target.id == user.id:
        if payload.active is False:
            raise HTTPException(status_code=400, detail="不能停用自己的账号")
        if payload.role is not None and payload.role != "admin":
            raise HTTPException(status_code=400, detail="不能降低自己的管理员权限")
    changed: dict = {}
    if payload.password is not None:
        target.password_hash = hash_password(payload.password)
        changed["password"] = "reset"
    if payload.active is not None:
        target.active = payload.active
        changed["active"] = payload.active
    if payload.display_name is not None:
        target.display_name = payload.display_name.strip() or target.username
        changed["display_name"] = target.display_name
    if payload.role is not None:
        target.role = payload.role
        changed["role"] = payload.role
    audit(db, user.id, "user.update", "user", target.id, changed)
    db.commit()
    return managed_user_payload(target)


def _price_sheet_counts(db: Session) -> dict[int, int]:
    """一次 group-by 汇总各客户专属价目行数，避免列表接口 N+1 查询。"""
    rows = db.execute(
        select(HistoryQuote.customer_id, func.count())
        .where(HistoryQuote.customer_id.is_not(None))
        .group_by(HistoryQuote.customer_id)
    ).all()
    return {customer_id: int(count) for customer_id, count in rows}


@app.get("/api/customers")
def list_customers(
    user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    customers = db.scalars(
        select(Customer).options(selectinload(Customer.requirements)).where(Customer.active.is_(True)).order_by(Customer.created_at.desc())
    ).all()
    counts = _price_sheet_counts(db)
    return [customer_payload(item, price_sheet_count=counts.get(item.id, 0)) for item in customers]


@app.post("/api/customers")
def create_customer(
    payload: CustomerInput,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    if db.scalar(select(Customer).where(Customer.name == payload.name)):
        raise HTTPException(status_code=409, detail="客户名称已存在")
    customer = Customer(
        name=payload.name,
        customer_type=payload.customer_type,
        discount_percent=payload.discount_percent,
        minimum_margin_percent=payload.minimum_margin_percent,
        preferred_manufacturers=payload.preferred_manufacturers,
        notes=payload.notes,
    )
    db.add(customer)
    db.flush()
    for requirement in payload.requirements:
        db.add(CustomerRequirement(customer_id=customer.id, **requirement.model_dump()))
    audit(db, user.id, "customer.create", "customer", customer.id, {"name": customer.name})
    db.commit()
    db.refresh(customer)
    customer = db.scalar(select(Customer).options(selectinload(Customer.requirements)).where(Customer.id == customer.id))
    return customer_payload(customer, price_sheet_count=_price_sheet_counts(db).get(customer.id, 0))


@app.patch("/api/customers/{customer_id}")
def update_customer(
    customer_id: int,
    payload: CustomerUpdate,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    customer = db.scalar(
        select(Customer).options(selectinload(Customer.requirements)).where(Customer.id == customer_id)
    )
    if not customer or not customer.active:
        raise HTTPException(status_code=404, detail="客户不存在")
    new_type = payload.customer_type or customer.customer_type
    requirement_count = (
        len(payload.requirements) if payload.requirements is not None else len(customer.requirements)
    )
    if new_type == "special" and requirement_count == 0:
        raise HTTPException(status_code=400, detail="特殊要求客户至少需要一条结构化要求")
    if payload.name is not None and payload.name != customer.name:
        if db.scalar(select(Customer).where(Customer.name == payload.name)):
            raise HTTPException(status_code=409, detail="客户名称已存在")
        customer.name = payload.name
    if payload.customer_type is not None:
        customer.customer_type = payload.customer_type
    if payload.discount_percent is not None:
        customer.discount_percent = payload.discount_percent
    if payload.minimum_margin_percent is not None:
        customer.minimum_margin_percent = payload.minimum_margin_percent
    if payload.preferred_manufacturers is not None:
        customer.preferred_manufacturers = payload.preferred_manufacturers
    if payload.notes is not None:
        customer.notes = payload.notes
    if payload.requirements is not None:
        for item in list(customer.requirements):
            db.delete(item)
        db.flush()
        for requirement in payload.requirements:
            db.add(CustomerRequirement(customer_id=customer.id, **requirement.model_dump()))
    audit(db, user.id, "customer.update", "customer", customer.id, {"name": customer.name})
    db.commit()
    db.expire_all()
    customer = db.scalar(
        select(Customer).options(selectinload(Customer.requirements)).where(Customer.id == customer_id)
    )
    return customer_payload(customer, price_sheet_count=_price_sheet_counts(db).get(customer_id, 0))


@app.post("/api/customers/{customer_id}/price-sheet")
def upload_customer_price_sheet(
    customer_id: int,
    file: UploadFile = File(...),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """上传（整表替换）客户专属报价单：每客户至多一份，重新上传即覆盖旧行。"""
    customer = db.get(Customer, customer_id)
    if not customer or not customer.active:
        raise HTTPException(status_code=404, detail="客户不存在")
    original_name = Path(file.filename or "price-sheet.xlsx").name
    suffix = Path(original_name).suffix.lower()
    if suffix not in (".xlsx", ".xlsm", ".xls"):
        raise HTTPException(status_code=400, detail="仅支持 .xlsx / .xlsm / .xls 文件")
    temp_dir = DATA_DIR / "tmp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    temp_path = temp_dir / f"price-sheet-{uuid.uuid4()}{suffix or '.xlsx'}"
    try:
        temp_path.write_bytes(file.file.read())
        rows = parse_history_workbook(str(temp_path), original_name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception:
        raise HTTPException(status_code=400, detail="无法解析该文件，请确认是有效的 Excel 文件")
    finally:
        temp_path.unlink(missing_ok=True)

    # 整表替换：先清空该客户全部旧专属行，再写入新行
    db.execute(delete(HistoryQuote).where(HistoryQuote.customer_id == customer_id))
    source_file = f"客户专属价目-{customer.name}"
    inserted = skipped_duplicates = skipped_invalid = 0
    seen_keys: set[tuple] = set()
    used_ids: set[str] = set()
    for row in rows:
        price = row["price"]
        if not price or price <= 0:
            skipped_invalid += 1
            continue
        # 只在该客户自己的专属行集合内去重，不与公共库互相去重
        key = history_dedup_key(row["name"], row["spec"], row["unit"], row["manufacturer"], price)
        if key in seen_keys:
            skipped_duplicates += 1
            continue
        seen_keys.add(key)
        record_id = f"customer-{customer_id}:{row['sheet_name']}:{row['source_row']}"
        if record_id in used_ids:
            sequence = 2
            while f"{record_id}#{sequence}" in used_ids:
                sequence += 1
            record_id = f"{record_id}#{sequence}"
        used_ids.add(record_id)
        quality_fields = [row["spec"], row["model"], row["manufacturer"], row["unit"], "", source_file]
        db.add(
            HistoryQuote(
                id=record_id,
                source_file=source_file,
                source_sheet=row["sheet_name"],
                source_row=row["source_row"],
                name=row["name"],
                normalized_name=normalize_text(row["name"]),
                spec=row["spec"],
                normalized_spec=normalize_text(row["spec"]),
                product_code="",
                normalized_product_code="",
                model=row["model"],
                normalized_model=normalize_text(row["model"]),
                brand=row["brand"],
                manufacturer=row["manufacturer"],
                unit=row["unit"],
                normalized_unit=normalize_text(row["unit"]),
                price=price,
                quote_date="",
                source_priority=0,
                data_quality=sum(bool(item) for item in quality_fields) / len(quality_fields),
                customer_id=customer_id,
            )
        )
        inserted += 1
    audit(
        db,
        user.id,
        "customer.price_sheet.upload",
        "customer",
        customer_id,
        {
            "file": original_name,
            "inserted": inserted,
            "skipped_duplicates": skipped_duplicates,
            "skipped_invalid": skipped_invalid,
        },
    )
    bump_history_version(db)
    db.commit()
    return {
        "inserted": inserted,
        "skipped_duplicates": skipped_duplicates,
        "skipped_invalid": skipped_invalid,
    }


@app.get("/api/customers/{customer_id}/price-sheet")
def get_customer_price_sheet(
    customer_id: int,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    customer = db.get(Customer, customer_id)
    if not customer or not customer.active:
        raise HTTPException(status_code=404, detail="客户不存在")
    rows = db.scalars(
        select(HistoryQuote)
        .where(HistoryQuote.customer_id == customer_id)
        .order_by(HistoryQuote.source_sheet, HistoryQuote.source_row)
    ).all()
    return {
        "count": len(rows),
        "rows": [
            {
                "name": item.name,
                "spec": item.spec,
                "model": item.model,
                "brand": item.brand,
                "manufacturer": item.manufacturer,
                "unit": item.unit,
                "price": item.price,
            }
            for item in rows[:20]
        ],
    }


@app.delete("/api/customers/{customer_id}/price-sheet")
def delete_customer_price_sheet(
    customer_id: int,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    customer = db.get(Customer, customer_id)
    if not customer or not customer.active:
        raise HTTPException(status_code=404, detail="客户不存在")
    deleted = int(
        db.scalar(select(func.count(HistoryQuote.id)).where(HistoryQuote.customer_id == customer_id)) or 0
    )
    db.execute(delete(HistoryQuote).where(HistoryQuote.customer_id == customer_id))
    audit(db, user.id, "customer.price_sheet.delete", "customer", customer_id, {"deleted": deleted})
    bump_history_version(db)
    db.commit()
    return {"ok": True, "deleted": deleted}


@app.delete("/api/customers/{customer_id}")
def delete_customer(
    customer_id: int,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    customer = db.get(Customer, customer_id)
    if not customer or not customer.active:
        raise HTTPException(status_code=404, detail="客户不存在")
    customer.active = False
    audit(db, user.id, "customer.delete", "customer", customer.id, {"name": customer.name})
    db.commit()
    return {"ok": True, "id": customer.id}


@app.post("/api/quote-jobs", status_code=202)
def create_quote_job(
    background_tasks: BackgroundTasks,
    customer_id: int | None = Form(None),
    requested_option_count: int = Form(3, ge=1, le=5),
    tax_rate: float = Form(0.10, ge=0.0, le=1.0),
    file: UploadFile = File(...),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    customer = db.get(Customer, customer_id) if customer_id else None
    if customer_id and (not customer or not customer.active):
        raise HTTPException(status_code=404, detail="客户不存在")
    original_name = Path(file.filename or "quote.xlsx").name
    suffix = Path(original_name).suffix.lower()
    if suffix not in (".xlsx", ".xlsm", ".xls"):
        raise HTTPException(status_code=400, detail="仅支持 .xlsx / .xlsm / .xls 文件")
    job_id = str(uuid.uuid4())
    destination = UPLOAD_DIR / f"{job_id}{suffix or '.xlsx'}"
    size = 0
    with destination.open("wb") as output:
        while chunk := file.file.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_UPLOAD_BYTES:
                output.close()
                destination.unlink(missing_ok=True)
                raise HTTPException(status_code=413, detail=f"文件不能超过 {MAX_UPLOAD_BYTES // 1024 // 1024}MB")
            output.write(chunk)
    job = QuoteJob(
        id=job_id,
        customer_id=customer.id if customer else None,
        created_by_id=user.id,
        file_name=original_name,
        source_file_path=str(destination),
        status="queued",
        requested_option_count=requested_option_count,
        tax_rate=tax_rate,
    )
    db.add(job)
    audit(db, user.id, "job.create", "quote_job", job.id, {"file": original_name})
    db.commit()
    job = db.scalar(
        select(QuoteJob)
        .options(selectinload(QuoteJob.customer).selectinload(Customer.requirements), selectinload(QuoteJob.created_by))
        .where(QuoteJob.id == job.id)
    )
    if RUN_INLINE_JOBS:
        background_tasks.add_task(process_job, job_id)
    return job_payload(job)


@app.get("/api/quote-jobs")
def list_jobs(
    limit: int = Query(30, ge=1, le=100),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    jobs = db.scalars(
        select(QuoteJob)
        .options(selectinload(QuoteJob.customer).selectinload(Customer.requirements), selectinload(QuoteJob.created_by))
        .order_by(QuoteJob.created_at.desc())
        .limit(limit)
    ).all()
    return [job_payload(item) for item in jobs]


@app.get("/api/quote-jobs/{job_id}")
def get_job(
    job_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    job = db.scalar(
        select(QuoteJob)
        .options(selectinload(QuoteJob.customer).selectinload(Customer.requirements), selectinload(QuoteJob.created_by))
        .where(QuoteJob.id == job_id)
    )
    if not job:
        raise HTTPException(status_code=404, detail="报价任务不存在")
    return job_payload(job)


@app.patch("/api/quote-jobs/{job_id}")
def rename_job(
    job_id: str,
    payload: JobRenameInput,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    job = db.get(QuoteJob, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="报价任务不存在")
    if payload.tax_rate is not None:
        job.tax_rate = payload.tax_rate
    job.display_name = payload.display_name.strip() or None
    audit(db, user.id, "job.rename", "quote_job", job.id, {"display_name": job.display_name or "", "tax_rate": job.tax_rate})
    db.commit()
    return {"id": job.id, "display_name": job.display_name, "tax_rate": job.tax_rate}


@app.delete("/api/quote-jobs/{job_id}")
def delete_job(
    job_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    job = db.get(QuoteJob, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="报价任务不存在")
    source_file_path = job.source_file_path
    file_name = job.file_name
    db.execute(
        delete(QuoteOption).where(
            QuoteOption.line_id.in_(select(QuoteLine.id).where(QuoteLine.job_id == job_id))
        )
    )
    db.execute(delete(QuoteLine).where(QuoteLine.job_id == job_id))
    db.delete(job)
    audit(db, user.id, "job.delete", "quote_job", job_id, {"file": file_name})
    db.commit()
    if source_file_path:
        Path(source_file_path).unlink(missing_ok=True)
    shutil.rmtree(EXPORT_DIR / job_id, ignore_errors=True)
    return {"ok": True, "id": job_id}


def json_text_contains(column, phrase: str):
    """Match JSON text serialized by either SQLite or PostgreSQL."""
    rendered = func.cast(column, String)
    escaped = json.dumps(phrase, ensure_ascii=True)[1:-1]
    return or_(rendered.like(f"%{phrase}%"), rendered.like(f"%{escaped}%"))


def apply_line_filters(statement, job_id: str, row_filter: str, query: str):
    statement = statement.where(QuoteLine.job_id == job_id)
    if row_filter == "confirmed":
        statement = statement.where(QuoteLine.confirmed.is_(True))
    elif row_filter == "pending":
        statement = statement.where(QuoteLine.confirmed.is_(False))
    elif row_filter in ("review", "unmatched", "suggested"):
        statement = statement.where(QuoteLine.status == row_filter)
    elif row_filter == "unit_conflict":
        statement = statement.where(json_text_contains(QuoteLine.warnings, "单位不可直接换算"))
    elif row_filter == "parameter_conflict":
        statement = statement.where(json_text_contains(QuoteLine.warnings, "未满足客户要求"))
    elif row_filter == "price_anomaly":
        statement = statement.where(json_text_contains(QuoteLine.warnings, "价格偏离"))
    elif row_filter == "score_tie":
        statement = statement.where(json_text_contains(QuoteLine.warnings, "同分多价"))
    elif row_filter == "low_confidence":
        statement = statement.where(QuoteLine.confidence.in_(["low", "unreliable", "review"]))
    if query.strip():
        pattern = f"%{query.strip()}%"
        statement = statement.where(
            or_(
                QuoteLine.name.ilike(pattern),
                QuoteLine.product_code.ilike(pattern),
                QuoteLine.model.ilike(pattern),
                func.cast(QuoteLine.source_row, String) == query.strip(),
            )
        )
    return statement


@app.get("/api/quote-jobs/{job_id}/lines")
def list_job_lines(
    job_id: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=25, le=100),
    row_filter: str = Query("all"),
    query: str = Query(""),
    sort: str = Query("source_row"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not db.get(QuoteJob, job_id):
        raise HTTPException(status_code=404, detail="报价任务不存在")
    count_statement = apply_line_filters(select(func.count(QuoteLine.id)), job_id, row_filter, query)
    total = int(db.scalar(count_statement) or 0)
    page_count = max(1, (total + page_size - 1) // page_size)
    actual_page = min(page, page_count)
    statement = apply_line_filters(
        select(QuoteLine).options(
            selectinload(QuoteLine.options).selectinload(QuoteOption.history_quote)
        ),
        job_id,
        row_filter,
        query,
    )
    if sort == "score_asc":
        statement = statement.order_by(QuoteLine.recommended_score.asc(), QuoteLine.source_row)
    elif sort == "score_desc":
        statement = statement.order_by(QuoteLine.recommended_score.desc(), QuoteLine.source_row)
    else:
        statement = statement.order_by(QuoteLine.source_row)
    lines = db.scalars(
        statement.offset((actual_page - 1) * page_size).limit(page_size)
    ).unique().all()
    return {
        "items": [line_payload(item) for item in lines],
        "total": total,
        "page": actual_page,
        "page_size": page_size,
        "page_count": page_count,
    }


@app.get("/api/quote-lines/{line_id}")
def get_line(
    line_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    line = db.scalar(
        select(QuoteLine)
        .options(selectinload(QuoteLine.options).selectinload(QuoteOption.history_quote))
        .where(QuoteLine.id == line_id)
    )
    if not line:
        raise HTTPException(status_code=404, detail="报价行不存在")
    return line_payload(line, include_options=True)


@app.patch("/api/quote-lines/{line_id}")
def update_line(
    line_id: int,
    payload: LineUpdate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    line = db.scalar(
        select(QuoteLine)
        .options(selectinload(QuoteLine.options).selectinload(QuoteOption.history_quote))
        .where(QuoteLine.id == line_id)
    )
    if not line:
        raise HTTPException(status_code=404, detail="报价行不存在")
    available = {item.id: item for item in line.options}
    if any(option_id not in available for option_id in payload.selected_option_ids):
        raise HTTPException(status_code=400, detail="包含无效候选方案")
    identity_keys: set[tuple] = set()
    for option_id in payload.selected_option_ids:
        option = available[option_id]
        # Only fully identical options (same manufacturer + price + spec +
        # model) conflict; same manufacturer with a different quote is allowed.
        key = option_identity(option, price_override=payload.final_prices.get(str(option.id)))
        if key in identity_keys:
            raise HTTPException(status_code=400, detail="不能重复选择完全相同的方案")
        identity_keys.add(key)
    for option in line.options:
        option.selected = option.id in payload.selected_option_ids
        price = payload.final_prices.get(str(option.id))
        if price is not None:
            if price <= 0:
                raise HTTPException(status_code=400, detail="报价必须大于0")
            option.final_price = round(price, 2)
        if option.selected:
            option.manual_note = payload.manual_note
    line.confirmed = False
    line.status = "review"
    audit(
        db,
        user.id,
        "line.options.update",
        "quote_line",
        line.id,
        {"selected": payload.selected_option_ids, "note": payload.manual_note},
    )
    db.commit()
    return line_payload(line, include_options=True)


@app.post("/api/quote-lines/{line_id}/options", status_code=201)
def create_manual_option(
    line_id: int,
    payload: ManualOptionInput,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    line = db.scalar(
        select(QuoteLine).options(selectinload(QuoteLine.options)).where(QuoteLine.id == line_id)
    )
    if not line:
        raise HTTPException(status_code=404, detail="报价行不存在")
    history = db.get(HistoryQuote, payload.history_quote_id) if payload.history_quote_id else None
    if payload.history_quote_id and history is None:
        raise HTTPException(status_code=404, detail="历史报价记录不存在")
    if not history and not (payload.manufacturer.strip() or payload.brand.strip()):
        raise HTTPException(status_code=400, detail="手工方案至少填写制造商或品牌之一")
    max_rank = max((item.rank for item in line.options), default=0)
    option = QuoteOption(
        line_id=line.id,
        history_quote_id=history.id if history else None,
        rank=max_rank + 1,
        score=0,
        confidence="manual",
        component_scores={},
        reasons=[],
        warnings=[],
        unit_status="manual",
        normalized_price=None,
        selected=True,
        final_price=round(payload.price, 2),
        manual_brand=payload.brand.strip(),
        manual_manufacturer=payload.manufacturer.strip(),
        manual_model=payload.model.strip(),
        manual_spec=payload.spec.strip(),
        manual_unit=payload.unit.strip(),
    )
    db.add(option)
    line.confirmed = False
    if line.status == "confirmed":
        line.status = "review"
    db.flush()
    audit(
        db,
        user.id,
        "line.option.create",
        "quote_option",
        option.id,
        {
            "line_id": line.id,
            "manufacturer": option.manual_manufacturer,
            "history_quote_id": option.history_quote_id,
            "price": option.final_price,
        },
    )
    db.commit()
    return option_payload(option)


@app.delete("/api/quote-options/{option_id}")
def delete_manual_option(
    option_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    option = db.get(QuoteOption, option_id)
    if not option:
        raise HTTPException(status_code=404, detail="报价方案不存在")
    if option.history_quote_id is not None:
        raise HTTPException(status_code=400, detail="仅允许删除手工方案")
    line_id = option.line_id
    db.delete(option)
    audit(db, user.id, "line.option.delete", "quote_option", option_id, {"line_id": line_id})
    db.commit()
    return {"ok": True, "id": option_id}


def _record_confirmed_price_draft(
    db: Session,
    line: QuoteLine,
    user: User,
    suggested_price: float | None,
) -> ConfirmedPriceDraft | None:
    """When a line is confirmed (or re-confirmed after price override) write a
    draft price record into the confirmed_price_drafts queue. Admins approve via
    the /api/price-drafts endpoints to promote it into HistoryQuote."""
    selected = sorted((item for item in line.options if item.selected), key=lambda o: o.rank)
    if not selected:
        return None
    primary = selected[0]
    history = primary.history_quote
    confirmed_price = float(primary.final_price)
    if confirmed_price <= 0:
        return None
    # 避免重复回写：同一行同一价已存在草稿则复用
    existing = db.scalar(
        select(ConfirmedPriceDraft).where(
            ConfirmedPriceDraft.line_id == line.id,
            ConfirmedPriceDraft.confirmed_price == confirmed_price,
            ConfirmedPriceDraft.status.in_(["pending", "approved"]),
        )
    )
    if existing:
        return existing
    draft = ConfirmedPriceDraft(
        line_id=line.id,
        job_id=line.job_id,
        name=line.name,
        spec=history.spec if history else (primary.manual_spec or line.spec),
        model=history.model if history else (primary.manual_model or line.model),
        brand=history.brand if history else (primary.manual_brand or line.brand),
        manufacturer=history.manufacturer if history else (primary.manual_manufacturer or line.manufacturer),
        unit=history.unit if history else (primary.manual_unit or line.unit),
        product_code=history.product_code if history else line.product_code,
        confirmed_price=confirmed_price,
        suggested_price=suggested_price,
        source="人工确认" if user.role != "admin" else "管理员确认",
        status="pending",
    )
    db.add(draft)
    return draft


@app.post("/api/quote-lines/{line_id}/confirm")
def confirm_line(
    line_id: int,
    payload: ConfirmInput,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    line = db.scalar(
        select(QuoteLine)
        .options(selectinload(QuoteLine.options).selectinload(QuoteOption.history_quote))
        .where(QuoteLine.id == line_id)
    )
    if not line:
        raise HTTPException(status_code=404, detail="报价行不存在")
    selected = [item for item in line.options if item.selected]
    if not selected:
        raise HTTPException(status_code=400, detail="请至少选择一个制造商方案")
    line.confirmed = True
    line.status = "confirmed"
    line.confirmed_by_id = user.id
    line.confirmed_at = datetime.now(timezone.utc)
    line.override_reason = payload.override_reason.strip()
    # 记录 recommended_score 作为 suggested 价，供后续差异分析
    primary = sorted(selected, key=lambda o: o.rank)[0]
    _record_confirmed_price_draft(
        db, line, user,
        suggested_price=float(line.recommended_score) if line.recommended_score else None,
    )
    job = db.get(QuoteJob, line.job_id)
    db.flush()
    job.confirmed_lines = int(
        db.scalar(
            select(func.count(QuoteLine.id)).where(QuoteLine.job_id == job.id, QuoteLine.confirmed.is_(True))
        )
        or 0
    )
    if job.confirmed_lines >= job.total_lines and job.total_lines:
        job.status = "confirmed"
    audit(db, user.id, "line.confirm", "quote_line", line.id, {"override_reason": line.override_reason})
    db.commit()
    return line_payload(line, include_options=True)


@app.post("/api/quote-jobs/{job_id}/batch-confirm")
def batch_confirm(
    job_id: str,
    payload: BatchConfirmInput,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    job = db.get(QuoteJob, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="报价任务不存在")
    statement = select(QuoteLine).options(selectinload(QuoteLine.options))
    if payload.scope == "ids":
        statement = statement.where(QuoteLine.job_id == job_id, QuoteLine.id.in_(payload.line_ids))
    else:
        statement = apply_line_filters(statement, job_id, payload.row_filter, payload.query)
        if payload.scope == "current":
            statement = statement.order_by(QuoteLine.source_row).offset(
                (payload.page - 1) * payload.page_size
            ).limit(payload.page_size)
    lines = db.scalars(statement).unique().all()
    confirmed = skipped = 0
    for line in lines:
        selected = [item for item in line.options if item.selected]
        blocking = any(
            str(warning).startswith("BLOCK:") for item in selected for warning in (item.warnings or [])
        )
        if (
            line.confirmed
            or line.confidence != "high"
            or line.recommended_score < 80
            or blocking
            or not selected
        ):
            skipped += 1
            continue
        line.confirmed = True
        line.status = "confirmed"
        line.confirmed_by_id = user.id
        line.confirmed_at = datetime.now(timezone.utc)
        confirmed += 1
        # 批量确认也走草稿回写（人工核对过才 batch_confirm，因此算可回写样本）
        _record_confirmed_price_draft(
            db, line, user,
            suggested_price=float(line.recommended_score) if line.recommended_score else None,
        )
    db.flush()
    job.confirmed_lines = int(
        db.scalar(
            select(func.count(QuoteLine.id)).where(QuoteLine.job_id == job_id, QuoteLine.confirmed.is_(True))
        )
        or 0
    )
    if job.confirmed_lines >= job.total_lines and job.total_lines:
        job.status = "confirmed"
    audit(db, user.id, "job.batch_confirm", "quote_job", job_id, {"confirmed": confirmed, "skipped": skipped, "scope": payload.scope})
    db.commit()
    return {"confirmed": confirmed, "skipped": skipped, "confirmed_lines": job.confirmed_lines}


@app.post("/api/quote-jobs/{job_id}/reprocess", status_code=202)
def reprocess_job(
    job_id: str,
    background_tasks: BackgroundTasks,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    job = db.get(QuoteJob, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="报价任务不存在")
    job.status = "queued"
    job.progress = 0
    job.error_message = ""
    audit(db, user.id, "job.reprocess", "quote_job", job_id)
    db.commit()
    if RUN_INLINE_JOBS:
        background_tasks.add_task(process_job, job_id)
    return {"id": job_id, "status": "queued"}


@app.post("/api/quote-jobs/{job_id}/export/{variant}")
def export_job(
    job_id: str,
    variant: Literal["internal", "customer"],
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    job = db.scalar(
        select(QuoteJob)
        .options(
            selectinload(QuoteJob.lines)
            .selectinload(QuoteLine.options)
            .selectinload(QuoteOption.history_quote)
        )
        .where(QuoteJob.id == job_id)
    )
    if not job:
        raise HTTPException(status_code=404, detail="报价任务不存在")
    # 未确认行在导出中留空（客户版）/标记待复核（内部复核版），不再阻断导出。
    return _deliver_export(job, variant, user, db)


@app.post("/api/quote-jobs/{job_id}/auto-confirm-and-export/{variant}")
def auto_confirm_and_export(
    job_id: str,
    variant: Literal["internal", "customer"],
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """一键导出：有默认选中方案的行自动确认，其余行保持待复核并直接导出。"""
    job = db.scalar(
        select(QuoteJob)
        .options(
            selectinload(QuoteJob.lines)
            .selectinload(QuoteLine.options)
            .selectinload(QuoteOption.history_quote)
        )
        .where(QuoteJob.id == job_id)
    )
    if not job:
        raise HTTPException(status_code=404, detail="报价任务不存在")
    if job.status in ("queued", "reserved", "matching"):
        raise HTTPException(status_code=400, detail="任务仍在匹配中，请稍后再试")
    for line in job.lines:
        if line.confirmed:
            continue
        selected = [item for item in line.options if item.selected]
        # 估算价方案（待复核）不自动确认
        if selected and not any(
            "估算价" in str(warning)
            for option in selected
            for warning in (option.warnings or [])
        ):
            line.confirmed = True
            line.status = "confirmed"
            line.confirmed_by_id = user.id
            line.confirmed_at = datetime.now(timezone.utc)
    db.flush()
    job.confirmed_lines = int(
        db.scalar(
            select(func.count(QuoteLine.id)).where(QuoteLine.job_id == job.id, QuoteLine.confirmed.is_(True))
        )
        or 0
    )
    if job.confirmed_lines >= job.total_lines and job.total_lines:
        job.status = "confirmed"
    audit(db, user.id, f"job.auto_confirm_export.{variant}", "quote_job", job.id, {"confirmed_lines": job.confirmed_lines})
    return _deliver_export(job, variant, user, db)


def _deliver_export(job, variant: str, user, db) -> FileResponse:
    stem = re.sub(r"[^\w\u4e00-\u9fff-]+", "_", Path(job.file_name).stem)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    label = "内部复核版" if variant == "internal" else "客户版"
    file_name = f"{stem}_{label}_{stamp}.xlsx"
    path = EXPORT_DIR / job.id / file_name
    create_export(job, variant, str(path), db=db)
    job.status = "exported"
    audit(db, user.id, f"job.export.{variant}", "quote_job", job.id, {"file": file_name})
    db.commit()
    return FileResponse(
        str(path),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=file_name,
    )


@app.get("/api/history/search")
def history_search(
    q: str = Query(..., min_length=1),
    limit: int = Query(30, ge=1, le=100),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    pattern = f"%{q.strip()}%"
    rows = db.scalars(
        select(HistoryQuote)
        .where(
            or_(
                HistoryQuote.name.ilike(pattern),
                HistoryQuote.spec.ilike(pattern),
                HistoryQuote.model.ilike(pattern),
                HistoryQuote.brand.ilike(pattern),
                HistoryQuote.manufacturer.ilike(pattern),
            )
        )
        .order_by(HistoryQuote.source_priority.desc(), HistoryQuote.source_row)
        .limit(limit)
    ).all()
    return [
        {
            "id": item.id,
            "name": item.name,
            "spec": item.spec,
            "model": item.model,
            "brand": item.brand,
            "manufacturer": item.manufacturer,
            "unit": item.unit,
            "price": item.price,
            "quote_date": item.quote_date,
            "source": f"{item.source_file} · {item.source_sheet} · 第{item.source_row}行",
        }
        for item in rows
    ]


@app.post("/api/history", status_code=201)
def create_history_quote(
    payload: HistoryQuoteInput,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    quality_fields = [payload.spec, payload.model, payload.manufacturer, payload.unit, payload.quote_date, payload.source_file]
    record = HistoryQuote(
        id=f"manual:{uuid.uuid4()}",
        source_file=payload.source_file.strip() or "手工录入",
        source_sheet="",
        source_row=0,
        name=payload.name.strip(),
        normalized_name=normalize_text(payload.name),
        spec=payload.spec,
        normalized_spec=normalize_text(payload.spec),
        product_code="",
        normalized_product_code="",
        model=payload.model,
        normalized_model=normalize_text(payload.model),
        brand=payload.brand,
        manufacturer=payload.manufacturer,
        unit=payload.unit,
        normalized_unit=normalize_text(payload.unit),
        price=payload.price,
        quote_date=payload.quote_date,
        source_priority=0,
        data_quality=sum(bool(item) for item in quality_fields) / len(quality_fields),
    )
    db.add(record)
    audit(db, user.id, "history.create", "history_quote", record.id, {"name": record.name})
    bump_history_version(db)
    db.commit()
    return {
        "id": record.id,
        "name": record.name,
        "spec": record.spec,
        "model": record.model,
        "brand": record.brand,
        "manufacturer": record.manufacturer,
        "unit": record.unit,
        "price": record.price,
        "quote_date": record.quote_date,
        "source": record.source_file,
    }


@app.get("/api/price-drafts")
def list_price_drafts(
    status: str = Query("pending"),
    limit: int = Query(50, ge=1, le=200),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """管理员查看人工确认价草稿队列（等待审核后进入历史库）。"""
    rows = db.scalars(
        select(ConfirmedPriceDraft)
        .where(ConfirmedPriceDraft.status == status)
        .order_by(ConfirmedPriceDraft.created_at.desc())
        .limit(limit)
    ).all()
    return [
        {
            "id": item.id,
            "line_id": item.line_id,
            "job_id": item.job_id,
            "name": item.name,
            "spec": item.spec,
            "model": item.model,
            "brand": item.brand,
            "manufacturer": item.manufacturer,
            "unit": item.unit,
            "product_code": item.product_code,
            "confirmed_price": item.confirmed_price,
            "suggested_price": item.suggested_price,
            "source": item.source,
            "status": item.status,
            "created_at": item.created_at.isoformat(),
        }
        for item in rows
    ]


class PriceDraftReviewInput(BaseModel):
    action: Literal["approve", "reject"]


@app.post("/api/price-drafts/{draft_id}/review")
def review_price_draft(
    draft_id: int,
    payload: PriceDraftReviewInput,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Approve: 把草稿行写入正式 HistoryQuote；Reject: 丢弃。"""
    draft = db.get(ConfirmedPriceDraft, draft_id)
    if not draft:
        raise HTTPException(status_code=404, detail="草稿不存在")
    if draft.status != "pending":
        return {"ok": False, "reason": f"已审核过: {draft.status}"}
    draft.reviewed_by_id = user.id
    draft.reviewed_at = datetime.now(timezone.utc)
    if payload.action == "reject":
        draft.status = "rejected"
        db.commit()
        return {"ok": True, "status": "rejected"}
    # 批准：写入公共历史库（去重靠 history_dedup_key，重复则跳过）。
    # 只与公共库（customer_id 为空）比对：客户专属价目行不参与，
    # 避免草稿价与某客户专属价相同时被误判为重复而丢弃。
    draft.status = "approved"
    dedup_key = history_dedup_key(
        draft.name, draft.spec, draft.unit, draft.manufacturer, draft.confirmed_price
    )
    existing = db.scalars(select(HistoryQuote).where(HistoryQuote.customer_id.is_(None))).all()
    seen_keys = {
        history_dedup_key(item.name, item.spec, item.unit, item.manufacturer, item.price)
        for item in existing
    }
    if dedup_key in seen_keys:
        db.commit()
        return {"ok": True, "status": "approved", "history_quote_id": None, "note": "重复条目，已跳过写入"}
    new_id = f"draft:{draft.id}"
    db.add(
        HistoryQuote(
            id=new_id,
            source_file=f"人工确认:{draft.source}",
            source_sheet="",
            source_row=draft.id,
            name=draft.name,
            normalized_name=normalize_text(draft.name),
            spec=draft.spec,
            normalized_spec=normalize_text(draft.spec),
            product_code=draft.product_code,
            normalized_product_code=normalize_text(draft.product_code),
            model=draft.model,
            normalized_model=normalize_text(draft.model),
            brand=draft.brand,
            manufacturer=draft.manufacturer,
            unit=draft.unit,
            normalized_unit=normalize_text(draft.unit),
            price=draft.confirmed_price,
            quote_date=datetime.now(timezone.utc).date().isoformat(),
            source_priority=2,  # 人工确认价优先于导入价
            data_quality=0.9,
        )
    )
    audit(db, user.id, "price_draft.approve", "price_draft", draft.id, {"history_quote_id": new_id})
    bump_history_version(db)
    db.commit()
    return {"ok": True, "status": "approved", "history_quote_id": new_id}


@app.get("/api/governance/summary")
def governance_summary(
    user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    records = db.scalars(select(HistoryQuote)).all()
    by_name: dict[str, list[HistoryQuote]] = {}
    for record in records:
        by_name.setdefault(record.normalized_name, []).append(record)
    duplicate_groups = sum(len(items) > 1 for items in by_name.values())
    unit_conflict_groups = sum(
        len({normalize_text(item.unit) for item in items if item.unit}) > 1 for items in by_name.values()
    )
    price_conflict_groups = sum(len({item.price for item in items}) > 1 for items in by_name.values())
    missing_manufacturer = sum(not item.manufacturer for item in records)
    audit_count = int(db.scalar(select(func.count(AuditEvent.id))) or 0)
    return {
        "record_count": len(records),
        "unique_name_count": len(by_name),
        "duplicate_name_groups": duplicate_groups,
        "unit_conflict_groups": unit_conflict_groups,
        "price_conflict_groups": price_conflict_groups,
        "missing_manufacturer_count": missing_manufacturer,
        "audit_event_count": audit_count,
        "matching_weights": {"参数": 45, "型号": 15, "名称": 15, "制造商/品牌": 10, "单位": 10, "数据质量": 5},
    }


@app.post("/api/governance/dedup-history")
def dedup_history(
    user: User = Depends(require_admin), db: Session = Depends(get_db)
):
    # 只对公共历史库（customer_id 为空）去重：客户专属价目行不参与，
    # 既不作为被删的重复项，也不作为公共行的合并目标，避免专属行被误删误并。
    records = db.scalars(
        select(HistoryQuote)
        .where(HistoryQuote.customer_id.is_(None))
        .order_by(HistoryQuote.created_at, HistoryQuote.id)
    ).all()
    keep_by_key: dict[tuple, HistoryQuote] = {}
    removed = 0
    for record in records:
        key = history_dedup_key(record.name, record.spec, record.unit, record.manufacturer, record.price)
        keep = keep_by_key.get(key)
        if keep is None:
            keep_by_key[key] = record
            continue
        db.execute(
            update(QuoteOption)
            .where(QuoteOption.history_quote_id == record.id)
            .values(history_quote_id=keep.id)
        )
        db.delete(record)
        removed += 1
    bump_history_version(db)
    audit(db, user.id, "history.dedup", "history_quote", "all", {"removed": removed})
    db.commit()
    return {"removed": removed}


@app.post("/api/governance/import-history")
def import_history(
    file: UploadFile = File(...),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    original_name = Path(file.filename or "history.xlsx").name
    suffix = Path(original_name).suffix.lower()
    if suffix not in (".xlsx", ".xlsm", ".xls"):
        raise HTTPException(status_code=400, detail="仅支持 .xlsx / .xlsm / .xls 文件")
    temp_dir = DATA_DIR / "tmp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    temp_path = temp_dir / f"import-{uuid.uuid4()}{suffix or '.xlsx'}"
    try:
        temp_path.write_bytes(file.file.read())
        rows = parse_history_workbook(str(temp_path), original_name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception:
        raise HTTPException(status_code=400, detail="无法解析该文件，请确认是有效的 Excel 文件")
    finally:
        temp_path.unlink(missing_ok=True)

    seen_keys = {
        history_dedup_key(item.name, item.spec, item.unit, item.manufacturer, item.price)
        # 只与公共历史库去重：客户专属价目行不参与，避免专属价阻塞公共导入
        for item in db.scalars(select(HistoryQuote).where(HistoryQuote.customer_id.is_(None))).all()
    }
    inserted = skipped_duplicates = skipped_invalid = 0
    for row in rows:
        price = row["price"]
        if not price or price <= 0:
            skipped_invalid += 1
            continue
        key = history_dedup_key(row["name"], row["spec"], row["unit"], row["manufacturer"], price)
        if key in seen_keys:
            skipped_duplicates += 1
            continue
        seen_keys.add(key)
        record_id = f"{original_name}:{row['sheet_name']}:{row['source_row']}"
        if db.get(HistoryQuote, record_id):
            skipped_duplicates += 1
            continue
        quality_fields = [row["spec"], row["model"], row["manufacturer"], row["unit"], "", original_name]
        db.add(
            HistoryQuote(
                id=record_id,
                source_file=original_name,
                source_sheet=row["sheet_name"],
                source_row=row["source_row"],
                name=row["name"],
                normalized_name=normalize_text(row["name"]),
                spec=row["spec"],
                normalized_spec=normalize_text(row["spec"]),
                product_code="",
                normalized_product_code="",
                model=row["model"],
                normalized_model=normalize_text(row["model"]),
                brand=row["brand"],
                manufacturer=row["manufacturer"],
                unit=row["unit"],
                normalized_unit=normalize_text(row["unit"]),
                price=price,
                quote_date="",
                source_priority=0,
                data_quality=sum(bool(item) for item in quality_fields) / len(quality_fields),
            )
        )
        inserted += 1
    audit(
        db,
        user.id,
        "history.import",
        "history_quote",
        original_name,
        {"inserted": inserted, "skipped_duplicates": skipped_duplicates, "skipped_invalid": skipped_invalid},
    )
    bump_history_version(db)
    db.commit()
    return {
        "inserted": inserted,
        "skipped_duplicates": skipped_duplicates,
        "skipped_invalid": skipped_invalid,
    }


@app.get("/api/audit-events")
def audit_events(
    limit: int = Query(50, ge=1, le=200),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    events = db.scalars(
        select(AuditEvent).options(selectinload(AuditEvent.user)).order_by(AuditEvent.created_at.desc()).limit(limit)
    ).all()
    return [
        {
            "id": item.id,
            "action": item.action,
            "entity_type": item.entity_type,
            "entity_id": item.entity_id,
            "detail": item.detail,
            "created_at": item.created_at.isoformat(),
            "user": user_payload(item.user),
        }
        for item in events
    ]


# ---- 数据库管理（新建空库 / 切换库 / 列表）--------------------------------
# 演示与交付场景需要"从零建库"（上传赛特尔25年.xls 前先切到空库）。
# 切换是热切换：当前进程的 engine/SessionLocal 直接换绑，无需重启服务。

def _db_stats(db_name: str) -> dict:
    """Read-only stats of a database file (no engine switch)."""
    path = DATA_DIR / f"{db_name}.db"
    if not path.exists():
        return {"name": db_name, "exists": False, "size_mb": 0, "history_count": 0, "job_count": 0}
    import sqlite3
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        cur = conn.cursor()
        tables = {row[0] for row in cur.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        history = 0
        jobs = 0
        if "history_quotes" in tables:
            history = cur.execute("SELECT COUNT(*) FROM history_quotes").fetchone()[0]
        if "quote_jobs" in tables:
            jobs = cur.execute("SELECT COUNT(*) FROM quote_jobs").fetchone()[0]
    except Exception:
        history = jobs = 0
    finally:
        conn.close()
    return {
        "name": db_name,
        "exists": True,
        "size_mb": round(path.stat().st_size / 1024 / 1024, 1),
        "history_count": history,
        "job_count": jobs,
    }


@app.get("/api/databases")
def list_databases(
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """列出 data 目录下所有 .db 文件及当前激活库。"""
    current = Path(dbmod.DATABASE_URL.split("///", 1)[-1]).name if dbmod.DATABASE_URL.startswith("sqlite") else ""
    current = current.removesuffix(".db")
    names = sorted(
        path.stem for path in DATA_DIR.glob("*.db")
        if path.stem not in ("quote_clean",)  # 内部临时库不展示
    )
    return {
        "current": current,
        "databases": [_db_stats(name) for name in names],
    }


class DatabaseSwitchInput(BaseModel):
    name: str = Field(min_length=1, max_length=80)


@app.post("/api/databases/switch")
def switch_database(
    payload: DatabaseSwitchInput,
    response: Response,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """热切换到指定库；不存在则新建空库并初始化（仅默认账号/示例客户，
    不导入演示历史，保持空库状态供用户自行导入价目本）。
    切换后旧库会话失效，为当前用户重建会话并下发新 cookie。"""
    url = switch_engine(payload.name)
    seed_database(seed_history=False)
    # 新库中重建当前用户会话（旧库会话表已随切换失效）
    new_db = dbmod.SessionLocal()
    try:
        current = new_db.get(User, user.id) or new_db.scalar(
            select(User).where(User.username == user.username)
        )
        if current:
            token = create_session(new_db, current)
            response.set_cookie(
                SESSION_COOKIE,
                token,
                httponly=True,
                samesite="lax",
                secure=os.getenv("COOKIE_SECURE", "false").lower() == "true",
                max_age=int(os.getenv("SESSION_HOURS", "12")) * 3600,
                path="/",
            )
    finally:
        new_db.close()
    return {"ok": True, "url": url, "name": payload.name}
