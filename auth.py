"""Staff passwords, expiring sessions and role enforcement."""

import hashlib
import hmac
import secrets
from datetime import timedelta
from engine import Problem, iso


def hash_password(password):
    if not isinstance(password, str) or not 12 <= len(password) <= 256:
        raise Problem("Password must contain 12–256 characters")
    salt = secrets.token_hex(16)
    key = hashlib.scrypt(password.encode(), salt=salt.encode(), n=16384, r=8, p=1).hex()
    return f"scrypt${salt}${key}"


def verify(password, stored):
    try:
        _, salt, key = stored.split("$")
        result = hashlib.scrypt(
            password.encode(), salt=salt.encode(), n=16384, r=8, p=1
        ).hex()
        return hmac.compare_digest(key, result)
    except (ValueError, TypeError, AttributeError):
        return False


class Auth:
    def __init__(self, engine):
        self.engine = engine
        self.dummy = hash_password(secrets.token_urlsafe(24))

    def create_user(
        self, username, password, role="manager", actor="bootstrap", replace=False
    ):
        import re

        if (
            not isinstance(username, str)
            or not re.fullmatch(r"[a-zA-Z0-9_.-]{3,60}", username)
            or role not in ("admin", "manager")
        ):
            raise Problem(
                "Use a username of 3–60 characters and the admin/manager role"
            )
        stored = hash_password(password)
        with self.engine.db(True) as db:
            if (
                db.execute(
                    "SELECT 1 FROM users WHERE username=?", (username,)
                ).fetchone()
                and not replace
            ):
                raise Problem("Username already exists", 409)
            db.execute(
                "INSERT INTO users VALUES (?,?,?,1) ON CONFLICT(username) DO UPDATE SET password=excluded.password,role=excluded.role,active=1",
                (username, stored, role),
            )
            db.execute("DELETE FROM sessions WHERE username=?", (username,))
            self.engine.audit(db, actor, "staff.save", username)
        return {"username": username, "role": role}

    def login(self, username, password):
        if (
            not isinstance(username, str)
            or not isinstance(password, str)
            or len(password) > 256
        ):
            raise Problem("Invalid username or password", 401)
        with self.engine.db(True) as db:
            row = db.execute(
                "SELECT * FROM users WHERE username=? AND active=1", (username,)
            ).fetchone()
            ok = verify(password, row["password"] if row else self.dummy)
            if not ok or not row:
                raise Problem("Invalid username or password", 401, "unauthorized")
            token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
            db.execute(
                "DELETE FROM sessions WHERE expires<=?", (iso(self.engine.clock()),)
            )
            db.execute(
                "INSERT INTO sessions VALUES (?,?,?,?)",
                (
                    hashlib.sha256(token.encode()).hexdigest(),
                    username,
                    iso(self.engine.clock() + timedelta(hours=8)),
                    csrf,
                ),
            )
            self.engine.audit(db, username, "staff.login", username)
            return token, {"username": username, "role": row["role"], "csrf": csrf}

    def session(self, token):
        if not token:
            raise Problem("Staff sign-in required", 401, "unauthorized")
        with self.engine.db() as db:
            row = db.execute(
                """SELECT u.username,u.role,s.csrf FROM sessions s JOIN users u ON s.username=u.username
              WHERE s.token_hash=? AND s.expires>? AND u.active=1""",
                (hashlib.sha256(token.encode()).hexdigest(), iso(self.engine.clock())),
            ).fetchone()
            if not row:
                raise Problem(
                    "Staff session expired. Sign in again.", 401, "unauthorized"
                )
            return dict(row)

    def logout(self, token):
        with self.engine.db(True) as db:
            db.execute(
                "DELETE FROM sessions WHERE token_hash=?",
                (hashlib.sha256(token.encode()).hexdigest(),),
            )
