"""Print bcrypt hashes for the three role passwords (paste into .env)."""

from __future__ import annotations

import bcrypt
import getpass
import sys


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def main() -> int:
    if len(sys.argv) == 4:
        labels = ("admin", "manager", "user")
        passwords = sys.argv[1:4]
    else:
        labels = ("admin", "manager", "user")
        passwords = []
        for label in labels:
            passwords.append(getpass.getpass(f"{label} password: "))
    for label, raw in zip(labels, passwords, strict=True):
        key = f"{label.upper()}_PASSWORD_HASH"
        print(f"{key}={hash_password(raw)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
