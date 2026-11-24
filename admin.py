"""Operator CLI for the license service.

Examples:
    python -m server.admin init-db
    python -m server.admin create-code --plan creator --term yearly --email buyer@example.com
    python -m server.admin list-orders
    python -m server.admin show-code --reference ml_...
    python -m server.admin revoke --entitlement-id ...
"""

from __future__ import annotations

import argparse
import sys

try:
    from .config import load_settings
    from .db import Database
    from .payments import PaymentVerification
    from .plans import PLAN_TERMS, get_plan
    from .security import iso, utcnow
    from .service import LicenseError, LicenseService
except (ImportError, ValueError):
    from config import load_settings
    from db import Database
    from payments import PaymentVerification
    from plans import PLAN_TERMS, get_plan
    from security import iso, utcnow
    from service import LicenseError, LicenseService


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="server.admin")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="Create the SQLite schema.")

    create = sub.add_parser("create-code", help="Create a paid manual order and print its code.")
    create.add_argument("--plan", required=True, choices=["starter", "creator", "studio"])
    create.add_argument("--term", required=True, choices=list(PLAN_TERMS))
    create.add_argument("--email", default="manual@moonlight.local")

    sub.add_parser("list-orders", help="Show recent orders.")

    show = sub.add_parser("show-code", help="Re-print a paid order's code.")
    show.add_argument("--reference", required=True)

    revoke = sub.add_parser("revoke", help="Revoke an entitlement.")
    revoke.add_argument("--entitlement-id", required=True)

    args = parser.parse_args(argv)
    settings = load_settings()
    database = Database(settings.database_path)
    database.initialize()
    service = LicenseService(database, settings)

    if args.command == "init-db":
        print(f"Database ready at {settings.database_path}")
        return 0

    if args.command == "create-code":
        plan = get_plan(args.plan)
        order, _ = service.create_order(
            plan_id=args.plan,
            term=args.term,
            email=args.email,
            provider="manual",
        )
        service.mark_paid(
            PaymentVerification(
                success=True,
                reference=order["reference"],
                amount_kobo=order["amount_kobo"],
                currency=order["currency"],
                event_id=f"manual:{order['reference']}",
                raw={},
            ),
            provider_name="manual",
        )
        order = service.get_order(order["reference"]) or order
        print(f"Reference: {order['reference']}")
        print(f"Plan:      {plan.name if plan else args.plan} ({args.term})")
        print(f"Amount:    {order['amount_kobo'] / 100:.2f} {order['currency']}")
        print(f"Code:      {service.code_for_order(order)}")
        return 0

    if args.command == "list-orders":
        with database.connect() as connection:
            rows = connection.execute(
                "SELECT reference, plan_id, term, status, email, created_at "
                "FROM orders ORDER BY created_at DESC LIMIT 50"
            ).fetchall()
        for row in rows:
            print(
                f"{row['created_at']}  {row['status']:<8}  {row['plan_id']:<8}  "
                f"{row['term']:<11}  {row['email']:<28}  {row['reference']}"
            )
        return 0

    if args.command == "show-code":
        order = service.get_order(args.reference)
        if order is None:
            print("Order not found.", file=sys.stderr)
            return 1
        if order["status"] != "paid":
            print(f"Order is {order['status']}, not paid.", file=sys.stderr)
            return 1
        print(service.code_for_order(order))
        return 0

    if args.command == "revoke":
        with database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM entitlements WHERE id = ?", (args.entitlement_id,)
            ).fetchone()
            if row is None:
                print("Entitlement not found.", file=sys.stderr)
                return 1
            connection.execute(
                "UPDATE entitlements SET status = 'revoked' WHERE id = ?",
                (args.entitlement_id,),
            )
            connection.commit()
        print(f"Revoked {args.entitlement_id} at {iso(utcnow())}")
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
