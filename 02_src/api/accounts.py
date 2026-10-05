"""Restaurant-owner accounts: created when a restaurant becomes a client, used to sign in to the dashboard.

Flow: the restaurant clicks "Interested" -> the Strategy is built -> the Outreach Agent drafts the
"your strategy is ready" email. When that email is built, ``provision_access`` creates the client's
account and the email carries their username, password and the dashboard link. These are the client's
credentials to keep; there is no forced change.

The password is derived from the restaurant and a server secret (ACCOUNT_SECRET), so building the same
email again always gives the same password and nothing in plaintext has to be stored. Only a salted
scrypt hash is kept for signing in.
"""

import hashlib
import hmac
import os
import re
import secrets
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from database.database import SessionLocal
from database.models import Restaurant, UserAccount, ClientTrial, OutreachRelationship

MAX_FAILED_ATTEMPTS = 5
LOCK_MINUTES = 10
PASSWORD_LENGTH = 12
MIN_SECRET_LENGTH = 32
# No look-alike characters (0/O, 1/l/I): the password is read from an email and typed in.
_PASSWORD_ALPHABET = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKMNPQRSTUVWXYZ23456789"


class AccountConfigurationError(RuntimeError):
    """ACCOUNT_SECRET is missing or too short."""


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, salt_hex, hash_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        digest = hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt_hex), n=2**14, r=8, p=1, dklen=32)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest.hex(), hash_hex)


def client_password(restaurant_id: int) -> str:
    """The client's password: HMAC(ACCOUNT_SECRET, restaurant) mapped onto readable characters."""
    secret = os.getenv("ACCOUNT_SECRET", "")
    if len(secret) < MIN_SECRET_LENGTH:
        raise AccountConfigurationError(
            f"ACCOUNT_SECRET must be set to a random string of at least {MIN_SECRET_LENGTH} characters."
        )
    digest = hmac.new(secret.encode("utf-8"), f"rawaj-client-password:{restaurant_id}".encode("utf-8"), hashlib.sha256).digest()
    return "".join(_PASSWORD_ALPHABET[byte % len(_PASSWORD_ALPHABET)] for byte in digest[:PASSWORD_LENGTH])


def _username_for(db: Session, restaurant: Restaurant) -> str:
    base = re.sub(r"[^a-z0-9._-]+", "", (restaurant.instagram_username or "").lower()) or f"restaurant{restaurant.id}"
    return f"{base}.{restaurant.id}_rawaj"


def login_url() -> str:
    return os.getenv("DASHBOARD_URL", "http://localhost:8501").rstrip("/")


def provision_access(restaurant_id: int, *, session_factory=None) -> dict | None:
    """Create the client's account if needed and return what the email should say: username, password, sign-in link.

    Safe to call again for the same restaurant. A changed ACCOUNT_SECRET must
    never silently replace credentials that have already been emailed.
    """
    password = client_password(restaurant_id)
    with (session_factory or SessionLocal)() as db:
        restaurant = db.get(Restaurant, restaurant_id)
        if restaurant is None:
            return None
        account = db.scalar(select(UserAccount).where(UserAccount.restaurant_id == restaurant_id))
        if account is None:
            account = UserAccount(
                restaurant_id=restaurant_id, username=_username_for(db, restaurant), password_hash=hash_password(password),
            )
            db.add(account)
        elif not verify_password(password, account.password_hash):
            raise AccountConfigurationError(
                "ACCOUNT_SECRET does not match this existing account. Restore the original session secret; "
                "the saved password was not changed."
            )
        db.commit()
        return {"username": account.username, "password": password, "rawaj_username": account.username, "rawaj_password": password, "login_url": login_url()}


class AuthError(Exception):
    """Wrong credentials or a locked account. Deliberately one message for every cause."""


def authenticate(db: Session, username: str, password: str) -> UserAccount:
    account = db.scalar(select(UserAccount).where(UserAccount.username == username.strip().lower()))
    now = datetime.utcnow()
    if account is None:
        hash_password(password)  # keep the timing similar whether or not the username exists
        raise AuthError
    if account.locked_until and account.locked_until > now:
        raise AuthError
    if not verify_password(password, account.password_hash):
        account.failed_attempts += 1
        if account.failed_attempts >= MAX_FAILED_ATTEMPTS:
            account.locked_until = now + timedelta(minutes=LOCK_MINUTES)
            account.failed_attempts = 0
        db.commit()
        raise AuthError
    activate_trial(db, account.restaurant_id, now=now)
    account.failed_attempts, account.locked_until, account.last_login_at = 0, None, now
    db.commit()
    return account


def admin_login(username: str, password: str) -> bool:
    """The staff account from ADMIN_USERNAME / ADMIN_PASSWORD (both must be set)."""
    expected_user, expected_password = os.getenv("ADMIN_USERNAME", ""), os.getenv("ADMIN_PASSWORD", "")
    if not expected_user or not expected_password:
        return False
    ok_user = hmac.compare_digest(username.strip().lower(), expected_user.strip().lower())
    ok_password = hmac.compare_digest(password, expected_password)
    return ok_user and ok_password


def activate_trial(db: Session, restaurant_id: int, *, now=None) -> ClientTrial:
    """First successful owner sign-in activates exactly 30 days, with feedback at day 27."""
    now = now or datetime.utcnow()
    trial = db.get(ClientTrial, restaurant_id)
    if trial is None:
        trial = ClientTrial(restaurant_id=restaurant_id, activated_at=now,
            expires_at=now + timedelta(days=30), feedback_due_at=now + timedelta(days=27))
        db.add(trial)
        relation = db.scalar(select(OutreachRelationship).where(OutreachRelationship.restaurant_id == restaurant_id))
        if relation and relation.do_not_contact_at is None and relation.status != "DO_NOT_CONTACT":
            relation.status = "ACTIVE_CLIENT"
            relation.next_contact_at = trial.feedback_due_at
            relation.version += 1
    return trial


def owner_token(account: UserAccount) -> str:
    import time
    payload = f"{account.restaurant_id}.{int(time.time()) + 86400}"
    signature = hmac.new(account.password_hash.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{signature}"


def verify_owner_token(db: Session, token: str) -> int:
    import time
    try:
        restaurant_id, expiry, signature = token.split(".")
        account = db.scalar(select(UserAccount).where(UserAccount.restaurant_id == int(restaurant_id)))
        if account is None or int(expiry) <= time.time():
            raise ValueError()
        expected = hmac.new(account.password_hash.encode(), f"{restaurant_id}.{expiry}".encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError()
        return account.restaurant_id
    except (ValueError, TypeError):
        raise AuthError from None
