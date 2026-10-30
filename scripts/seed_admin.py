"""Create the first admin user, if one doesn't already exist for that email.

Usage (inside the app container):
    python scripts/seed_admin.py --email admin@example.com --password ChangeMe123!
"""

import argparse
import asyncio

from app.core.database import AsyncSessionLocal
from app.core.security import hash_password
from app.models.user import Role
from app.schemas.user import UserCreate
from app.services import user_service


async def seed(email: str, password: str, full_name: str | None, promote: bool) -> None:
    async with AsyncSessionLocal() as db:
        existing = await user_service.get_user_by_email(db, email)
        if existing is not None:
            if not promote:
                print(f"User {email} already exists (role={existing.role.value}); skipping.")
                print("Re-run with --promote to make this account an admin.")
                return
            existing.role = Role.admin
            if password:
                existing.hashed_password = hash_password(password)
            if full_name is not None:
                existing.full_name = full_name
            await db.commit()
            await db.refresh(existing)
            print(f"Promoted existing user {existing.email} to admin (id={existing.id}).")
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
    parser.add_argument(
        "--promote",
        action="store_true",
        help="If the account already exists, promote it to admin (and reset its password).",
    )
    args = parser.parse_args()

    asyncio.run(seed(args.email, args.password, args.full_name, args.promote))
