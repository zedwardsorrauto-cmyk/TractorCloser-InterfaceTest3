import csv
import json
import os
import time
import zipfile
from base64 import b64encode
from datetime import datetime, timedelta, timezone
from io import BytesIO, StringIO
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload
from openai import OpenAI

from .config import get_settings
from .database import Base, SessionLocal, engine, get_db
from .migrations import current_schema_version, run_schema_migrations
from .models import AuditEvent, Deal, Lead, LeadActivity, Role, SystemSetting, User, Workspace, WorkspaceRecord
from .schemas import ActivityCreate, ActivityResponse, ClosingCoachRequest, ClosingCoachResponse, DealCreate, DealResponse, LeadAssignmentUpdate, LeadCreate, LeadPipelineUpdate, LeadProfileUpdate, LeadResponse, LoginRequest, LoginResponse, ManagerBriefResponse, PasswordSetupRequest, SalesManagerRequest, SalesManagerResponse, UserResponse, WorkspaceResponse, WorkspaceUserCreate, WorkspaceUserUpdate
from .security import create_access_token, get_current_user, hash_password, verify_password

app = FastAPI(title="TractorCloser API", version="0.1.0")
settings = get_settings()
allowed_origins = [origin.strip() for origin in settings.allowed_origins.split(",") if origin.strip()] or ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


def user_response(user: User) -> UserResponse:
    workspace = getattr(user, "support_workspace", None) or user.workspace
    return UserResponse(
        id=user.id,
        email=user.email,
        role=user.role,
        workspace_id=getattr(user, "support_workspace_id", None) or user.workspace_id,
        workspace_name=workspace.name if workspace else None,
        timezone=workspace.timezone if workspace else None,
        must_change_password=user.must_change_password,
    )


def audit(db: Session, actor: User | None, event_type: str, detail: str = "", workspace_id: int | None = None) -> None:
    db.add(AuditEvent(actor_user_id=actor.id if actor else None, workspace_id=workspace_id or (actor.workspace_id if actor else None), event_type=event_type, detail=detail))


def run_openai_coaching(instructions: str, context: dict, settings) -> str:
    try:
        response = OpenAI(api_key=settings.openai_api_key).responses.create(
            model=settings.openai_model,
            instructions=instructions,
            input=json.dumps(context),
            max_output_tokens=700,
            store=False,
        )
        return (response.output_text or "").strip()
    except Exception:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Sales Manager could not complete that request. Please try again.")


def json_coaching_output(raw: str) -> dict:
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[-1]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Sales Manager returned an unreadable brief. Please try again.")


def seed_initial_workspace() -> None:
    Base.metadata.create_all(bind=engine)
    run_schema_migrations(engine)
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
        # Add a focused set of safe composer test records once, without touching
        # any customer records the team has already created during testing.
        composer_test_reference = "Composer interface test set"
        if not db.scalar(select(Lead).where(Lead.workspace_id == workspace.id, Lead.source_reference == composer_test_reference)):
            composer_leads = [
                ("Avery Collins (Test)", "555-0241", "", "2025 Yanmar SA325", 28500, "New", "Website", "I need a compact tractor for five acres. Can someone text me pricing today?"),
                ("Emery Price (Test)", "", "emery.price.test@example.com", "Yanmar YM347", 41500, "Contacted", "Website", "Please send package details and financing information by email."),
                ("Noah Bennett (Test)", "555-0243", "", "Yanmar UTV", 21800, "New", "Facebook Marketplace", "Is the UTV still available? I would like to message about a trade."),
                ("Quinn Harper (Test)", "555-0244", "", "", 0, "New", "Missed call", "Missed call from the business line. No voicemail was left."),
                ("Unknown walk-in (Test)", "", "", "", 0, "New", "In-person", "Stopped by asking about compact tractor options; no contact details collected yet."),
            ]
            for name, phone, email, equipment, budget, stage, source, inquiry in composer_leads:
                lead = Lead(
                    workspace_id=workspace.id,
                    assigned_user_id=salesperson.id if salesperson else None,
                    name=name,
                    phone=phone,
                    email=email,
                    equipment=equipment,
                    budget=budget,
                    pipeline_stage=stage,
                    source=source,
                    source_reference=composer_test_reference,
                    contact_consent="Granted" if name in {"Avery Collins (Test)", "Emery Price (Test)", "Noah Bennett (Test)"} else "Unknown",
                    preferred_contact_channel="Text" if name == "Avery Collins (Test)" else "Email" if name == "Emery Price (Test)" else "Social" if name == "Noah Bennett (Test)" else "",
                    original_inquiry=inquiry,
                    is_test_data=True,
                )
                db.add(lead)
                db.flush()
                db.add(LeadActivity(workspace_id=workspace.id, lead_id=lead.id, type="incoming inquiry", body=inquiry))
        if not db.scalar(select(WorkspaceRecord).where(WorkspaceRecord.workspace_id == workspace.id, WorkspaceRecord.record_type == "intake")):
            intake_samples = [
                {"name": "", "phone": "555-0201", "email": "", "source": "Missed call", "source_reference": "Business line", "message": "Missed call at 10:42 AM. No voicemail.", "equipment": "", "classification": "Needs review", "confidence": "Low", "status": "Pending"},
                {"name": "Jamie Reed", "phone": "", "email": "jamie.reed@example.com", "source": "Website", "source_reference": "Request information form", "message": "Looking for a compact tractor with a loader for 12 acres. Please email pricing.", "equipment": "Compact tractor with loader", "classification": "Likely new lead", "confidence": "High", "status": "Pending"},
                {"name": "Jordan Miller", "phone": "555-0142", "email": "", "source": "Marketplace", "source_reference": "Yanmar YM347 listing", "message": "Is this still available? What would payments look like?", "equipment": "2025 Yanmar YM347", "classification": "Possible existing customer", "confidence": "Medium", "status": "Pending"},
                {"name": "Parts Department", "phone": "", "email": "parts@tractorbob.com", "source": "Business email", "source_reference": "Shared inbox", "message": "Can you send the updated internal parts schedule?", "equipment": "", "classification": "Likely non-sales", "confidence": "High", "status": "Pending"},
            ]
            db.add_all([WorkspaceRecord(workspace_id=workspace.id, record_type="intake", payload=sample) for sample in intake_samples])
        # Enrich only the marked test records so the AI workspace demo has
        # realistic context without touching any customer-created data.
        if not db.scalar(select(WorkspaceRecord).where(WorkspaceRecord.workspace_id == workspace.id, WorkspaceRecord.record_type == "ai_test_context")):
            context_by_name = {
                "Jordan Miller (Test)": [
                    ("call note", "Customer wants to compare a trade-in value before deciding. Asked about a 60-month option."),
                    ("quote viewed", "Opened the YM347 quote yesterday afternoon; no response since."),
                ],
                "Casey Rodriguez (Test)": [
                    ("appointment", "On-site demo is scheduled for Friday at 10:30 AM. Customer is bringing spouse."),
                ],
                "Morgan Lee (Test)": [
                    ("quote sent", "Compact tractor package quote sent with loader, delivery, and warranty options."),
                    ("note", "Primary concern is staying under a comfortable monthly payment."),
                ],
                "Noah Bennett (Test)": [
                    ("incoming inquiry", "Marketplace lead asked whether the Yanmar UTV is still available and mentioned a possible trade."),
                ],
                "Avery Collins (Test)": [
                    ("incoming inquiry", "Needs a compact tractor for five acres. Requested pricing by text today."),
                ],
            }
            for lead_name, activities in context_by_name.items():
                lead = db.scalar(select(Lead).where(Lead.workspace_id == workspace.id, Lead.name == lead_name, Lead.is_test_data.is_(True)))
                if lead:
                    for activity_type, body in activities:
                        db.add(LeadActivity(workspace_id=workspace.id, lead_id=lead.id, type=activity_type, body=body))
            db.add(WorkspaceRecord(workspace_id=workspace.id, record_type="ai_test_context", payload={"seeded": True}))
        # Recover the salesperson for older linked deals when the lead still
        # has an assigned owner. Unknown legacy walk-ins remain clearly labeled.
        for deal in db.scalars(select(Deal).where(Deal.workspace_id == workspace.id, Deal.sold_by_user_id.is_(None), Deal.lead_id.is_not(None))):
            lead = db.get(Lead, deal.lead_id)
            if lead and lead.assigned_user_id:
                deal.sold_by_user_id = lead.assigned_user_id
        db.commit()
    finally:
        db.close()


@app.on_event("startup")
def startup() -> None:
    seed_initial_workspace()


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "environment": settings.app_environment, "schema_version": current_schema_version(engine)}


@app.post("/api/auth/login", response_model=LoginResponse)
def login(payload: LoginRequest, db: Session = Depends(get_db)) -> LoginResponse:
    user = db.scalar(select(User).options(joinedload(User.workspace)).where(User.email == str(payload.email).lower()))
    if not user or not user.active or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password")
    security = db.get(SystemSetting, "security")
    if security and not security.payload.get("sign_in_enabled", True) and user.role != Role.DEVELOPER:
        raise HTTPException(status_code=status.HTTP_423_LOCKED, detail="Sign-in is temporarily disabled by Developer security controls")
    audit(db, user, "developer_login" if user.role == Role.DEVELOPER else "login")
    db.commit()
    return LoginResponse(access_token=create_access_token(user), user=user_response(user))


@app.get("/api/me", response_model=UserResponse)
def me(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> UserResponse:
    db.refresh(user)
    return user_response(user)


def require_workspace(user: User) -> int:
    if user.must_change_password:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Set your new password before accessing the workspace")
    workspace_id = getattr(user, "support_workspace_id", None) or user.workspace_id
    if workspace_id is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Select a dealership workspace before accessing CRM data")
    return workspace_id


def ideal_connection_ready() -> tuple[bool, str]:
    required = {
        "IDEAL_API_BASE_URL": settings.ideal_api_base_url,
        "IDEAL_API_USERNAME": settings.ideal_api_username,
        "IDEAL_API_PASSWORD": settings.ideal_api_password,
        "IDEAL_COMPANY_ID": settings.ideal_company_id,
        "IDEAL_LOCATION_ID": settings.ideal_location_id,
    }
    missing = [name for name, value in required.items() if not value.strip()]
    if missing:
        return False, "Ideal is not configured yet. Add the required Ideal settings in Render before testing."
    if not settings.ideal_api_base_url.lower().startswith("https://"):
        return False, "Ideal must provide an HTTPS endpoint before TractorCloser sends Basic Authentication over the internet."
    return True, "Ready"


def test_ideal_inventory_connection() -> dict:
    ready, message = ideal_connection_ready()
    if not ready:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=message)
    endpoint = f"{settings.ideal_api_base_url.rstrip('/')}/Api/Inventory/{settings.ideal_company_id}/Unit"
    if settings.ideal_api_test_stock_number.strip():
        endpoint += f"/{settings.ideal_api_test_stock_number.strip()}"
    query = urlencode({"LocationID": settings.ideal_location_id.strip(), "PerPage": 1, "Page": 1, "UnitStatus": "I"})
    credentials = b64encode(f"{settings.ideal_api_username}:{settings.ideal_api_password}".encode()).decode()
    request = Request(f"{endpoint}?{query}", headers={"Authorization": f"Basic {credentials}", "Accept": "application/json"}, method="GET")
    started = time.monotonic()
    try:
        with urlopen(request, timeout=12) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Ideal rejected the read-only test with HTTP {error.code}.")
    except URLError:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="TractorCloser could not reach Ideal. Confirm Ideal's HTTPS endpoint and network access.")
    except (TimeoutError, ValueError, json.JSONDecodeError):
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Ideal returned an unreadable response. No data was changed.")
    units = payload.get("Units", []) if isinstance(payload, dict) else []
    return {"status": "Connected", "detail": "Read-only inventory lookup completed. No Ideal records were changed.", "latency_ms": round((time.monotonic() - started) * 1000), "result_count": len(units)}


@app.post("/api/auth/set-password", response_model=LoginResponse)
def set_initial_password(payload: PasswordSetupRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> LoginResponse:
    if len(payload.password) < 10:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Use a password of at least 10 characters")
    user.password_hash = hash_password(payload.password)
    user.must_change_password = False
    user.session_version = int(user.session_version or 1) + 1
    audit(db, user, "password_setup_completed", user.email, user.workspace_id)
    db.commit()
    db.refresh(user)
    return LoginResponse(access_token=create_access_token(user), user=user_response(user))


def lead_query_for_user(user: User):
    statement = select(Lead).where(Lead.workspace_id == require_workspace(user))
    if user.role == Role.SALESPERSON:
        statement = statement.where(Lead.assigned_user_id == user.id)
    return statement


@app.post("/api/leads/{lead_id}/sales-manager", response_model=SalesManagerResponse)
def ask_sales_manager(lead_id: int, payload: SalesManagerRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> SalesManagerResponse:
    """Return on-demand coaching from CRM context only; it never changes CRM data."""
    settings = get_settings()
    if not settings.openai_api_key:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Sales Manager is not configured yet")

    lead = db.scalar(lead_query_for_user(user).where(Lead.id == lead_id))
    if not lead:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Customer not found")

    activities = list(db.scalars(
        select(LeadActivity)
        .where(LeadActivity.workspace_id == require_workspace(user), LeadActivity.lead_id == lead.id)
        .order_by(LeadActivity.created_at.desc())
        .limit(12)
    ))
    context = {
        "customer": {
            "name": lead.name,
            "stage": lead.pipeline_stage,
            "interested_product": lead.equipment,
            "source": lead.source,
            "first_inquiry": lead.original_inquiry,
            "budget": lead.budget,
            "follow_up_enabled": lead.follow_up_enabled,
        },
        "recent_activity": [
            {"type": activity.type, "note": activity.body, "created_at": activity.created_at.isoformat() if activity.created_at else ""}
            for activity in activities
        ],
        "salesperson_question": payload.question.strip(),
    }
    instructions = """You are TractorCloser Sales Manager: a direct, practical dealership sales coach.
Use only the CRM context supplied. Do not claim to have contacted the customer, researched the web, or completed any CRM action.
Never draft a customer response unless the salesperson specifically asks for one. Be concise and specific.
Give: (1) the highest-priority next move, (2) why it matters, (3) a short suggested activity or follow-up to confirm. Do not be overly friendly or generic."""
    try:
        client = OpenAI(api_key=settings.openai_api_key)
        response = client.responses.create(
            model=settings.openai_model,
            instructions=instructions,
            input=json.dumps(context),
            max_output_tokens=420,
            store=False,
        )
        advice = (response.output_text or "").strip()
    except Exception:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Sales Manager could not complete that request. Please try again.")
    if not advice:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Sales Manager returned no recommendation. Please try again.")
    audit(db, user, "sales_manager_requested", f"Customer {lead.id}", require_workspace(user))
    db.commit()
    return SalesManagerResponse(advice=advice)


@app.post("/api/ai", response_model=ClosingCoachResponse)
def closing_coach(payload: ClosingCoachRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> ClosingCoachResponse:
    """On-demand coaching for the AI page. No customer record is modified."""
    settings = get_settings()
    if not settings.openai_api_key:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Sales Manager is not configured yet")
    details = payload.details.strip()
    if not details:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Describe the sales situation first")
    instructions = """You are TractorCloser Sales Manager, a direct and practical dealership sales coach.
Answer the salesperson's specific situation with concise guidance: acknowledge the real concern, identify the best next move, give one or two useful phrases to use verbally, and name a fallback approach.
Do not claim you searched the web or accessed systems outside the supplied information. Do not draft a full customer message unless the salesperson explicitly asks for one."""
    result = run_openai_coaching(instructions, {"request_type": payload.type, "salesperson_situation": details}, settings)
    if not result:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Sales Manager returned no coaching. Please try again.")
    audit(db, user, "closing_coach_requested", payload.type, require_workspace(user))
    db.commit()
    return ClosingCoachResponse(result=result)


@app.post("/api/ai/sales-manager", response_model=ManagerBriefResponse)
def sales_manager_brief(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> ManagerBriefResponse:
    """Generate a concise, role-scoped manager brief from the current CRM workspace."""
    settings = get_settings()
    if not settings.openai_api_key:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Sales Manager is not configured yet")
    workspace_id = require_workspace(user)
    leads = list(db.scalars(lead_query_for_user(user).order_by(Lead.updated_at.desc()).limit(60)))
    lead_ids = [lead.id for lead in leads]
    activities = []
    if lead_ids:
        activities = list(db.scalars(
            select(LeadActivity).where(LeadActivity.workspace_id == workspace_id, LeadActivity.lead_id.in_(lead_ids))
            .order_by(LeadActivity.created_at.desc()).limit(40)
        ))
    deals = list(db.scalars(select(Deal).where(Deal.workspace_id == workspace_id).order_by(Deal.sold_at.desc()).limit(20)))
    records = list(db.scalars(select(WorkspaceRecord).where(WorkspaceRecord.workspace_id == workspace_id, WorkspaceRecord.record_type.in_(["followup", "appointment"])).order_by(WorkspaceRecord.updated_at.desc()).limit(30)))
    lead_context = [
        {"id": lead.id, "name": lead.name, "stage": lead.pipeline_stage, "product": lead.equipment, "source": lead.source, "budget": lead.budget, "follow_up_enabled": lead.follow_up_enabled, "response_sent": lead.response_sent}
        for lead in leads
    ]
    context = {
        "workspace_scope": "team" if user.role in {Role.ADMIN, Role.DEVELOPER} else "assigned customers only",
        "leads": lead_context,
        "recent_activities": [
            {"lead_id": activity.lead_id, "type": activity.type, "note": activity.body, "created_at": activity.created_at.isoformat() if activity.created_at else ""}
            for activity in activities
        ],
        "recent_deals": [{"customer": deal.customer, "equipment": deal.equipment, "gross": deal.gross_profit, "sold_at": deal.sold_at.isoformat() if deal.sold_at else ""} for deal in deals],
        "open_followups_and_appointments": [record.payload for record in records],
    }
    instructions = """You are TractorCloser Sales Manager: direct, concise, and commercially practical.
Review only the supplied CRM workspace context. Do not invent facts, claim to contact customers, or take actions.
Return valid JSON only, with this exact shape:
{"headline":"short heading","summary":"2-3 sentence overview","priorities":[{"title":"short action","reason":"why now","next_action":"specific next step"}],"risks":["risk"],"coaching":"one concise coaching observation"}
Include 3 to 5 priorities, ordered most important first. Focus on stalled opportunities, unanswered inquiries, imminent appointments, and follow-ups. Never include a response draft unless explicitly requested."""
    raw = run_openai_coaching(instructions, context, settings)
    brief = json_coaching_output(raw)
    try:
        result = ManagerBriefResponse(**brief)
    except Exception:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Sales Manager returned an incomplete brief. Please try again.")
    audit(db, user, "manager_brief_requested", f"{len(leads)} customers analyzed", workspace_id)
    db.commit()
    return result


@app.get("/api/leads", response_model=list[LeadResponse])
def list_leads(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> list[Lead]:
    return list(db.scalars(lead_query_for_user(user).order_by(Lead.created_at.desc())))


@app.post("/api/leads", response_model=LeadResponse, status_code=status.HTTP_201_CREATED)
def create_lead(payload: LeadCreate, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> Lead:
    workspace_id = require_workspace(user)
    payload.name = payload.name.strip()
    if not payload.name:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Customer name cannot be blank")
    payload.phone = payload.phone.strip()
    payload.email = payload.email.strip()
    payload.equipment = payload.equipment.strip()
    payload.budget = max(0, int(payload.budget or 0))
    if user.role == Role.SALESPERSON:
        payload.assigned_user_id = user.id
    if payload.assigned_user_id is not None:
        assignee = db.scalar(select(User).where(User.id == payload.assigned_user_id, User.workspace_id == workspace_id, User.active.is_(True)))
        if not assignee:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Team member not found in this workspace")
    lead = Lead(workspace_id=workspace_id, **payload.model_dump())
    db.add(lead)
    db.flush()
    db.add(LeadActivity(workspace_id=workspace_id, lead_id=lead.id, actor_user_id=user.id, type="lead created", body="Customer profile created."))
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
        db.add(LeadActivity(workspace_id=lead.workspace_id, lead_id=lead.id, actor_user_id=user.id, type="stage changed", body=f"Moved from {previous_stage} to {lead.pipeline_stage}."))
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
    db.add(LeadActivity(workspace_id=lead.workspace_id, lead_id=lead.id, actor_user_id=user.id, type="lead assigned", body=detail))
    audit(db, user, "lead_assignment_changed", detail, lead.workspace_id)
    db.commit()
    db.refresh(lead)
    return lead


@app.patch("/api/leads/{lead_id}", response_model=LeadResponse)
def update_lead_profile(lead_id: int, payload: LeadProfileUpdate, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> Lead:
    lead = get_workspace_lead(lead_id, user, db)
    updates = payload.model_dump(exclude_none=True)
    if "name" in updates and not updates["name"].strip():
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Customer name cannot be blank")
    changed = []
    for field, value in updates.items():
        value = value.strip()
        if getattr(lead, field) != value:
            setattr(lead, field, value)
            changed.append(field.replace("_", " "))
    if changed:
        detail = "Updated " + ", ".join(changed) + "."
        db.add(LeadActivity(workspace_id=lead.workspace_id, lead_id=lead.id, actor_user_id=user.id, type="customer details updated", body=detail))
        audit(db, user, "lead_profile_updated", f"{lead.name}: {', '.join(changed)}", lead.workspace_id)
    db.commit()
    db.refresh(lead)
    return lead


@app.get("/api/admin/integrations")
def integration_health(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    workspace_id = require_admin(user)
    configured = {
        record.payload.get("key"): record.payload
        for record in db.scalars(select(WorkspaceRecord).where(WorkspaceRecord.workspace_id == workspace_id, WorkspaceRecord.record_type == "integration_status"))
    }


    defaults = [
        ("website", "Website forms", "Ready for a signed webhook"),
        ("messaging", "Text, email & social messaging", "Ready for a provider connection"),
        ("inventory", "Inventory and DMS", "Ready for a catalog connection"),
    ]
    providers = []
    for key, name, detail in defaults:
        saved = configured.get(key, {})
        providers.append({"key": key, "name": name, "status": saved.get("status", "Not connected"), "detail": saved.get("detail", detail), "last_sync": saved.get("last_sync")})
    return {
        "testing_mode": not bool(settings.allowed_origins),
        "intake_enabled": settings.integration_intake_enabled,
        "environment": settings.app_environment,
        "schema_version": current_schema_version(engine),
        "providers": providers,
        "rules": [
            "Incoming records remain in Intake until an authorized user accepts or matches them.",
            "Outbound messaging stays in test mode until a provider is connected and customer permissions are confirmed.",
            "Customer records retain the source and external reference needed for duplicate review and traceability.",
        ],
    }


@app.post("/api/developer/integrations/ideal/test")
def test_ideal_connection(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    if user.role != Role.DEVELOPER:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Developer access required")
    workspace_id = require_workspace(user)
    result = test_ideal_inventory_connection()
    audit(db, user, "ideal_connection_tested", "Read-only inventory lookup completed", workspace_id)
    db.commit()
    return result


@app.get("/api/leads/{lead_id}/activities", response_model=list[ActivityResponse])
def list_lead_activities(lead_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> list[LeadActivity]:
    lead = get_workspace_lead(lead_id, user, db)
    return list(db.scalars(select(LeadActivity).where(LeadActivity.lead_id == lead.id, LeadActivity.workspace_id == lead.workspace_id).order_by(LeadActivity.created_at.desc())))


@app.post("/api/leads/{lead_id}/activities", response_model=ActivityResponse, status_code=status.HTTP_201_CREATED)
def create_lead_activity(lead_id: int, payload: ActivityCreate, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> LeadActivity:
    lead = get_workspace_lead(lead_id, user, db)
    activity = LeadActivity(workspace_id=lead.workspace_id, lead_id=lead.id, actor_user_id=user.id, **payload.model_dump())
    if (any(channel in activity.type.lower() for channel in ("text message", "email", "social reply")) and "sent" in activity.type.lower() and "draft" not in activity.type.lower()) or "callback" in activity.type.lower():
        lead.response_sent = True
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
        if existing.sold_by_user_id is None:
            existing.sold_by_user_id = user.id
        deal = existing
    else:
        deal = Deal(workspace_id=workspace_id, sold_by_user_id=user.id, **payload.model_dump())
        db.add(deal)
        db.flush()
    if lead:
        db.add(LeadActivity(workspace_id=workspace_id, lead_id=lead.id, actor_user_id=user.id, type="deal recorded", body=f"Sold for ${payload.sale_price:,} with ${payload.gross_profit:,} total gross."))
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
        db.add(LeadActivity(workspace_id=workspace_id, lead_id=lead.id, actor_user_id=user.id, type="sale reverted", body="Recorded sale was reverted and the customer returned to Negotiating."))
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


def get_intake_record(record_id: int, user: User, db: Session) -> WorkspaceRecord:
    workspace_id = require_admin(user)
    record = db.scalar(select(WorkspaceRecord).where(WorkspaceRecord.id == record_id, WorkspaceRecord.workspace_id == workspace_id, WorkspaceRecord.record_type == "intake"))
    if not record:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Intake item not found")
    return record


@app.get("/api/intake")
def get_intake(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> list[dict]:
    require_admin(user)
    return list_records("intake", user, db)


@app.patch("/api/intake/{record_id}")
def update_intake(record_id: int, payload: dict, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    get_intake_record(record_id, user, db)
    return update_record("intake", record_id, payload, user, db)


@app.post("/api/intake/{record_id}/create-lead", response_model=LeadResponse)
def intake_to_lead(record_id: int, payload: dict, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> Lead:
    record = get_intake_record(record_id, user, db)
    workspace_id = require_workspace(user)
    data = record.payload
    name = str(payload.get("name") or data.get("name") or f"New {data.get('source', 'inquiry')} inquiry").strip()
    lead = Lead(workspace_id=workspace_id, name=name, phone=str(data.get("phone", "")), email=str(data.get("email", "")), equipment=str(data.get("equipment", "")), source=str(data.get("source", "Manual")), source_reference=str(data.get("source_reference", "")), original_inquiry=str(data.get("message", "")), assigned_user_id=payload.get("assigned_user_id"), is_test_data=True)
    if lead.assigned_user_id is not None:
        assignee = db.scalar(select(User).where(User.id == lead.assigned_user_id, User.workspace_id == workspace_id, User.active.is_(True)))
        if not assignee:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Choose an active teammate")
    db.add(lead)
    db.flush()
    db.add(LeadActivity(workspace_id=workspace_id, lead_id=lead.id, actor_user_id=user.id, type="lead accepted from intake", body=f"Accepted from {lead.source}. {lead.original_inquiry}"))
    record.payload = {**record.payload, "status": "Created lead", "lead_id": lead.id}
    audit(db, user, "intake_lead_created", lead.name, workspace_id)
    db.commit()
    db.refresh(lead)
    return lead


@app.post("/api/intake/{record_id}/attach/{lead_id}")
def attach_intake_to_lead(record_id: int, lead_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    record = get_intake_record(record_id, user, db)
    lead = get_workspace_lead(lead_id, user, db)
    record.payload = {**record.payload, "status": "Attached to existing", "lead_id": lead.id}
    db.add(LeadActivity(workspace_id=lead.workspace_id, lead_id=lead.id, actor_user_id=user.id, type="intake attached", body=f"{record.payload.get('source', 'Incoming')} inquiry attached: {record.payload.get('message', '')}"))
    audit(db, user, "intake_attached_to_lead", lead.name, lead.workspace_id)
    db.commit()
    return {"id": record.id, **record.payload}


@app.get("/api/followups")
def get_followups(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> list[dict]:
    workspace_id = require_workspace(user)
    existing_records = list(db.scalars(select(WorkspaceRecord).where(WorkspaceRecord.workspace_id == workspace_id, WorkspaceRecord.record_type == "followup")))
    existing_lead_ids = {str(record.payload.get("lead_id")) for record in existing_records if record.payload.get("lead_id") is not None}
    scheduled = False
    for lead in db.scalars(lead_query_for_user(user)):
        if lead.follow_up_enabled and lead.pipeline_stage not in {"Sold", "Lost"} and str(lead.id) not in existing_lead_ids:
            db.add(WorkspaceRecord(workspace_id=workspace_id, record_type="followup", payload={"lead_id": lead.id, "name": lead.name, "due_at": (datetime.now(timezone.utc) + timedelta(hours=48)).isoformat(), "notes": "Initial pipeline follow-up", "status": "Pending"}))
            scheduled = True
    if scheduled:
        db.commit()
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
    workspace = db.get(Workspace, workspace_id)
    try:
        local_zone = ZoneInfo(workspace.timezone if workspace else "America/Chicago")
    except Exception:
        local_zone = timezone.utc
    now = datetime.now(local_zone)
    leads = list(db.scalars(lead_query_for_user(user)))
    lead_ids = {lead.id for lead in leads}
    deals = list(db.scalars(select(Deal).where(Deal.workspace_id == workspace_id)))
    if user.role == Role.SALESPERSON:
        deals = [deal for deal in deals if deal.lead_id in lead_ids]
    appointments = list(db.scalars(select(WorkspaceRecord).where(WorkspaceRecord.workspace_id == workspace_id, WorkspaceRecord.record_type == "appointment")))
    followups = list(db.scalars(select(WorkspaceRecord).where(WorkspaceRecord.workspace_id == workspace_id, WorkspaceRecord.record_type == "followup")))
    def local_deal_time(deal: Deal) -> datetime | None:
        if not deal.sold_at:
            return None
        value = deal.sold_at
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(local_zone)
    def to_local(value: datetime | None) -> datetime | None:
        if not value:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(local_zone)
    def scheduled_in_current_month(item: WorkspaceRecord) -> bool:
        try:
            starts_at = datetime.fromisoformat(str(item.payload.get("starts_at", "")).replace("Z", "+00:00"))
        except ValueError:
            return False
        starts_at = starts_at.replace(tzinfo=local_zone) if starts_at.tzinfo is None else starts_at.astimezone(local_zone)
        return starts_at.year == now.year and starts_at.month == now.month
    month_deals = [deal for deal in deals if (sold_at := local_deal_time(deal)) and sold_at.year == now.year and sold_at.month == now.month]
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
        "month_appointments": sum(1 for item in appointments if item.payload.get("status") == "Scheduled" and scheduled_in_current_month(item)),
        "appointments_today": appointments_today,
        "month_contacts": sum(1 for lead in leads if (created_at := to_local(lead.created_at)) and created_at.year == now.year and created_at.month == now.month),
        "overdue_followups": overdue_followups,
    }


@app.get("/api/team")
def team_overview(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    if user.role not in {Role.ADMIN, Role.DEVELOPER}:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")
    workspace_id = require_workspace(user)
    workspace = db.get(Workspace, workspace_id)
    try:
        local_zone = ZoneInfo(workspace.timezone if workspace else "America/Chicago")
    except Exception:
        local_zone = timezone.utc
    now = datetime.now(local_zone)
    members = list(db.scalars(select(User).where(User.workspace_id == workspace_id, User.active.is_(True)).order_by(User.email)))
    leads = list(db.scalars(select(Lead).where(Lead.workspace_id == workspace_id)))
    deals = list(db.scalars(select(Deal).where(Deal.workspace_id == workspace_id)))
    activities = list(db.scalars(select(LeadActivity).where(LeadActivity.workspace_id == workspace_id).order_by(LeadActivity.created_at.desc()).limit(8)))
    lead_names = {lead.id: lead.name for lead in leads}
    actor_ids = {item.actor_user_id for item in activities if item.actor_user_id is not None}
    activity_users = list(db.scalars(select(User).where(User.id.in_(actor_ids)))) if actor_ids else []
    user_names = {member.id: member.email for member in activity_users}
    def local_deal_time(deal: Deal) -> datetime | None:
        if not deal.sold_at:
            return None
        value = deal.sold_at.replace(tzinfo=timezone.utc) if deal.sold_at.tzinfo is None else deal.sold_at
        return value.astimezone(local_zone)
    month_deals = [deal for deal in deals if (sold_at := local_deal_time(deal)) and sold_at.year == now.year and sold_at.month == now.month]
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
        "activity": [{"id": item.id, "lead_id": item.lead_id, "lead_name": lead_names.get(item.lead_id, "Customer"), "type": item.type, "body": item.body, "actor_email": user_names.get(item.actor_user_id, "System"), "created_at": item.created_at} for item in activities],
    }


def require_admin(user: User) -> int:
    if user.role not in {Role.ADMIN, Role.DEVELOPER}:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")
    return require_workspace(user)


@app.get("/api/admin/users")
def list_workspace_users(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> list[dict]:
    workspace_id = require_admin(user)
    users = list(db.scalars(select(User).where(User.workspace_id == workspace_id).order_by(User.email)))
    leads = list(db.scalars(select(Lead).where(Lead.workspace_id == workspace_id)))
    return [{"id": member.id, "email": member.email, "role": member.role.value, "active": member.active, "assigned_leads": sum(1 for lead in leads if lead.assigned_user_id == member.id), "open_leads": sum(1 for lead in leads if lead.assigned_user_id == member.id and lead.pipeline_stage not in {"Sold", "Lost"})} for member in users]


@app.post("/api/admin/users", status_code=status.HTTP_201_CREATED)
def create_workspace_user(payload: WorkspaceUserCreate, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    workspace_id = require_admin(user)
    if len(payload.password) < 10:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Use a temporary password of at least 10 characters")
    email = str(payload.email).lower()
    if db.scalar(select(User).where(User.email == email)):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="An account already exists for that email")
    member = User(workspace_id=workspace_id, email=email, password_hash=hash_password(payload.password), role=Role.SALESPERSON, must_change_password=True)
    db.add(member)
    db.flush()
    audit(db, user, "salesperson_account_created", email, workspace_id)
    db.commit()
    return {"id": member.id, "email": member.email, "role": member.role.value, "active": member.active}


@app.patch("/api/admin/users/{member_id}")
def update_workspace_user(member_id: int, payload: WorkspaceUserUpdate, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    workspace_id = require_admin(user)
    member = db.scalar(select(User).where(User.id == member_id, User.workspace_id == workspace_id))
    if not member:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Team member not found")
    if member.role != Role.SALESPERSON:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only salesperson accounts can be managed here")
    if payload.password is not None:
        if len(payload.password) < 10:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Use a temporary password of at least 10 characters")
        member.password_hash = hash_password(payload.password)
        member.must_change_password = True
        member.session_version = int(member.session_version or 1) + 1
        audit(db, user, "salesperson_password_reset", member.email, workspace_id)
    if payload.active is not None and member.active != payload.active:
        if not payload.active:
            open_leads = list(db.scalars(select(Lead).where(Lead.workspace_id == workspace_id, Lead.assigned_user_id == member.id, Lead.pipeline_stage.not_in({"Sold", "Lost"}))))
            if open_leads:
                if not payload.reassign_to_user_id:
                    raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Reassign this salesperson’s open leads before deactivating the account")
                recipient = db.scalar(select(User).where(User.id == payload.reassign_to_user_id, User.workspace_id == workspace_id, User.active.is_(True)))
                if not recipient or recipient.id == member.id:
                    raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Choose an active teammate to receive open leads")
                for lead in open_leads:
                    lead.assigned_user_id = recipient.id
                audit(db, user, "salesperson_leads_reassigned", f"{member.email} → {recipient.email}", workspace_id)
            member.active = False
            member.session_version = int(member.session_version or 1) + 1
            audit(db, user, "salesperson_account_deactivated", member.email, workspace_id)
        else:
            member.active = True
            audit(db, user, "salesperson_account_reactivated", member.email, workspace_id)
    db.commit()
    return {"id": member.id, "email": member.email, "active": member.active}


@app.get("/api/developer/workspaces", response_model=list[WorkspaceResponse])
def developer_workspaces(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> list[WorkspaceResponse]:
    if user.role != Role.DEVELOPER:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Developer access required")
    audit(db, user, "workspace_list_viewed", "Developer support workspace list")
    db.commit()
    return list(db.scalars(select(Workspace).order_by(Workspace.name)))


def developer_audit_rows(db: Session) -> list[dict]:
    developers = list(db.scalars(select(User).where(User.role == Role.DEVELOPER)))
    developer_ids = {developer.id for developer in developers}
    events = list(db.scalars(select(AuditEvent).where(AuditEvent.actor_user_id.in_(developer_ids)).order_by(AuditEvent.created_at.desc()).limit(500))) if developer_ids else []
    actor_ids = {event.actor_user_id for event in events if event.actor_user_id is not None}
    workspace_ids = {event.workspace_id for event in events if event.workspace_id is not None}
    users = {record.id: record.email for record in developers if record.id in actor_ids}
    workspaces = {record.id: record.name for record in db.scalars(select(Workspace).where(Workspace.id.in_(workspace_ids)))} if workspace_ids else {}
    return [{
        "id": event.id,
        "timestamp": event.created_at,
        "event": event.event_type,
        "developer": users.get(event.actor_user_id, "System"),
        "workspace": workspaces.get(event.workspace_id, "Developer workspace"),
        "detail": event.detail,
    } for event in events]


@app.get("/api/developer/audit")
def developer_audit_log(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> list[dict]:
    if user.role != Role.DEVELOPER:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Developer access required")
    return developer_audit_rows(db)


def security_status(db: Session) -> dict:
    setting = db.get(SystemSetting, "security")
    return setting.payload if setting else {"sign_in_enabled": True, "updated_at": None, "reason": ""}


@app.get("/api/developer/security")
def get_security_status(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    if user.role != Role.DEVELOPER:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Developer access required")
    return security_status(db)


def set_security_lock(sign_in_enabled: bool, reason: str, user: User, db: Session) -> dict:
    if user.role != Role.DEVELOPER:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Developer access required")
    if not reason.strip():
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="A security reason is required")
    setting = db.get(SystemSetting, "security")
    payload = {"sign_in_enabled": sign_in_enabled, "updated_at": datetime.now(timezone.utc).isoformat(), "reason": reason.strip(), "changed_by": user.email}
    if setting:
        setting.payload = payload
    else:
        db.add(SystemSetting(key="security", payload=payload))
    for account in db.scalars(select(User).where(User.active.is_(True))):
        account.session_version = int(account.session_version or 1) + 1
    db.flush()
    audit(db, user, "security_lockdown_restored" if sign_in_enabled else "security_lockdown_enabled", reason.strip(), getattr(user, "support_workspace_id", None))
    db.commit()
    db.refresh(user)
    return {**payload, "access_token": create_access_token(user, getattr(user, "support_workspace_id", None))}


@app.post("/api/developer/security/lockdown")
def enable_security_lockdown(reason: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    return set_security_lock(False, reason, user, db)


@app.post("/api/developer/security/restore")
def restore_sign_in(reason: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    return set_security_lock(True, reason, user, db)


@app.get("/api/developer/audit.csv")
def download_developer_audit(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> StreamingResponse:
    if user.role != Role.DEVELOPER:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Developer access required")
    rows = developer_audit_rows(db)
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(["timestamp", "event", "developer", "workspace", "detail"])
    for row in rows:
        writer.writerow([row["timestamp"].isoformat() if row["timestamp"] else "", row["event"], row["developer"], row["workspace"], row["detail"]])
    audit(db, user, "developer_audit_exported", "Developer audit CSV exported", getattr(user, "support_workspace_id", None))
    db.commit()
    return StreamingResponse(iter([output.getvalue()]), media_type="text/csv", headers={"Content-Disposition": "attachment; filename=tractorcloser-developer-audit.csv"})


def csv_content(headers: list[str], rows: list[list[object]]) -> str:
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(headers)
    writer.writerows(rows)
    return output.getvalue()


@app.get("/api/admin/export/workspace.zip")
def export_workspace_backup(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> StreamingResponse:
    if user.role not in {Role.ADMIN, Role.DEVELOPER}:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")
    workspace_id = require_workspace(user)
    workspace = db.get(Workspace, workspace_id)
    leads = list(db.scalars(select(Lead).where(Lead.workspace_id == workspace_id).order_by(Lead.id)))
    deals = list(db.scalars(select(Deal).where(Deal.workspace_id == workspace_id).order_by(Deal.id)))
    activities = list(db.scalars(select(LeadActivity).where(LeadActivity.workspace_id == workspace_id).order_by(LeadActivity.id)))
    users = list(db.scalars(select(User).where(User.workspace_id == workspace_id).order_by(User.id)))
    records = list(db.scalars(select(WorkspaceRecord).where(WorkspaceRecord.workspace_id == workspace_id).order_by(WorkspaceRecord.id)))
    events = list(db.scalars(select(AuditEvent).where(AuditEvent.workspace_id == workspace_id).order_by(AuditEvent.id)))
    user_emails = {record.id: record.email for record in db.scalars(select(User))}
    lead_names = {lead.id: lead.name for lead in leads}
    payload_keys = sorted({key for record in records for key in record.payload.keys()})

    archive = BytesIO()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr("manifest.json", json.dumps({"workspace": workspace.name if workspace else "Workspace", "timezone": workspace.timezone if workspace else None, "exported_at": datetime.now(timezone.utc).isoformat(), "format": "TractorCloser workspace backup"}, indent=2))
        bundle.writestr("customers.csv", csv_content(["id", "name", "phone", "email", "equipment", "source", "source_reference", "external_source_id", "contact_consent", "preferred_contact_channel", "budget", "pipeline_stage", "assigned_to", "follow_up_enabled", "test_data", "created_at", "updated_at"], [[lead.id, lead.name, lead.phone, lead.email, lead.equipment, lead.source, lead.source_reference, lead.external_source_id, lead.contact_consent, lead.preferred_contact_channel, lead.budget, lead.pipeline_stage, user_emails.get(lead.assigned_user_id, ""), lead.follow_up_enabled, lead.is_test_data, lead.created_at.isoformat() if lead.created_at else "", lead.updated_at.isoformat() if lead.updated_at else ""] for lead in leads]))
        bundle.writestr("deals.csv", csv_content(["id", "customer", "lead", "equipment", "sale_price", "gross_profit", "sold_at"], [[deal.id, deal.customer, lead_names.get(deal.lead_id, ""), deal.equipment, deal.sale_price, deal.gross_profit, deal.sold_at.isoformat() if deal.sold_at else ""] for deal in deals]))
        bundle.writestr("customer_activity.csv", csv_content(["id", "customer", "type", "detail", "performed_by", "created_at"], [[activity.id, lead_names.get(activity.lead_id, ""), activity.type, activity.body, user_emails.get(activity.actor_user_id, "System"), activity.created_at.isoformat() if activity.created_at else ""] for activity in activities]))
        bundle.writestr("team_members.csv", csv_content(["email", "role", "active", "created_at"], [[record.email, record.role.value, record.active, record.created_at.isoformat() if record.created_at else ""] for record in users]))
        bundle.writestr("workspace_records.csv", csv_content(["id", "record_type", *payload_keys, "created_at", "updated_at"], [[record.id, record.record_type, *[record.payload.get(key, "") for key in payload_keys], record.created_at.isoformat() if record.created_at else "", record.updated_at.isoformat() if record.updated_at else ""] for record in records]))
        bundle.writestr("workspace_audit.csv", csv_content(["id", "event", "detail", "performed_by", "created_at"], [[event.id, event.event_type, event.detail, user_emails.get(event.actor_user_id, "System"), event.created_at.isoformat() if event.created_at else ""] for event in events]))
    archive.seek(0)
    audit(db, user, "workspace_backup_exported", "Workspace backup ZIP exported", workspace_id)
    db.commit()
    safe_name = "".join(character.lower() if character.isalnum() else "-" for character in (workspace.name if workspace else "workspace")).strip("-")
    return StreamingResponse(archive, media_type="application/zip", headers={"Content-Disposition": f"attachment; filename=tractorcloser-{safe_name}-backup.zip"})


@app.post("/api/developer/workspaces/{workspace_id}/support-access")
def support_access(workspace_id: int, reason: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    if user.role != Role.DEVELOPER:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Developer access required")
    workspace = db.get(Workspace, workspace_id)
    if not workspace:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workspace not found")
    audit(db, user, "support_access_started", reason, workspace_id)
    db.commit()
    user.support_workspace_id = workspace.id
    user.support_workspace = workspace
    return {"access_token": create_access_token(user, support_workspace_id=workspace.id), "user": user_response(user), "support_access": True}


@app.post("/api/developer/support-access/exit")
def exit_support_access(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    if user.role != Role.DEVELOPER:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Developer access required")
    workspace_id = getattr(user, "support_workspace_id", None)
    if workspace_id:
        audit(db, user, "support_access_ended", "Developer exited support mode", workspace_id)
        db.commit()
    if hasattr(user, "support_workspace_id"):
        delattr(user, "support_workspace_id")
    if hasattr(user, "support_workspace"):
        delattr(user, "support_workspace")
    return {"access_token": create_access_token(user), "user": user_response(user), "support_access": False}


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
