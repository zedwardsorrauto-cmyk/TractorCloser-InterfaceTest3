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


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserResponse


class WorkspaceResponse(BaseModel):
    id: int
    name: str
    timezone: str
    created_at: datetime


class LeadCreate(BaseModel):
    name: str
    phone: str = ""
    email: str = ""
    equipment: str = ""
    budget: int = 0
    pipeline_stage: str = "New"


class LeadPipelineUpdate(BaseModel):
    pipeline_stage: str
    follow_up_enabled: bool | None = None


class ActivityCreate(BaseModel):
    type: str = "note"
    body: str


class LeadResponse(BaseModel):
    id: int
    name: str
    phone: str
    email: str
    equipment: str
    budget: int
    pipeline_stage: str
    follow_up_enabled: bool
    is_test_data: bool
    created_at: datetime


class ActivityResponse(BaseModel):
    id: int
    lead_id: int
    type: str
    body: str
    created_at: datetime
