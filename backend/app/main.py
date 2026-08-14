import csv
import os
from datetime import datetime, timezone
from io import StringIO

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from sqlalchemy import inspect, select, text
from sqlalchemy.orm import Session, joinedload

from .database import Base, SessionLocal, engine, get_db
from .models import AuditEvent, Deal, Lead, LeadActivity, Role, User, Workspace, WorkspaceRecord
from .schemas import ActivityCreate, ActivityResponse, DealCreate, DealResponse, LeadAssignmentUpdate, LeadCreate, LeadPipelineUpdate, LeadResponse, LoginRequest, LoginResponse, UserResponse, WorkspaceResponse
from .security import create_access_token, get_current_user, hash_password, verify_password

app = FastAPI(title="TractorCloser API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


def user_response(user: User) -> UserResponse:
    return UserResponse(
        id=user.id,
        email=user.email,
        role=user.role,
        workspace_id=user.workspace_id,
        workspace_name=user.workspace.name if user.workspace else None,
        timezone=user.workspace.timezone if user.workspace else None,
    )


def audit(db: Session, actor: User | None, event_type: str, detail: str = "", workspace_id: int | None = None) -> None:
    db.add(AuditEvent(actor_user_id=actor.id if actor else None, workspace_id=workspace_id or (actor.workspace_id if actor else None), event_type=event_type, detail=detail))


def seed_initial_workspace() -> None:
    Base.metadata.create_all(bind=engine)
    # create_all does not add new columns to an already-running test database.
    # Keep this small, idempotent compatibility step until formal migrations are added.
    columns = {column["name"] for column in inspect(engine).get_columns("leads")}
    if "assigned_user_id" not in columns:
        with engine.begin() as connection:
            connection.execute(text("ALTER TABLE leads ADD COLUMN assigned_user_id INTEGER"))
    db = SessionLocal()
    try:
        workspace = db.scalar(select(Workspace).where(Workspace.name == "Tractor Bob"))
        if not workspace:
            workspace = Workspace(name="Tractor Bob", timezone="America/Chicago")
            db.add(workspace)
            db.flush()
        accounts = [
            ("austin.barnett@tractorbob.com", Role.ADMIN, "SEED_ADMIN_PASSWORD", workspace.id),
            ("zedwards.orrauto@gmail.com", Role.SALESPERSON, "SEED_SALESPERSON_PASSWORD", workspace.id),
            ("austin.k.barnett@gmail.com", Role.DEVELOPER, "SEED_DEVELOPER_PASSWORD", None),
        ]
        for email, role, password_key, workspace_id in accounts:
            if not db.scalar(select(User).where(User.email == email)):
                password = os.getenv(password_key)
                if not password:
                    continue
                db.add(User(email=email, password_hash=hash_password(password), role=role, workspace_id=workspace_id))
        if not db.scalar(select(Lead).where(Lead.workspace_id == workspace.id)):
            demo_leads = [
                ("Jordan Miller (Test)", "555-0142", "jordan.test@example.com", "2025 Yanmar YM347", 42000, "Negotiating"),
                ("Casey Rodriguez (Test)", "555-0188", "casey.test@example.com", "SA425 tractor package", 31000, "Appointment"),
                ("Morgan Lee (Test)", "555-0161", "morgan.test@example.com", "Compact tractor", 28000, "Quote"),
                ("Riley Smith (Test)", "555-0173", "riley.test@example.com", "Zero-turn mower", 9500, "Contacted"),
                ("Taylor Brooks (Test)", "555-0124", "taylor.test@example.com", "24–30 HP compact tractor", 24000, "New"),
                ("Drew Patel (Test)", "555-0195", "drew.test@example.com", "Mid-mount mower package", 18000, "Demo"),
                ("Cameron Nguyen (Test)", "555-0119", "cameron.test@example.com", "Yanmar UTV", 22000, "Contacted"),
                ("Blake Thompson (Test)", "555-0157", "blake.test@example.com", "48-inch rotary cutter", 4200, "Quote"),
            ]
            db.add_all([Lead(workspace_id=workspace.id, name=name, phone=phone, email=email, equipment=equipment, budget=budget, pipeline_stage=stage, is_test_data=True) for name, phone, email, equipment, budget, stage in demo_leads])
        # Give the test salesperson a small, stable starting workload while
        # preserving unassigned demo leads for the Admin assignment workflow.
        salesperson = db.scalar(select(User).where(User.email == "zedwards.orrauto@gmail.com", User.workspace_id == workspace.id))
        if salesperson and not db.scalar(select(Lead).where(Lead.workspace_id == workspace.id, Lead.assigned_user_id == salesperson.id)):
            starter_leads = list(db.scalars(select(Lead).where(Lead.workspace_id == workspace.id, Lead.is_test_data.is_(True)).order_by(Lead.id).limit(3)))
            for lead in starter_leads:
                lead.assigned_user_id = salesperson.id
        db.commit()
    finally:
        db.close()


@app.on_event("startup")
def startup() -> None:
    seed_initial_workspace()


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/api/auth/login", response_model=LoginResponse)
def login(payload: LoginRequest, db: Session = Depends(get_db)) -> LoginResponse:
    user = db.scalar(select(User).options(joinedload(User.workspace)).where(User.email == str(payload.email).lower()))
    if not user or not user.active or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password")
    audit(db, user, "login")
    db.commit()
    return LoginResponse(access_token=create_access_token(user), user=user_response(user))


@app.get("/api/me", response_model=UserResponse)
def me(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> UserResponse:
    db.refresh(user)
    return user_response(user)


def require_workspace(user: User) -> int:
    if user.workspace_id is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Select a dealership workspace before accessing CRM data")
    return user.workspace_id


def lead_query_for_user(user: User):
    statement = select(Lead).where(Lead.workspace_id == require_workspace(user))
    if user.role == Role.SALESPERSON:
        statement = statement.where(Lead.assigned_user_id == user.id)
    return statement


@app.get("/api/leads", response_model=list[LeadResponse])
def list_leads(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> list[Lead]:
    return list(db.scalars(lead_query_for_user(user).order_by(Lead.created_at.desc())))


@app.post("/api/leads", response_model=LeadResponse, status_code=status.HTTP_201_CREATED)
def create_lead(payload: LeadCreate, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> Lead:
    workspace_id = require_workspace(user)
    if user.role == Role.SALESPERSON:
        payload.assigned_user_id = user.id
    if payload.assigned_user_id is not None:
        assignee = db.scalar(select(User).where(User.id == payload.assigned_user_id, User.workspace_id == workspace_id, User.active.is_(True)))
        if not assignee:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Team member not found in this workspace")
    lead = Lead(workspace_id=workspace_id, **payload.model_dump())
    db.add(lead)
    db.flush()
    db.add(LeadActivity(workspace_id=workspace_id, lead_id=lead.id, type="lead created", body="Customer profile created."))
    audit(db, user, "lead_created", lead.name, workspace_id)
    db.commit()
    db.refresh(lead)
    return lead


def get_workspace_lead(lead_id: int, user: User, db: Session) -> Lead:
    lead = db.scalar(lead_query_for_user(user).where(Lead.id == lead_id))
    if not lead:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Customer not found")
    return lead


@app.patch("/api/leads/{lead_id}/pipeline", response_model=LeadResponse)
def update_lead_pipeline(lead_id: int, payload: LeadPipelineUpdate, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> Lead:
    lead = get_workspace_lead(lead_id, user, db)
    previous_stage = lead.pipeline_stage
    lead.pipeline_stage = payload.pipeline_stage
    if payload.follow_up_enabled is not None:
        lead.follow_up_enabled = payload.follow_up_enabled
    if previous_stage != lead.pipeline_stage:
        db.add(LeadActivity(workspace_id=lead.workspace_id, lead_id=lead.id, type="stage changed", body=f"Moved from {previous_stage} to {lead.pipeline_stage}."))
    audit(db, user, "lead_stage_changed", f"{lead.name}: {previous_stage} → {lead.pipeline_stage}", lead.workspace_id)
    db.commit()
    db.refresh(lead)
    return lead


@app.patch("/api/leads/{lead_id}/assignment", response_model=LeadResponse)
def assign_lead(lead_id: int, payload: LeadAssignmentUpdate, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> Lead:
    if user.role not in {Role.ADMIN, Role.DEVELOPER}:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")
    lead = get_workspace_lead(lead_id, user, db)
    if payload.assigned_user_id is not None:
        assignee = db.scalar(select(User).where(User.id == payload.assigned_user_id, User.workspace_id == lead.workspace_id, User.active.is_(True)))
        if not assignee:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Team member not found in this workspace")
    lead.assigned_user_id = payload.assigned_user_id
    detail = f"{lead.name} assigned" if payload.assigned_user_id else f"{lead.name} unassigned"
    db.add(LeadActivity(workspace_id=lead.workspace_id, lead_id=lead.id, type="lead assigned", body=detail))
    audit(db, user, "lead_assignment_changed", detail, lead.workspace_id)
    db.commit()
    db.refresh(lead)
    return lead


@app.get("/api/leads/{lead_id}/activities", response_model=list[ActivityResponse])
def list_lead_activities(lead_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> list[LeadActivity]:
    lead = get_workspace_lead(lead_id, user, db)
    return list(db.scalars(select(LeadActivity).where(LeadActivity.lead_id == lead.id, LeadActivity.workspace_id == lead.workspace_id).order_by(LeadActivity.created_at.desc())))


@app.post("/api/leads/{lead_id}/activities", response_model=ActivityResponse, status_code=status.HTTP_201_CREATED)
def create_lead_activity(lead_id: int, payload: ActivityCreate, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> LeadActivity:
    lead = get_workspace_lead(lead_id, user, db)
    activity = LeadActivity(workspace_id=lead.workspace_id, lead_id=lead.id, **payload.model_dump())
    db.add(activity)
    audit(db, user, "lead_activity_added", lead.name, lead.workspace_id)
    db.commit()
    db.refresh(activity)
    return activity


@app.get("/api/deals", response_model=list[DealResponse])
def list_deals(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> list[Deal]:
    workspace_id = require_workspace(user)
    statement = select(Deal).where(Deal.workspace_id == workspace_id)
    if user.role == Role.SALESPERSON:
        assigned_ids = select(Lead.id).where(Lead.workspace_id == workspace_id, Lead.assigned_user_id == user.id)
        statement = statement.where(Deal.lead_id.in_(assigned_ids))
    return list(db.scalars(statement.order_by(Deal.sold_at.desc())))


@app.post("/api/deals", response_model=DealResponse, status_code=status.HTTP_201_CREATED)
def record_deal(payload: DealCreate, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> Deal:
    workspace_id = require_workspace(user)
    lead = get_workspace_lead(payload.lead_id, user, db) if payload.lead_id else None
    if lead:
        lead.pipeline_stage = "Sold"
        lead.follow_up_enabled = False
        existing = db.scalar(select(Deal).where(Deal.workspace_id == workspace_id, Deal.lead_id == lead.id))
    else:
        existing = None
    if existing:
        existing.customer = payload.customer
        existing.equipment = payload.equipment
        existing.sale_price = payload.sale_price
        existing.gross_profit = payload.gross_profit
        deal = existing
    else:
        deal = Deal(workspace_id=workspace_id, **payload.model_dump())
        db.add(deal)
        db.flush()
    if lead:
        db.add(LeadActivity(workspace_id=workspace_id, lead_id=lead.id, type="deal recorded", body=f"Sold for ${payload.sale_price:,} with ${payload.gross_profit:,} total gross."))
    audit(db, user, "deal_recorded", payload.customer, workspace_id)
    db.commit()
    db.refresh(deal)
    return deal


@app.post("/api/deals/{deal_id}/revert")
def revert_deal(deal_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    workspace_id = require_workspace(user)
    deal = db.scalar(select(Deal).where(Deal.id == deal_id, Deal.workspace_id == workspace_id))
    if not deal:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Deal not found")
    if deal.lead_id:
        lead = get_workspace_lead(deal.lead_id, user, db)
        lead.pipeline_stage = "Negotiating"
        lead.follow_up_enabled = True
        db.add(LeadActivity(workspace_id=workspace_id, lead_id=lead.id, type="sale reverted", body="Recorded sale was reverted and the customer returned to Negotiating."))
    audit(db, user, "deal_reverted", deal.customer, workspace_id)
    db.delete(deal)
    db.commit()
    return {"reverted": True}


def list_records(record_type: str, user: User, db: Session) -> list[dict]:
    workspace_id = require_workspace(user)
    records = db.scalars(select(WorkspaceRecord).where(WorkspaceRecord.workspace_id == workspace_id, WorkspaceRecord.record_type == record_type).order_by(WorkspaceRecord.created_at.desc()))
    result = []
    for item in records:
        lead_id = item.payload.get("lead_id")
        if user.role == Role.SALESPERSON:
            if not lead_id:
                continue
            if not db.scalar(select(Lead.id).where(Lead.id == int(lead_id), Lead.workspace_id == workspace_id, Lead.assigned_user_id == user.id)):
                continue
        result.append({"id": item.id, **item.payload})
    return result


def create_record(record_type: str, payload: dict, user: User, db: Session) -> dict:
    workspace_id = require_workspace(user)
    if user.role == Role.SALESPERSON and record_type in {"followup", "appointment", "quote"}:
        lead_id = payload.get("lead_id")
        if not lead_id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Choose one of your assigned customers")
        get_workspace_lead(int(lead_id), user, db)
    record = WorkspaceRecord(workspace_id=workspace_id, record_type=record_type, payload=payload)
    db.add(record)
    db.flush()
    audit(db, user, f"{record_type}_created", "", workspace_id)
    db.commit()
    return {"id": record.id, **record.payload}


def update_record(record_type: str, record_id: int, payload: dict, user: User, db: Session) -> dict:
    workspace_id = require_workspace(user)
    record = db.scalar(select(WorkspaceRecord).where(WorkspaceRecord.id == record_id, WorkspaceRecord.workspace_id == workspace_id, WorkspaceRecord.record_type == record_type))
    if not record:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Record not found")
    if user.role == Role.SALESPERSON and record_type in {"followup", "appointment", "quote"}:
        lead_id = payload.get("lead_id", record.payload.get("lead_id"))
        if not lead_id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="This customer record is not assigned to you")
        get_workspace_lead(int(lead_id), user, db)
    record.payload = {**record.payload, **payload}
    audit(db, user, f"{record_type}_updated", "", workspace_id)
    db.commit()
    return {"id": record.id, **record.payload}


@app.get("/api/followups")
def get_followups(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> list[dict]:
    return list_records("followup", user, db)


@app.post("/api/followups")
def create_followup(payload: dict, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    if payload.get("lead_id"):
        payload = {**payload, "name": get_workspace_lead(int(payload["lead_id"]), user, db).name}
    return create_record("followup", {**payload, "status": payload.get("status", "Pending")}, user, db)


@app.patch("/api/followups/{record_id}")
def update_followup(record_id: int, payload: dict, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    return update_record("followup", record_id, payload, user, db)


@app.get("/api/appointments")
def get_appointments(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> list[dict]:
    return list_records("appointment", user, db)


@app.post("/api/appointments")
def create_appointment(payload: dict, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    if payload.get("lead_id"):
        payload = {**payload, "lead_name": get_workspace_lead(int(payload["lead_id"]), user, db).name}
    return create_record("appointment", {**payload, "status": payload.get("status", "Scheduled")}, user, db)


@app.patch("/api/appointments/{record_id}")
def update_appointment(record_id: int, payload: dict, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    return update_record("appointment", record_id, payload, user, db)


@app.get("/api/inventory")
def get_inventory(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> list[dict]:
    return list_records("inventory", user, db)


@app.post("/api/inventory")
def create_inventory(payload: dict, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    return create_record("inventory", {**payload, "status": payload.get("status", "Available")}, user, db)


@app.patch("/api/inventory/{record_id}")
def update_inventory(record_id: int, payload: dict, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    return update_record("inventory", record_id, payload, user, db)


@app.delete("/api/inventory/{record_id}")
def delete_inventory(record_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    workspace_id = require_workspace(user)
    record = db.scalar(select(WorkspaceRecord).where(WorkspaceRecord.id == record_id, WorkspaceRecord.workspace_id == workspace_id, WorkspaceRecord.record_type == "inventory"))
    if not record: raise HTTPException(status_code=404, detail="Record not found")
    db.delete(record); db.commit(); return {"deleted": True}


@app.get("/api/quotes")
def get_quotes(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> list[dict]:
    return list_records("quote", user, db)


@app.post("/api/quotes")
def create_quote(payload: dict, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    return create_record("quote", {**payload, "status": payload.get("status", "Draft")}, user, db)


@app.patch("/api/quotes/{record_id}")
def update_quote(record_id: int, payload: dict, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    return update_record("quote", record_id, payload, user, db)


@app.get("/api/settings")
def get_settings_record(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    workspace_id = require_workspace(user)
    record = db.scalar(select(WorkspaceRecord).where(WorkspaceRecord.workspace_id == workspace_id, WorkspaceRecord.record_type == "settings"))
    return record.payload if record else {"stages": ["New", "Contacted", "Appointment", "Demo", "Quote", "Negotiating", "Sold", "Lost"], "followup_hours": 48, "goals": {"units_goal": 10, "gross_goal": 50000, "appointments_goal": 20, "contacts_goal": 100}}


@app.put("/api/settings")
def put_settings_record(payload: dict, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    if user.role not in {Role.ADMIN, Role.DEVELOPER}: raise HTTPException(status_code=403, detail="Admin access required")
    workspace_id = require_workspace(user)
    record = db.scalar(select(WorkspaceRecord).where(WorkspaceRecord.workspace_id == workspace_id, WorkspaceRecord.record_type == "settings"))
    if record: record.payload = {**record.payload, **payload}
    else: record = WorkspaceRecord(workspace_id=workspace_id, record_type="settings", payload=payload); db.add(record)
    db.commit(); return record.payload


@app.get("/api/metrics")
def metrics(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    workspace_id = require_workspace(user)
    now = datetime.now(timezone.utc)
    leads = list(db.scalars(lead_query_for_user(user)))
    lead_ids = {lead.id for lead in leads}
    deals = list(db.scalars(select(Deal).where(Deal.workspace_id == workspace_id)))
    if user.role == Role.SALESPERSON:
        deals = [deal for deal in deals if deal.lead_id in lead_ids]
    appointments = list(db.scalars(select(WorkspaceRecord).where(WorkspaceRecord.workspace_id == workspace_id, WorkspaceRecord.record_type == "appointment")))
    followups = list(db.scalars(select(WorkspaceRecord).where(WorkspaceRecord.workspace_id == workspace_id, WorkspaceRecord.record_type == "followup")))
    month_deals = [deal for deal in deals if deal.sold_at and deal.sold_at.year == now.year and deal.sold_at.month == now.month]
    if user.role == Role.SALESPERSON:
        appointments = [item for item in appointments if str(item.payload.get("lead_id")) in {str(lead_id) for lead_id in lead_ids}]
        followups = [item for item in followups if str(item.payload.get("lead_id")) in {str(lead_id) for lead_id in lead_ids}]
    appointments_today = sum(1 for item in appointments if item.payload.get("status") == "Scheduled" and str(item.payload.get("starts_at", ""))[:10] == now.date().isoformat())
    overdue_followups = sum(1 for item in followups if item.payload.get("status") == "Pending" and str(item.payload.get("due_at", ""))[:10] < now.date().isoformat())
    return {
        "active_leads": sum(1 for lead in leads if lead.pipeline_stage not in {"Sold", "Lost"}),
        "month_units": len(month_deals),
        "month_gross": sum(deal.gross_profit for deal in month_deals),
        "month_sales_volume": sum(deal.sale_price for deal in month_deals),
        "month_appointments": sum(1 for item in appointments if item.payload.get("status") == "Scheduled"),
        "appointments_today": appointments_today,
        "month_contacts": len(leads),
        "overdue_followups": overdue_followups,
    }


@app.get("/api/team")
def team_overview(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    if user.role not in {Role.ADMIN, Role.DEVELOPER}:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")
    workspace_id = require_workspace(user)
    now = datetime.now(timezone.utc)
    members = list(db.scalars(select(User).where(User.workspace_id == workspace_id, User.active.is_(True)).order_by(User.email)))
    leads = list(db.scalars(select(Lead).where(Lead.workspace_id == workspace_id)))
    deals = list(db.scalars(select(Deal).where(Deal.workspace_id == workspace_id)))
    activities = list(db.scalars(select(LeadActivity).where(LeadActivity.workspace_id == workspace_id).order_by(LeadActivity.created_at.desc()).limit(8)))
    lead_names = {lead.id: lead.name for lead in leads}
    month_deals = [deal for deal in deals if deal.sold_at and deal.sold_at.year == now.year and deal.sold_at.month == now.month]
    rows = []
    for member in members:
        owned = [lead for lead in leads if lead.assigned_user_id == member.id]
        owned_ids = {lead.id for lead in owned}
        closed = [deal for deal in month_deals if deal.lead_id in owned_ids]
        rows.append({
            "id": member.id,
            "email": member.email,
            "role": member.role.value,
            "assigned_leads": len(owned),
            "active_leads": sum(1 for lead in owned if lead.pipeline_stage not in {"Sold", "Lost"}),
            "deals_sold": len(closed),
            "gross": sum(deal.gross_profit for deal in closed),
        })
    return {
        "members": rows,
        "unassigned": [{"id": lead.id, "name": lead.name, "pipeline_stage": lead.pipeline_stage, "equipment": lead.equipment} for lead in leads if lead.assigned_user_id is None and lead.pipeline_stage not in {"Sold", "Lost"}],
        "activity": [{"id": item.id, "lead_id": item.lead_id, "lead_name": lead_names.get(item.lead_id, "Customer"), "type": item.type, "body": item.body, "created_at": item.created_at} for item in activities],
    }


@app.get("/api/developer/workspaces", response_model=list[WorkspaceResponse])
def developer_workspaces(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> list[WorkspaceResponse]:
    if user.role != Role.DEVELOPER:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Developer access required")
    audit(db, user, "workspace_list_viewed", "Developer support workspace list")
    db.commit()
    return list(db.scalars(select(Workspace).order_by(Workspace.name)))


@app.post("/api/developer/workspaces/{workspace_id}/support-access")
def support_access(workspace_id: int, reason: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    if user.role != Role.DEVELOPER:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Developer access required")
    workspace = db.get(Workspace, workspace_id)
    if not workspace:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workspace not found")
    audit(db, user, "support_access_started", reason, workspace_id)
    db.commit()
    return {"workspace_id": workspace.id, "workspace_name": workspace.name, "support_access": True}


@app.get("/api/admin/export/users.csv")
def export_users(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> StreamingResponse:
    if user.role not in {Role.ADMIN, Role.DEVELOPER}:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")
    target_workspace_id = user.workspace_id
    if user.role == Role.DEVELOPER:
        target_workspace_id = user.workspace_id
    if target_workspace_id is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Choose a dealership workspace before exporting data")
    users = list(db.scalars(select(User).where(User.workspace_id == target_workspace_id).order_by(User.email)))
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(["email", "role", "active", "created_at"])
    for record in users:
        writer.writerow([record.email, record.role.value, record.active, record.created_at.isoformat() if record.created_at else ""])
    audit(db, user, "data_exported", "users.csv", target_workspace_id)
    db.commit()
    return StreamingResponse(iter([output.getvalue()]), media_type="text/csv", headers={"Content-Disposition": "attachment; filename=tractorcloser-users.csv"})
