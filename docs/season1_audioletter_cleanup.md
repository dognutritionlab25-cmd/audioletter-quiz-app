# Season 1 Audioletter markup cleanup

This maintenance command reads the **current** Audioletter database; it does not read the migration manifest or Notion. It scans Season 1 episode titles, canonical block titles, Audio transcripts, and Info bodies. A legacy episode transcript is scanned only when that episode has no canonical blocks, because that is the only situation in which the customer page renders it.

## Read-only scan

```bash
/opt/venv/bin/python manage.py season1-audioletter-cleanup --db "$DB_PATH"
```

The default opens SQLite with `mode=ro`. Review `SAFE_AUTO_FIX`, `REVIEW_REQUIRED`, `PRESERVED_MARKDOWN`, and every before/after preview before considering any write.

To save every finding with the complete Season 1 text value (not just console previews), use a local report path. This report contains only Season 1 Audioletter fields, never subscriber data.

```bash
/opt/venv/bin/python manage.py season1-audioletter-cleanup --db "$DB_PATH" \
  --report-json /tmp/season1-cleanup-report.json
```

The report keeps `safe_patterns` and `review_patterns` separately. A field can show a safe proposed-after value for `<br>` cleanup while still being `REVIEW_REQUIRED` because of a separate uncertain pattern. This improves review visibility only: any `REVIEW_REQUIRED` finding still blocks apply.

## What may be fixed automatically

- encoded or literal `<br>` variants become a newline;
- standalone `<empty-block>` variants are removed without changing prose;
- standalone `<synced_block ...>` wrapper lines are removed;
- repeated leading tabs or four-space prefixes on ordinary prose lines are removed, including mixed paragraphs where only the affected prose lines were indented.

Markdown emphasis such as `**bold**` and `***bold italic***` is never removed. It is reported as `PRESERVED_MARKDOWN` unless another safe normalization (such as indentation) is needed to let the renderer interpret it.

Unknown tag-like residue, inline wrappers/empty-blocks, mixed indentation, one-line indentation, lists/tables, and code-like indentation are `REVIEW_REQUIRED` and block apply.

## Future apply only

Do not run this until the Production dry-run has been reviewed. Apply needs both flags and runs in one transaction:

```bash
/opt/venv/bin/python manage.py season1-audioletter-cleanup --db "$DB_PATH" --apply --confirm-apply
```

Apply changes only the identified `title`, `transcript`, or `body` text field. It never changes `audio_storage_key`, ordering, publication status, rows, or any non-Audioletter table. Any `REVIEW_REQUIRED` finding blocks all writes.
