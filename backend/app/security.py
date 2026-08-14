from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from passlib.context import CryptContext
from sqlalchemy.orm import Session

from .config import get_settings
from .database import get_db
from .models import User, Workspace

password_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
bearer_scheme = HTTPBearer(auto_error=False)


def hash_password(password: str) -> str:
    return password_context.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    return password_context.verify(password, password_hash)


def create_access_token(user: User, support_workspace_id: int | None = None) -> str:
    settings = get_settings()
    expires = datetime.now(timezone.utc) + timedelta(minutes=settings.access_token_minutes)
    payload = {"sub": str(user.id), "role": user.role.value, "workspace_id": user.workspace_id, "session_version": int(user.session_version or 1), "exp": expires}
    if support_workspace_id is not None:
        payload["support_workspace_id"] = support_workspace_id
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def get_current_user(credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme), db: Session = Depends(get_db)) -> User:
    if credentials is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    try:
        payload = jwt.decode(credentials.credentials, get_settings().jwt_secret, algorithms=[get_settings().jwt_algorithm])
        user = db.get(User, int(payload["sub"]))
        support_workspace_id = payload.get("support_workspace_id")
        if support_workspace_id is not None and user and user.role.value == "developer":
            user.support_workspace_id = int(support_workspace_id)
            user.support_workspace = db.get(Workspace, int(support_workspace_id))
    except (jwt.PyJWTError, KeyError, ValueError):
        user = None
    if not user or not user.active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired session")
    if int(payload.get("session_version", 0)) != int(user.session_version or 1):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Your session was revoked. Please sign in again.")
    return user
