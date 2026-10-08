"""Create the first admin user, if one doesn't already exist for that email.

Usage (inside the app container):
    python scripts/seed_admin.py --email admin@example.com --password ChangeMe123!
"""

import argparse
import asyncio

from app.core.database import AsyncSessionLocal
from app.models.user import Role
from app.schemas.user import UserCreate
from app.services import user_service


async def seed(email: str, password: str, full_name: str | None) -> None:
    async with AsyncSessionLocal() as db:
        existing = await user_service.get_user_by_email(db, email)
        if existing is not None:
            print(f"User {email} already exists (role={existing.role.value}); skipping.")
            return
        user = await user_service.create_user(
            db,
            UserCreate(email=email, password=password, full_name=full_name),
            role=Role.admin,
        )
        print(f"Created admin user {user.email} (id={user.id}).")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Seed the first admin user")
    parser.add_argument("--email", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--full-name", default=None)
    args = parser.parse_args()

    asyncio.run(seed(args.email, args.password, args.full_name))
