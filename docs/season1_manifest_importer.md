# Season 1 manifest importer

`season1_manifest_importer.py` uses only the supplied Final Migration Manifest. It does not contact Notion, inspect MP3 files, or access the Bucket.

Default operation is read-only:

```bash
python manage.py season1-audioletter-import --manifest /absolute/path/Season1_Final_Migration_Manifest.md --db "$DB_PATH"
```

The command opens SQLite with `mode=ro`; it cannot initialize, migrate, or write the database. Review every `CONFLICT`, especially S1-07, before considering any apply.

The future apply path is deliberately separate and additionally requires `--apply --confirm-apply`. It creates absent episodes and blocks in one transaction and can fill only an empty unpublished draft. It never deletes or overwrites populated content; any existing content difference blocks the whole apply.

The sole explicit exception is an existing S1-07 (`sequence=7`). It is the independently verified Production reference episode, so dry-run reports `PRESERVE_EXISTING` and apply performs zero writes to that episode or any of its blocks. If S1-07 is absent, the Manifest S1-07 is created normally.
