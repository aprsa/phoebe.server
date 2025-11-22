# Copilot instructions for this repo (phoebe-server)

Purpose: FastAPI service that manages per-user PHOEBE compute sessions. Each session is a separate Python worker speaking ZMQ; the API starts/stops workers and proxies commands.

## Architecture overview

- Web API: `phoebe_server.main:app` (FastAPI) with routers in `phoebe_server/api/*`.
  - Health: `api/health.py` → `/health`, `/`.
  - Session mgmt (prefixed `/dash`): `api/session.py` → start/end/list sessions, memory, port pool.
  - Command proxy: `api/command.py` → `/send/{session_id}` forwards JSON to a worker and tracks activity.
- Session Manager: `manager/session_manager.py`
  - Tracks sessions in `server_registry` with activity timestamps; uses list for port allocation.
  - Spawns workers via `psutil.Popen([sys.executable, "-m", "phoebe_server.worker.phoebe_worker", port])`.
  - On startup, automatically detects and terminates orphaned workers from previous runs.
  - Waits for worker readiness (ping command) before returning from start-session (30s timeout).
  - Cleans up idle sessions (configurable timeout) and terminates workers robustly (terminate → wait → kill).
  - Always frees ports on shutdown, even if worker is dead.
- Worker: `worker/phoebe_worker.py`
  - ZMQ REP server bound to `tcp://127.0.0.1:{port}` for security.
  - Command set in `self.commands` includes `ping` for readiness checks.
  - Returns `{success: bool, result|error, traceback?}`.
  - Uses `make_json_serializable` to normalize numpy/units for JSON.
- Proxy: `worker/proxy.py` (ZMQ REQ client) connects to `tcp://127.0.0.1:{port}` for one request-response.
- Config: `config.py` loads `config.toml` (TOML format). Port pool and idle timeout loaded on FastAPI startup via lifespan.
- Database: `database.py` provides SQLite logging with WAL mode for session tracking, command history, and metrics.
  - 4 tables: `sessions`, `session_metrics`, `session_commands`, `session_user_info`.
  - Sync mode (no threading/async) for simplicity.
  - Configurable command filtering (exclude ping by default).
- Background tasks: FastAPI lifespan runs periodic cleanup (every 60s) to terminate idle sessions.
- Graceful shutdown: When server stops (Ctrl+C or SIGTERM), all active sessions are terminated automatically to free ports and prevent orphaned ZMQ processes.

## Run and develop

- Setup: Create venv at `~/.venvs/phoebe.server`, activate, install: `pip install -e .[dev]`.
- Start server:
  - `uvicorn phoebe_server.main:app --host 0.0.0.0 --port 8001` or `phoebe-server run --port 8001`.
  - Lifespan: cleans orphaned workers, loads port pool, starts background cleanup task, configures logging.
  - Stop: Ctrl+C triggers graceful shutdown that terminates all active sessions and frees all ports.
- Tests: `pytest -v` (health/root tests in `tests/`). Ensure venv is activated.
- Config: Edit `config.toml` for port pool range, idle timeout, logging, auth settings.
- Orphaned workers: Run `scripts/cleanup_orphaned_workers.sh` to manually clean up workers if server crashed ungracefully.

## API workflow (happy path)

1) POST `/dash/start-session` → spawns worker, waits for readiness, returns `{ session_id, port, created_at, last_activity, ... }`.
2) POST `/send/{session_id}` with body like:
   `{ "command": "set_value", "twig": "period@binary", "value": 1.5 }` → proxied to worker, updates `last_activity`.
   - Available commands: `ping`, `get_value`, `set_value`, `run_compute`, `attach_parameters`, etc. (see `PhoebeWorker.commands`).
3) POST `/dash/end-session/{session_id}` → terminates worker (graceful then kill), frees port.
4) GET `/dash/sessions` → lists all active sessions (cleans up idle sessions first).
5) GET `/dash/port-status` → shows total/reserved/available ports and range.

## Session management patterns

- Activity tracking: `last_activity` updated on every command and session API call (user info, memory).
- Idle timeout: Configurable in `config.toml` (`session.idle_timeout_seconds`, default 1800s/30min).
- Port lifecycle: Ports allocated from list, returned on shutdown. Use `release_port()` to free.
- Readiness: Worker must respond to `ping` command within 30s or session creation fails and port is freed.
- Robust shutdown: Always terminate → wait(3s) → kill if needed, and free port in all cases.
- Database logging:
  - Session lifecycle logged: created_at, destroyed_at, termination_reason, client_ip, user_agent.
  - Commands logged with execution time (ms) and success/error (configurable filtering).
  - Memory polled and logged after every command execution.
  - User info (first_name, last_name) stored per session.
  - All timestamps are Unix epoch floats (from `time.time()`).

## Project conventions & patterns

- Config: TOML format (`config.toml`), loaded via `tomli`. Includes logging, port pool, idle timeout, auth, database.
- Routers: group endpoints per file, include in `main.py` (session router mounted at `/dash`).
- Worker contract: every command returns JSON-serializable data; errors become `{success:false, error, traceback}`.
- Serialization: when returning PHOEBE/numpy/units data, run through `make_json_serializable`.
- Session records: keep `psutil.Popen` objects in-memory only; never expose in API responses.
- Worker bind: `127.0.0.1` by default for security (workers only accessible via API, not externally).
- CORS: wide-open in dev (`*`); configure for production as needed.
- Database: SQLite with WAL mode; sync operations (no async/threading); command filtering via config; logs closed automatically via context manager.

## Extending the system

- New API endpoint: add router under `phoebe_server/api/`, include in `main.py` with prefix/tags.
- New worker command: add method to `PhoebeWorker`, register in `self.commands`, return JSON-safe data.
- Auth: `config.auth` supports `internal`/`jwt` but not enforced yet—add FastAPI dependencies/middleware when needed.
- Session persistence: Active sessions tracked in database for logging; in-memory registry survives idle timeout but not server restarts.
- Database queries: Use sqlite3 CLI or GUI tools to inspect logged data; no query API implemented yet (by design).

## Useful file references

- App: `phoebe_server/main.py` (lifespan, CORS, routers), CLI: `phoebe_server/cli.py`.
- Sessions: `phoebe_server/api/session.py`, Manager: `phoebe_server/manager/session_manager.py`.
- Worker: `phoebe_server/worker/phoebe_worker.py`, Proxy: `phoebe_server/worker/proxy.py`.
- Config: `phoebe_server/config.py`, example: `config.toml`.
- Database: `phoebe_server/database.py` (schema, logging functions), default location: `data/sessions.db`.
