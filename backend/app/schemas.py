from datetime import datetime

from pydantic import BaseModel, EmailStr

from .models import Role


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class UserResponse(BaseModel):
    id: int
    email: EmailStr
    role: Role
    workspace_id: int | None
    workspace_name: str | None
    timezone: str | None
    must_change_password: bool


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserResponse


class WorkspaceResponse(BaseModel):
    id: int
    name: str
    timezone: str
    created_at: datetime


class WorkspaceUserCreate(BaseModel):
    email: EmailStr
    password: str


class WorkspaceUserUpdate(BaseModel):
    password: str | None = None
    active: bool | None = None
    reassign_to_user_id: int | None = None


class PasswordSetupRequest(BaseModel):
    password: str


class LeadCreate(BaseModel):
    name: str
    phone: str = ""
    email: str = ""
    equipment: str = ""
    source: str = "Manual"
    source_reference: str = ""
    original_inquiry: str = ""
    budget: int = 0
    pipeline_stage: str = "New"
    assigned_user_id: int | None = None


class LeadPipelineUpdate(BaseModel):
    pipeline_stage: str
    follow_up_enabled: bool | None = None


class LeadAssignmentUpdate(BaseModel):
    assigned_user_id: int | None = None


class LeadProfileUpdate(BaseModel):
    name: str | None = None
    phone: str | None = None
    email: str | None = None
    equipment: str | None = None


class ActivityCreate(BaseModel):
    type: str = "note"
    body: str


class LeadResponse(BaseModel):
    id: int
    name: str
    phone: str
    email: str
    equipment: str
    source: str
    source_reference: str
    original_inquiry: str
    budget: int
    pipeline_stage: str
    follow_up_enabled: bool
    response_sent: bool
    assigned_user_id: int | None
    is_test_data: bool
    created_at: datetime


class ActivityResponse(BaseModel):
    id: int
    lead_id: int
    type: str
    body: str
    actor_user_id: int | None
    created_at: datetime


class DealCreate(BaseModel):
    lead_id: int | None = None
    customer: str
    equipment: str = ""
    sale_price: int = 0
    gross_profit: int = 0


class DealResponse(BaseModel):
    id: int
    lead_id: int | None
    customer: str
    equipment: str
    sale_price: int
    gross_profit: int
    sold_at: datetime
