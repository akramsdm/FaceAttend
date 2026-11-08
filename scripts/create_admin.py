"""Create an administrator account without ever printing the password."""

import getpass
import sys
from pathlib import Path

from sqlalchemy import select

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db import SessionLocal, init_db  # noqa: E402
from app.models import Admin  # noqa: E402
from app.security import hash_password  # noqa: E402

MIN_PASSWORD_LENGTH = 12


def prompt_username() -> str:
    while True:
        username = input("Admin username: ").strip()
        if username:
            return username
        print("Username must not be empty.")


def prompt_password() -> str:
    while True:
        password = getpass.getpass("Password: ")
        if len(password) < MIN_PASSWORD_LENGTH:
            print(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
            continue
        confirmation = getpass.getpass("Repeat password: ")
        if password != confirmation:
            print("Passwords do not match.")
            continue
        return password


def main() -> int:
    init_db()
    username = prompt_username()

    with SessionLocal() as session:
        existing = session.scalar(select(Admin).where(Admin.username == username))
        if existing is not None:
            print(f"Admin '{username}' already exists.")
            return 1

        password = prompt_password()
        session.add(Admin(username=username, password_hash=hash_password(password)))
        session.commit()

    print(f"Created admin '{username}'.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
