import argparse
import json
import os

from app import create_app, seed_demo
from db import init_db
from importers import import_google_form_payload, migrate_anonymous_feedback, migrate_historical_responses
from season1_manifest_importer import apply_manifest, dry_run
from season1_audioletter_cleanup import apply_cleanup, scan_cleanup


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
    season1 = sub.add_parser(
        "season1-audioletter-import",
        help="Read-only Season 1 manifest comparison by default",
    )
    season1.add_argument("--manifest", required=True)
    season1.add_argument("--db", default=os.environ.get("DB_PATH", "quiz.db"))
    season1.add_argument("--apply", action="store_true",
                         help="Future use only: write after explicit confirmation")
    season1.add_argument("--confirm-apply", action="store_true",
                         help="Required together with --apply")
    cleanup = sub.add_parser(
        "season1-audioletter-cleanup",
        help="Read-only Season 1 markup/Notion residue scan by default",
    )
    cleanup.add_argument("--db", default=os.environ.get("DB_PATH", "quiz.db"))
    cleanup.add_argument("--apply", action="store_true",
                         help="Future use only: write after explicit confirmation")
    cleanup.add_argument("--confirm-apply", action="store_true",
                         help="Required together with --apply")
    cleanup.add_argument("--report-json",
                         help="Write a full-text JSON analysis report during read-only dry-run")
    args = parser.parse_args()
    if args.command == "season1-audioletter-import":
        if args.apply:
            if not args.confirm_apply:
                parser.error("--apply requires --confirm-apply")
            result = apply_manifest(args.db, args.manifest)
        else:
            # Do not call create_app() here: it initializes SQLite and would
            # violate the importer's no-write dry-run contract.
            result = dry_run(args.db, args.manifest)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if args.command == "season1-audioletter-cleanup":
        if args.apply:
            if not args.confirm_apply:
                parser.error("--apply requires --confirm-apply")
            if args.report_json:
                parser.error("--report-json is available only for read-only dry-run")
            result = apply_cleanup(args.db)
        else:
            # Do not call create_app(): its database initialization would violate
            # the cleanup scanner's read-only dry-run contract.
            result = scan_cleanup(args.db, report_json=args.report_json)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

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
    elif args.command == "migrate-feedback":
        with open(args.file, encoding="utf-8-sig") as handle:
            result = migrate_anonymous_feedback(db_path, handle.read(), args.episode)
    else:
        raise AssertionError(f"Unhandled command: {args.command}")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
