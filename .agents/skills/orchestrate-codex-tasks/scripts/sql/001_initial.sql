PRAGMA user_version = 1;

CREATE TABLE IF NOT EXISTS run_state (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    run_id TEXT NOT NULL UNIQUE,
    protocol_version INTEGER NOT NULL,
    run_language TEXT NOT NULL CHECK (run_language IN ('en', 'zh-CN')),
    controller_thread_id TEXT NOT NULL,
    controller_host_id TEXT,
    controller_epoch INTEGER NOT NULL DEFAULT 1 CHECK (controller_epoch > 0),
    controller_title TEXT,
    status TEXT NOT NULL DEFAULT 'ACTIVE'
        CHECK (status IN ('ACTIVE', 'WAITING', 'COMPLETE', 'DEGRADED')),
    max_active_workers INTEGER NOT NULL DEFAULT 8 CHECK (max_active_workers > 0),
    local_preferred INTEGER NOT NULL DEFAULT 1 CHECK (local_preferred IN (0, 1)),
    current_cycle INTEGER NOT NULL DEFAULT 1 CHECK (current_cycle > 0),
    active_count INTEGER NOT NULL DEFAULT 0 CHECK (active_count >= 0),
    queued_count INTEGER NOT NULL DEFAULT 0 CHECK (queued_count >= 0),
    monitor_groups_json TEXT NOT NULL DEFAULT '[]',
    one_to_one_since TEXT,
    persistence_mode TEXT NOT NULL DEFAULT 'LOCAL'
        CHECK (persistence_mode IN ('LOCAL', 'DEGRADED')),
    reconstructed INTEGER NOT NULL DEFAULT 0 CHECK (reconstructed IN (0, 1)),
    current_manifest_hash TEXT,
    revision INTEGER NOT NULL DEFAULT 0 CHECK (revision >= 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS manifests (
    manifest_hash TEXT PRIMARY KEY,
    protocol_version INTEGER NOT NULL,
    manifest_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS planned_tasks (
    task_id TEXT PRIMARY KEY,
    worker_id TEXT NOT NULL UNIQUE,
    priority INTEGER NOT NULL DEFAULT 100,
    status TEXT NOT NULL DEFAULT 'QUEUED'
        CHECK (status IN ('QUEUED', 'DISPATCHING', 'DISPATCHED', 'CANCELLED', 'ACCEPTED', 'RETIRED')),
    objective TEXT NOT NULL,
    dependencies_json TEXT NOT NULL DEFAULT '[]',
    environment TEXT NOT NULL CHECK (environment IN ('local', 'worktree', 'projectless')),
    write_boundary_json TEXT NOT NULL DEFAULT '[]',
    spec_json TEXT NOT NULL,
    spec_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS workers (
    worker_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL UNIQUE REFERENCES planned_tasks(task_id),
    thread_id TEXT UNIQUE,
    client_thread_id TEXT UNIQUE,
    host_id TEXT,
    state TEXT NOT NULL DEFAULT 'PROVISIONING'
        CHECK (state IN ('PROVISIONING', 'RUNNING', 'REVIEW', 'BLOCKED', 'ACCEPTED', 'RETIRED')),
    health TEXT NOT NULL DEFAULT 'HEALTHY'
        CHECK (health IN ('HEALTHY', 'AT_RISK', 'STALLED')),
    title TEXT,
    objective TEXT NOT NULL,
    current_milestone TEXT,
    closed_acceptance_json TEXT NOT NULL DEFAULT '[]',
    remaining_acceptance_json TEXT NOT NULL DEFAULT '[]',
    last_details_json TEXT NOT NULL DEFAULT '[]',
    next_actions_json TEXT NOT NULL DEFAULT '[]',
    needs_json TEXT NOT NULL DEFAULT '[]',
    evidence_json TEXT NOT NULL DEFAULT '[]',
    last_seq INTEGER NOT NULL DEFAULT 0 CHECK (last_seq >= 0),
    last_controller_seq INTEGER NOT NULL DEFAULT 0 CHECK (last_controller_seq >= 0),
    last_controller_seq_reserved INTEGER NOT NULL DEFAULT 0
        CHECK (last_controller_seq_reserved >= last_controller_seq),
    cursor TEXT,
    result_summary TEXT,
    last_useful_progress_at TEXT,
    estimated_remaining TEXT,
    progress_without_completion INTEGER NOT NULL DEFAULT 0 CHECK (progress_without_completion >= 0),
    decision_round_trips INTEGER NOT NULL DEFAULT 0 CHECK (decision_round_trips >= 0),
    scope_delta_count INTEGER NOT NULL DEFAULT 0 CHECK (scope_delta_count >= 0),
    timeout_count INTEGER NOT NULL DEFAULT 0 CHECK (timeout_count >= 0),
    next_health_review_at TEXT,
    archive_ready INTEGER NOT NULL DEFAULT 0 CHECK (archive_ready IN (0, 1)),
    terminal_reason TEXT,
    replacement_worker_id TEXT,
    prompt_version INTEGER NOT NULL,
    prompt_hash TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (
        (archive_ready = 0)
        OR (
            state IN ('ACCEPTED', 'RETIRED')
            AND terminal_reason IS NOT NULL
            AND length(trim(terminal_reason)) > 0
        )
    )
);

CREATE TABLE IF NOT EXISTS operations (
    operation_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL
        CHECK (kind IN ('CREATE_THREAD', 'SEND_MESSAGE', 'SET_TITLE', 'HANDOFF')),
    worker_id TEXT REFERENCES workers(worker_id),
    status TEXT NOT NULL DEFAULT 'INTENT'
        CHECK (status IN ('INTENT', 'SUCCEEDED', 'FAILED', 'UNKNOWN')),
    request_hash TEXT NOT NULL,
    request_json TEXT NOT NULL,
    response_hash TEXT,
    response_json TEXT,
    controller_seq INTEGER,
    attempt INTEGER NOT NULL DEFAULT 1 CHECK (attempt > 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS integrations (
    worker_id TEXT PRIMARY KEY REFERENCES workers(worker_id),
    target_branch TEXT,
    state TEXT NOT NULL DEFAULT 'NONE'
        CHECK (
            state IN (
                'NONE',
                'PENDING_REVIEW',
                'READY',
                'HANDOFF_RUNNING',
                'HANDED_OFF',
                'VALIDATING',
                'ACCEPTED',
                'FAILED',
                'DISCARDED'
            )
        ),
    external_operation_id TEXT,
    evidence_json TEXT NOT NULL DEFAULT '[]',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS decisions (
    decision_id TEXT PRIMARY KEY,
    source TEXT NOT NULL CHECK (source IN ('USER', 'CONTROLLER')),
    summary TEXT NOT NULL,
    scope_json TEXT NOT NULL DEFAULT '[]',
    worker_ids_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cycles (
    cycle_number INTEGER PRIMARY KEY CHECK (cycle_number > 0),
    status TEXT NOT NULL CHECK (status IN ('ACTIVE', 'COMPLETE')),
    goal_summary TEXT NOT NULL,
    result_summary TEXT,
    validation_json TEXT NOT NULL DEFAULT '[]',
    started_at TEXT NOT NULL,
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS events (
    revision INTEGER PRIMARY KEY CHECK (revision > 0),
    idempotency_key TEXT NOT NULL UNIQUE,
    event_type TEXT NOT NULL,
    worker_id TEXT,
    operation_id TEXT,
    payload_hash TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tasks_status_priority
    ON planned_tasks(status, priority, worker_id);
CREATE INDEX IF NOT EXISTS idx_workers_state_health
    ON workers(state, health, worker_id);
CREATE INDEX IF NOT EXISTS idx_operations_status
    ON operations(status, kind, worker_id);
CREATE INDEX IF NOT EXISTS idx_events_worker
    ON events(worker_id, revision);

CREATE TRIGGER IF NOT EXISTS events_no_update
BEFORE UPDATE ON events
BEGIN
    SELECT RAISE(ABORT, 'events are append-only');
END;

CREATE TRIGGER IF NOT EXISTS events_no_delete
BEFORE DELETE ON events
BEGIN
    SELECT RAISE(ABORT, 'events are append-only');
END;

CREATE TRIGGER IF NOT EXISTS terminal_workers_no_regression
BEFORE UPDATE OF state ON workers
WHEN OLD.state IN ('ACCEPTED', 'RETIRED') AND NEW.state <> OLD.state
BEGIN
    SELECT RAISE(ABORT, 'terminal worker state cannot regress');
END;

CREATE TRIGGER IF NOT EXISTS terminal_tasks_no_regression
BEFORE UPDATE OF status ON planned_tasks
WHEN OLD.status IN ('ACCEPTED', 'RETIRED') AND NEW.status <> OLD.status
BEGIN
    SELECT RAISE(ABORT, 'terminal task status cannot regress');
END;

CREATE TRIGGER IF NOT EXISTS completed_runs_no_regression
BEFORE UPDATE OF status ON run_state
WHEN OLD.status = 'COMPLETE' AND NEW.status <> OLD.status
BEGIN
    SELECT RAISE(ABORT, 'completed run status cannot regress');
END;
