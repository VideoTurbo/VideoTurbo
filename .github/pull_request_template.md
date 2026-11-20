## What and why

<!-- What does this change, and which problem does it solve? Link the issue, e.g. "Fixes #123". -->

## How it was tested

<!-- Commands you ran, and what you checked by hand (dashboard pages, GPU PC, platforms). -->

## Checklist

- [ ] `uv run ruff check src tests tools` and `uv run pytest -q` pass
- [ ] `npm test` and `npm run build` pass in `web/` (if the dashboard changed)
- [ ] Dependencies changed → `uv run python tools/export_requirements.py` run and the lists committed
- [ ] Database schema or stored settings changed → a new entry appended to `MIGRATIONS` in `src/video_turbo/migrations.py`
- [ ] Main app ↔ GPU PC API changed → `WORKER_PROTOCOL_VERSION` bumped in `src/video_turbo/protocol.py`
- [ ] Plugin interface (`*/base.py`) changed → `PLUGIN_API_VERSION` / `PLUGIN_API_MIN_VERSION` updated
- [ ] README.md and README.en.md updated if user-facing behavior changed
