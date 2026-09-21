import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, EmailStr, Field

from src.modules.users.models import UserRole

# Self-registration can only ever produce a student or instructor account — granting
# admin is an explicit, separate act (PATCH /users/{id}/role, admin-only), never
# something a registration payload can request for itself.
SelfRegisterableRole = Literal[UserRole.STUDENT, UserRole.INSTRUCTOR]


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    full_name: str = Field(min_length=1, max_length=255)
    role: SelfRegisterableRole = UserRole.STUDENT


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class RefreshRequest(BaseModel):
    refresh_token: str


class LogoutRequest(BaseModel):
    refresh_token: str


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str = Field(min_length=8, max_length=128)


class UserPublic(BaseModel):
    id: uuid.UUID
    email: EmailStr
    full_name: str
    role: UserRole
    is_active: bool
    created_at: datetime


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int
