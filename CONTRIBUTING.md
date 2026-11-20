# Contributing to VideoTurbo

Thanks for helping out. This document covers reporting problems, setting up a development environment and sending
changes.

## Reporting bugs and requesting features

Use the issue templates. For bugs, always attach the **diagnostics bundle** from the dashboard
(Settings → 日志与环境 / Logs & environment → 下载诊断包 / Download diagnostics). It contains the version, settings with
all keys removed, connected machines, installed plugins and the recent main app log, which answers most follow-up questions.

Plugins are maintained by their authors. If a problem only happens with a third-party plugin, report it to the plugin
first.

## Project layout

| Path | What it is |
| --- | --- |
| `src/video_turbo/orchestrator/` | Main app: FastAPI server, automatic processing, cloud hosts and video restoration |
| `src/video_turbo/orchestrator/*_api.py` | HTTP routes, one module per area (`jobs_api`, `worker_api`, `compute_api`, `restore_api`, `settings_api`, …); keep them thin and put logic in the services (`processor.py`, `worker_service.py`, `*_service.py`) |
| `src/video_turbo/orchestrator/context.py` | Typed `app_state(request)`, the worker-token check and shared constants |
| `src/video_turbo/worker/` | Service on the GPU PC: speech recognition and video restoration |
| `src/video_turbo/translation/` | Subtitle translation: `run.py` (batching, retries, fallback model, splitting), `api.py` (one `ApiFormat` per API type — add a subclass for a new provider), `pool.py`, `prompts.py`, `checkpoint.py` |
| `src/video_turbo/plugins/`, `*/base.py` | Plugin system; each plugin type's `base.py` is the public plugin interface |
| `src/video_turbo/database.py`, `*_store.py` | SQLite access: `Database` manages connections and combines one mixin per area (`job_store`, `media_store`, `worker_store`, `settings_store`, …); see `store_base.py`. All `runtime_settings` keys go through `settings_store` |
| `src/video_turbo/migrations.py` | Database schema versions |
| `src/video_turbo/protocol.py` | Main app ↔ GPU PC protocol version |
| `web/` | Dashboard (Vue 3 + TypeScript + Vite + Naive UI); builds into the Python package |
| `deploy/` | Docker image, compose example and GPU PC install scripts |
| `tools/` | Release packaging and GPU PC setup helpers |
| `tests/` | Python tests; `web/tests/` holds the dashboard tests |

## Development setup

Requirements: Python 3.11, [uv](https://docs.astral.sh/uv/), Node.js 22 and FFmpeg.

```bash
uv sync --extra orchestrator --extra worker --extra dev
npm --prefix web ci
npm --prefix web run build
```

Run the main app locally without touching real media. `VIDEO_TURBO_LOCAL_PREVIEW=1` listens on 127.0.0.1 only and
disables scanning, media server refreshes and notifications:

```bash
VIDEO_TURBO_LOCAL_PREVIEW=1 VIDEO_TURBO_DATABASE=runtime/app.db VIDEO_TURBO_CACHE=runtime/cache \
  VIDEO_TURBO_LOG_DIR=runtime/logs VIDEO_TURBO_RESTORE_BACKUP= uv run video-turbo-server
```

The dashboard is then at <http://127.0.0.1:8765>. For live reload of the dashboard, keep the server running and start
Vite in another terminal (it proxies `/api` to port 8765, override with `VIDEO_TURBO_SERVER`):

```bash
npm --prefix web run dev
```

## Checks

CI runs the same commands; please run them before opening a pull request.

```bash
uv run ruff check src tests tools
uv run pytest -q
npm --prefix web test
npm --prefix web run build
```

## Rules that keep upgrades safe

- **Database:** never edit a released migration. Append a new entry to `MIGRATIONS` in `src/video_turbo/migrations.py`
  for any change to tables or to the settings stored in `runtime_settings`.
- **Job states:** the allowed subtitle-job state changes live in `src/video_turbo/job_states.py`. Pipeline code moves
  jobs only through `Database.set_job_state`, which refuses changes not listed there; tests arrange states with
  `force_job_state` from `tests/conftest.py`.
- **GPU PC protocol:** if you add, remove or change a worker route or its fields, bump `WORKER_PROTOCOL_VERSION`
  in `src/video_turbo/protocol.py`. The rules are in that file's docstring.
- **Plugin interface:** each plugin type's `base.py` has `PLUGIN_API_VERSION` and `PLUGIN_API_MIN_VERSION`. Additive
  changes raise only `PLUGIN_API_VERSION`; removing or changing an existing interface raises both.
- **Dependencies:** GPU PCs install from the hashed lists in `deploy/worker/requirements/`, exported from `uv.lock`.
  After changing `pyproject.toml` or `uv.lock` (including Dependabot PRs), run
  `uv run python tools/export_requirements.py` and commit the result; CI fails while the lists are out of date.
- **Version number:** it lives only in `src/video_turbo/__init__.py`. Releases are cut by pushing a matching `v*` tag.
- **Docs:** user-facing changes update both `README.md` and `README.en.md`.

## Commit messages

Follow [Conventional Commits](https://www.conventionalcommits.org/), in English:

```
<type>(<scope>): <subject>
```

- `type`: `feat`, `fix`, `refactor`, `perf`, `docs`, `test`, `build`, `ci` or `chore`.
- `scope` (optional): the area touched, e.g. `orchestrator`, `worker`, `web`, `db`, `subtitles`, `translation`,
  `restore`, `plugins`, `cloud`, `deploy`.
- `subject`: imperative mood, lowercase, no trailing period, header ≤ 72 characters.
- One logical change per commit. Breaking changes add `!` after the type/scope and a `BREAKING CHANGE:` footer.

## License

By contributing you agree that your contributions are licensed under the project's
[AGPL-3.0-or-later](LICENSE) license.
