import hmac
import time

from fastapi import HTTPException, Request

from .security import csrf_for, hash_token, new_token

COOKIE = "bm_session"


class LoginRequired(Exception):
    pass


def db_of(request: Request):
    return request.app.state.db


def create_session(request: Request, user_id: int) -> str:
    token = new_token("bms_")
    now = time.time()
    days = request.app.state.settings.session_days
    db = db_of(request)
    db.run("DELETE FROM sessions WHERE expires_at < ?", now)
    db.run("INSERT INTO sessions(token_hash, user_id, created_at, expires_at) VALUES (?,?,?,?)",
           hash_token(token), user_id, now, now + days * 86400)
    return token


def set_cookie(request: Request, response, token: str) -> None:
    secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
    days = request.app.state.settings.session_days
    response.set_cookie(COOKIE, token, max_age=int(days * 86400), httponly=True,
                        samesite="lax", secure=secure, path="/")


def current_user(request: Request):
    token = request.cookies.get(COOKIE)
    if not token:
        return None
    row = db_of(request).one(
        "SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id "
        "WHERE s.token_hash = ? AND s.expires_at > ?", hash_token(token), time.time())
    if row:
        request.state.csrf = csrf_for(token)
    return row


def require_user(request: Request):
    user = current_user(request)
    if not user:
        raise LoginRequired()
    request.state.user = user
    return user


def require_api_user(request: Request):
    user = current_user(request)
    if not user:
        raise HTTPException(401, "Niet ingelogd")
    request.state.user = user
    return user


def require_admin(request: Request):
    user = require_user(request)
    if not user["is_admin"]:
        raise HTTPException(403, "Alleen voor beheerders")
    return user


def check_csrf(request: Request, value: str | None) -> None:
    expected = getattr(request.state, "csrf", None)
    if not expected or not value or not hmac.compare_digest(expected, value):
        raise HTTPException(403, "Ongeldig formulier, laad de pagina opnieuw")


def logout(request: Request) -> None:
    token = request.cookies.get(COOKIE)
    if token:
        db_of(request).run("DELETE FROM sessions WHERE token_hash=?", hash_token(token))
