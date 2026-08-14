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
