import csv
import os
from io import StringIO

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from .database import Base, SessionLocal, engine, get_db
from .models import AuditEvent, Role, User, Workspace
from .schemas import LoginRequest, LoginResponse, UserResponse, WorkspaceResponse
from .security import create_access_token, get_current_user, hash_password, verify_password

app = FastAPI(title="TractorCloser API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_credentials=True,
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
