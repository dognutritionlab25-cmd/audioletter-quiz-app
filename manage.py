import argparse
import json
import os

from app import create_app, seed_demo
from db import init_db
from importers import import_google_form_payload, migrate_anonymous_feedback, migrate_historical_responses


def main():
    parser = argparse.ArgumentParser(description="Quiz/Participation App maintenance commands")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init-db")
    sub.add_parser("seed-demo")
    form = sub.add_parser("import-form-json")
    form.add_argument("file")
    form.add_argument("--season", default="S1")
    history = sub.add_parser("migrate-history")
    history.add_argument("file")
    history.add_argument("--episode", required=True)
    history.add_argument("--score-policy", choices=["first", "last", "max"], default="last")
    feedback = sub.add_parser("migrate-feedback")
    feedback.add_argument("file")
    feedback.add_argument("--episode", required=True)
    args = parser.parse_args()
    app = create_app()
    db_path = app.config["DB_PATH"]
    if args.command == "init-db":
        init_db(db_path)
        result = {"initialized": db_path}
    elif args.command == "seed-demo":
        seed_demo(db_path)
        result = {"seeded": db_path}
    elif args.command == "import-form-json":
        with open(args.file, encoding="utf-8") as handle:
            result = import_google_form_payload(db_path, json.load(handle), args.season)
    elif args.command == "migrate-history":
        with open(args.file, encoding="utf-8-sig") as handle:
            result = migrate_historical_responses(
                db_path, handle.read(), args.episode, app.config["MIGRATION_HASH_SECRET"], args.score_policy
            )
    else:
        with open(args.file, encoding="utf-8-sig") as handle:
            result = migrate_anonymous_feedback(db_path, handle.read(), args.episode)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

