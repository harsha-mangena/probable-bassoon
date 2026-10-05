#!/usr/bin/env python3
"""Local staff bootstrap. Passwords are prompted, never hardcoded or logged."""
import argparse
import getpass
from auth import Auth
from engine import Problem
from server import make_engine


def main():
    p = argparse.ArgumentParser()
    p.add_argument("command", choices=["create-user", "reset-password"])
    p.add_argument("username")
    p.add_argument("--role", choices=["admin", "manager"], default="manager")
    p.add_argument("--db")
    p.add_argument("--config")
    a = p.parse_args()
    password = getpass.getpass("Password (at least 12 characters): ")
    if password != getpass.getpass("Confirm password: "):
        p.error("Passwords do not match")
    engine = make_engine(a.db, a.config)
    if a.command == "reset-password":
        with engine.db() as db:
            row = db.execute(
                "SELECT role FROM users WHERE username=?", (a.username,)
            ).fetchone()
        if not row:
            p.error("User not found")
        a.role = row[0]
    try:
        result = Auth(engine).create_user(
            a.username, password, a.role, replace=a.command == "reset-password"
        )
    except Problem as e:
        p.error(str(e))
    print(f"Staff account ready: {result['username']} ({result['role']})")


if __name__ == "__main__":
    main()
