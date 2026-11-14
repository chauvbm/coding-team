"""Repository-scoped orchestration ownership, deterministic routing and handoff validation.

State lives in ``<main worktree>/.coding-team/orchestration.json`` (never inside ``.git/``), so every
worktree of one repository shares it. Ownership is per session: ``claim`` issues a random owner token,
and later updates, handoffs and releases must present that token or match a trusted, non-null
``owner_session_id``. Tokens only appear in the result that creates them and in orchestration.json.
They prevent collisions between sessions that follow the process; they do not stop a process that
deliberately reads the file.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
import subprocess

from ct_storage import atomic_write, locked, reject_links

ORCHESTRATORS = ('leader-direct', 'coding-team', 'superpowers-sdd', 'superpowers-dpa',
                 'superpowers-executing-plans')
LEVELS = ('L0', 'L1', 'L2', 'L3')
SCHEMA_VERSION = 1
STATE_DIR = '.coding-team'
STATE_NAME = 'orchestration.json'
NOTE = 'unresolved SDD workspaces indicate unfinished progress, not a running process'
TERMINATION_EVIDENCE = 'caller-asserted'
BOOL_FACTS = ('safety_forbids_user_choice', 'architecture_dispute', 'multi_contract_change',
              'has_plan', 'sequential_review_sufficient', 'superpowers_available')
FACT_KEYS = ('level', 'user_choice') + BOOL_FACTS + ('independent_issues',)


class OrchestrationConflict(ValueError):
    """Another session or orchestrator holds the task, or unresolved work blocks it."""

    def __init__(self, message, blockers=None):
        super().__init__(message)
        self.blockers = list(blockers or [message])


# --- locations ---------------------------------------------------------------------------------

class OrchestrationLocationError(ValueError):
    """The shared state location cannot be determined safely; nothing was written."""


_GIT_ENV = {'LC_ALL': 'C', 'LANGUAGE': 'C'}


def _run_git(command):
    """Run git; None means git is not installed. Timeouts and other OS errors fail closed."""
    try:
        return subprocess.run(command, capture_output=True, text=True, timeout=10, encoding='utf-8',
                              errors='surrogateescape', env=dict(os.environ, **_GIT_ENV))
    except FileNotFoundError:
        return None
    except subprocess.TimeoutExpired as error:
        raise OrchestrationLocationError('git timed out locating the main worktree; refusing to guess a '
                                         'per-worktree state file: %s' % ' '.join(command)) from error
    except OSError as error:
        raise OrchestrationLocationError('git could not be run to locate the main worktree: %s' % error) from error


def _has_git_entry(workspace):
    path = Path(os.path.abspath(workspace))
    return any(os.path.lexists(candidate / '.git') for candidate in (path, *path.parents))


def _git_worktrees(workspace):
    """Worktree roots from git (main first), or None for a workspace that is definitely not in a repo.

    Falls back (None) only when git is missing or reports "not a git repository", and no ``.git`` entry
    exists in the workspace or any ancestor. Every other git failure raises, so two sessions can never
    silently use different state files for the same repository.
    """
    base = ['git', '-C', str(workspace), 'worktree', 'list', '--porcelain']
    separator = '\0'
    result = _run_git(base + ['-z'])
    if result is not None and result.returncode != 0 and 'unknown switch' in result.stderr:
        separator = '\n'
        result = _run_git(base)
    if result is None or result.returncode != 0:
        not_repo = result is None or 'not a git repository' in result.stderr.lower()
        if not_repo and not _has_git_entry(workspace):
            return None
        detail = 'git is not installed' if result is None else (result.stderr.strip() or 'exit %d' % result.returncode)
        raise OrchestrationLocationError('cannot locate the main worktree for %s; refusing to fall back to a '
                                         'per-worktree state file: %s' % (workspace, detail))
    entries = []
    for line in result.stdout.split(separator):
        if line.startswith('worktree '):
            entries.append({'path': Path(_unquote(line[len('worktree '):])), 'bare': False})
        elif line == 'bare' and entries:
            entries[-1]['bare'] = True
    if not entries:
        raise OrchestrationLocationError('git listed no worktrees for %s' % workspace)
    if entries[0]['bare']:
        raise OrchestrationLocationError('bare main worktree unsupported: state would land inside the git '
                                         'directory %s' % entries[0]['path'])
    return [entry['path'] for entry in entries if not entry['bare']]


def _unquote(value):
    """Undo git's C-style quoting (only seen without -z on old git)."""
    if len(value) >= 2 and value[0] == value[-1] == '"':
        raw = value[1:-1].encode('utf-8', 'surrogateescape').decode('unicode_escape')
        return raw.encode('latin-1').decode('utf-8', 'surrogateescape')
    return value


def _state_root(workspace):
    worktrees = _git_worktrees(workspace)
    return worktrees[0] if worktrees else Path(workspace).resolve()


def orchestration_file(workspace) -> Path:
    return _state_root(workspace) / STATE_DIR / STATE_NAME


def unresolved_sdd_workspaces(workspace) -> list[str]:
    """Absolute POSIX paths of ``.superpowers/sdd/*/progress.md`` across every worktree."""
    roots = _git_worktrees(workspace) or [Path(workspace).resolve()]
    found = set()
    for root in roots:
        for progress in root.glob('.superpowers/sdd/*/progress.md'):
            if progress.is_file():
                found.add(progress.resolve().as_posix())
    return sorted(found)


# --- state -------------------------------------------------------------------------------------

def _empty_state():
    return {'schema_version': SCHEMA_VERSION, 'tasks': {}}


def _load(path):
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return _empty_state()
    try:
        state = json.loads(raw.decode('utf-8'))
    except (UnicodeDecodeError, ValueError) as error:
        raise ValueError('orchestration state is not valid JSON: ' + str(path)) from error
    if (not isinstance(state, dict) or state.get('schema_version') != SCHEMA_VERSION
            or not isinstance(state.get('tasks'), dict)
            or not all(isinstance(record, dict) for record in state['tasks'].values())):
        raise ValueError('orchestration state must have schema_version=1 and a tasks object: ' + str(path))
    return state


def _save(path, state):
    atomic_write(path, (json.dumps(state, indent=2, sort_keys=True, ensure_ascii=False) + '\n').encode('utf-8'))


def _prepare(path):
    """Create the state dir and its catch-all .gitignore; refuse linked state paths."""
    state_dir = path.parent
    for target in (path, Path(str(path) + '.lock'), state_dir / '.gitignore'):
        reject_links(target, state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    reject_links(state_dir, state_dir)
    gitignore = state_dir / '.gitignore'
    if not gitignore.exists():
        try:
            atomic_write(gitignore, b'*\n')
        except OSError:
            # Concurrent first writers can collide on os.replace (Windows PermissionError).
            if not gitignore.exists():
                raise


def _clean_session(session_id):
    """A trusted session id, or None. Empty or padded values are never guessed into an id."""
    if isinstance(session_id, str) and session_id and session_id == session_id.strip():
        return session_id
    return None


def _now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def _require_text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(name + ' must be a non-empty string')
    return value


def _is_owner(current, token, session_id):
    owner_token = current.get('owner_token')
    if isinstance(token, str) and token and isinstance(owner_token, str) and owner_token:
        if secrets.compare_digest(token.encode('utf-8'), owner_token.encode('utf-8')):
            return True
    owner_session = current.get('owner_session_id')
    return owner_session is not None and session_id is not None and owner_session == session_id


def _owner_conflict(task_id, current):
    blocker = ('task %s is held by orchestrator %s (session %s, generation %s); present the owner token '
               'or the owning session id, or use takeover with user confirmation'
               % (task_id, current.get('orchestrator'), current.get('owner_session_id'), current.get('generation')))
    return OrchestrationConflict(blocker, [blocker])


def _public(task_id, record):
    view = {key: copy.deepcopy(value) for key, value in record.items() if key != 'owner_token'}
    view['has_token'] = bool(record.get('owner_token'))
    view['task_id'] = task_id
    return view


def _history_entry(current, reason, event):
    return {'event': event, 'generation': current.get('generation'),
            'owner_session_id': current.get('owner_session_id'),
            'orchestrator': current.get('orchestrator'), 'reason': reason, 'at': _now()}


def _new_record(orchestrator, session_id, token):
    return {'orchestrator': orchestrator, 'owner_session_id': session_id, 'owner_token': token,
            'generation': 1, 'mode': None, 'phase': None, 'handoff_from': None, 'handoff_record': None,
            'acknowledged_sdd': [], 'history': []}


def _sdd_blockers(unresolved):
    return ['unresolved SDD workspace (unfinished progress, not a running process): ' + item
            for item in unresolved]


# --- public API --------------------------------------------------------------------------------

def status(workspace, task_id: str | None = None) -> dict:
    """Read-only view. Never returns owner tokens."""
    state = _load(orchestration_file(workspace))
    unresolved = unresolved_sdd_workspaces(workspace)
    record = state['tasks'].get(task_id) if task_id is not None else None
    if record is not None:
        acknowledged = set(record.get('acknowledged_sdd') or [])
        unresolved = [item for item in unresolved if item not in acknowledged]
    blockers = _sdd_blockers(unresolved)
    if record is not None:
        blockers.append(_owner_conflict(task_id, record).blockers[0])
    return {'tasks': {key: _public(key, value) for key, value in sorted(state['tasks'].items())},
            'unresolved_sdd': unresolved, 'can_claim': not blockers, 'blockers': blockers, 'note': NOTE}


def claim(workspace, task_id, mode, phase, *, session_id: str | None, token: str | None = None) -> dict:
    """Create a coding-team record (result carries the token) or let the verified owner update it."""
    _require_text(task_id, 'task_id')
    _require_text(mode, 'mode')
    _require_text(phase, 'phase')
    session_id = _clean_session(session_id)
    path = orchestration_file(workspace)
    unresolved = unresolved_sdd_workspaces(workspace)
    _prepare(path)
    with locked(path):
        state = _load(path)
        current = state['tasks'].get(task_id)
        if current is None:
            if unresolved:
                raise OrchestrationConflict(
                    'unresolved SDD workspaces block claiming %s; hand off with acknowledged_sdd first' % task_id,
                    unresolved)
            new_token = secrets.token_hex(16)
            current = _new_record('coding-team', session_id, new_token)
            current.update(mode=mode, phase=phase)
            state['tasks'][task_id] = current
            _save(path, state)
            return dict(_public(task_id, current), token=new_token)
        if not _is_owner(current, token, session_id):
            raise _owner_conflict(task_id, current)
        if current.get('orchestrator') != 'coding-team':
            blocker = ('task %s is orchestrated by %s; change orchestrator only through handoff'
                       % (task_id, current.get('orchestrator')))
            raise OrchestrationConflict(blocker, [blocker])
        current.update(mode=mode, phase=phase)
        _save(path, state)
        return _public(task_id, current)


def takeover(workspace, task_id, *, session_id, reason: str, user_confirmed: bool) -> dict:
    """Reassign a task from a lost session. Requires explicit user confirmation; returns a new token."""
    if user_confirmed is not True:
        raise ValueError('takeover requires user_confirmed=True')
    _require_text(task_id, 'task_id')
    _require_text(reason, 'reason')
    session_id = _clean_session(session_id)
    path = orchestration_file(workspace)
    _prepare(path)
    with locked(path):
        state = _load(path)
        current = state['tasks'].get(task_id)
        if current is None:
            raise ValueError('no orchestration record for task ' + task_id)
        new_token = secrets.token_hex(16)
        current['history'] = list(current.get('history') or []) + [_history_entry(current, reason, 'takeover')]
        current.update(owner_token=new_token, owner_session_id=session_id,
                       generation=int(current.get('generation') or 0) + 1)
        _save(path, state)
        return dict(_public(task_id, current), token=new_token)


def _check_list(record, name, item_type=None):
    value = record.get(name) if isinstance(record, dict) else None
    if not isinstance(value, list) or (item_type and not all(isinstance(item, item_type) for item in value)):
        raise ValueError('handoff field %s must be a list' % name)
    return value


def _non_empty(value):
    if isinstance(value, str):
        return bool(value.strip())
    return isinstance(value, (list, dict)) and bool(value)


def validate_handoff(record: dict, current: dict | None, unresolved: list[str], *, session_id) -> None:
    """Raise ValueError (OrchestrationConflict for ownership) unless the handoff record is acceptable."""
    if not isinstance(record, dict):
        raise ValueError('handoff record must be an object')
    for name in ('task_id', 'reason', 'from', 'to', 'workers', 'snapshot', 'tests', 'findings',
                 'previous_ledger', 'acknowledged_sdd'):
        if name not in record:
            raise ValueError('handoff field %s is required' % name)
    _require_text(record['task_id'], 'handoff field task_id')
    _require_text(record['reason'], 'handoff field reason')
    for name in ('from', 'to'):
        if record[name] not in ORCHESTRATORS:
            raise ValueError('handoff field %s must be one of %s' % (name, ', '.join(ORCHESTRATORS)))
    if record['from'] == record['to']:
        raise ValueError('handoff fields from and to must differ')

    for index, worker in enumerate(_check_list(record, 'workers', dict)):
        label = 'handoff field workers[%d]' % index
        if not isinstance(worker.get('id'), str) or not worker['id'].strip():
            raise ValueError(label + '.id must be a non-empty string')
        if worker.get('terminated') is not True:
            raise ValueError(label + '.terminated must be true; hand off only after every worker has ended')
        if not _non_empty(worker.get('evidence')):
            raise ValueError(label + '.evidence must be non-empty runtime evidence of termination')

    snapshot = record['snapshot']
    if not isinstance(snapshot, dict):
        raise ValueError('handoff field snapshot must be an object')
    _require_text(snapshot.get('head'), 'handoff field snapshot.head')
    for name in ('uncommitted', 'untracked'):
        if not isinstance(snapshot.get(name), list):
            raise ValueError('handoff field snapshot.%s must be a list' % name)

    tests = record['tests']
    if not isinstance(tests, dict):
        raise ValueError('handoff field tests must be an object')
    if not isinstance(tests.get('commands'), list) or not all(isinstance(c, str) for c in tests['commands']):
        raise ValueError('handoff field tests.commands must be a list of strings')
    _require_text(tests.get('result'), 'handoff field tests.result')
    if not _non_empty(tests.get('snapshot')):
        raise ValueError('handoff field tests.snapshot must identify the snapshot the tests ran on')

    _check_list(record, 'findings')
    if record['previous_ledger'] is not None and not isinstance(record['previous_ledger'], str):
        raise ValueError('handoff field previous_ledger must be a path string or null')
    if 'user_confirmed' in record and not isinstance(record['user_confirmed'], bool):
        raise ValueError('handoff field user_confirmed must be a boolean')
    if 'token' in record and not isinstance(record['token'], str):
        raise ValueError('handoff field token must be a string')
    known = set(unresolved)
    for item in _check_list(record, 'acknowledged_sdd', str):
        if item not in known:
            raise ValueError('handoff field acknowledged_sdd lists %s, which is not an unresolved SDD workspace'
                             % item)

    session_id = _clean_session(session_id)
    if current is None:
        if record.get('user_confirmed') is not True:
            raise ValueError('handoff of a task without an orchestration record requires user_confirmed: true')
        return
    if record['from'] != current.get('orchestrator'):
        blocker = ('handoff from %s does not match the current orchestrator %s'
                   % (record['from'], current.get('orchestrator')))
        raise OrchestrationConflict(blocker, [blocker])
    if not _is_owner(current, record.get('token'), session_id):
        raise _owner_conflict(record['task_id'], current)


def handoff(workspace, record, *, session_id) -> dict:
    """Validate and apply a handoff. The result carries the newly issued owner token."""
    try:
        stored = json.loads(json.dumps(record))
    except (TypeError, ValueError) as error:
        raise ValueError('handoff record must be JSON-serializable') from error
    session_id = _clean_session(session_id)
    path = orchestration_file(workspace)
    unresolved = unresolved_sdd_workspaces(workspace)
    _prepare(path)
    with locked(path):
        state = _load(path)
        task_id = stored.get('task_id') if isinstance(stored, dict) else None
        current = state['tasks'].get(task_id) if isinstance(task_id, str) else None
        validate_handoff(stored, current, unresolved, session_id=session_id)
        stored.pop('token', None)
        new_token = secrets.token_hex(16)
        updated = _new_record(stored['to'], session_id, new_token)
        if current is not None:
            updated.update(
                generation=int(current.get('generation') or 0) + 1,
                mode=current.get('mode'), phase=current.get('phase'),
                history=list(current.get('history') or []) + [_history_entry(current, stored['reason'], 'handoff')])
        acknowledged = set((current or {}).get('acknowledged_sdd') or []) | set(stored['acknowledged_sdd'])
        updated.update(handoff_from=stored['from'], handoff_record=stored, acknowledged_sdd=sorted(acknowledged))
        state['tasks'][task_id] = updated
        _save(path, state)
        result = dict(_public(task_id, updated), termination_evidence=TERMINATION_EVIDENCE,
                      from_verified=current is not None)
        result['token'] = new_token
        return result


def release(workspace, task_id, *, session_id, token=None) -> dict:
    """Remove the task record; only its verified owner may do so."""
    _require_text(task_id, 'task_id')
    session_id = _clean_session(session_id)
    path = orchestration_file(workspace)
    if not path.exists():
        return {'task_id': task_id, 'released': False}
    _prepare(path)
    with locked(path):
        state = _load(path)
        current = state['tasks'].get(task_id)
        if current is None:
            return {'task_id': task_id, 'released': False}
        if not _is_owner(current, token, session_id):
            raise _owner_conflict(task_id, current)
        del state['tasks'][task_id]
        _save(path, state)
        return {'task_id': task_id, 'released': True, 'generation': current.get('generation'),
                'orchestrator': current.get('orchestrator')}


# --- routing -----------------------------------------------------------------------------------

def _validate_facts(facts):
    if not isinstance(facts, dict):
        raise ValueError('route facts must be an object')
    missing = [key for key in FACT_KEYS if key not in facts]
    if missing:
        raise ValueError('route facts missing: ' + ', '.join(missing))
    if facts['level'] not in LEVELS:
        raise ValueError('route fact level must be one of ' + ', '.join(LEVELS))
    if facts['user_choice'] is not None and facts['user_choice'] not in ORCHESTRATORS:
        raise ValueError('route fact user_choice must be null or one of ' + ', '.join(ORCHESTRATORS))
    for key in BOOL_FACTS:
        if not isinstance(facts[key], bool):
            raise ValueError('route fact %s must be a boolean' % key)
    issues = facts['independent_issues']
    if isinstance(issues, bool) or not isinstance(issues, int) or issues < 0:
        raise ValueError('route fact independent_issues must be an integer >= 0')


def route(facts: dict) -> dict:
    """Apply the spec section 6.2 table top-down on already-classified facts; first match wins."""
    _validate_facts(facts)
    superpowers = facts['superpowers_available']

    def pick(orchestrator, rule, reason):
        return {'orchestrator': orchestrator, 'rule': rule, 'reason': reason}

    if facts['user_choice'] is not None and not facts['safety_forbids_user_choice']:
        return pick(facts['user_choice'], 0, 'user chose the orchestrator explicitly')
    if facts['level'] == 'L3':
        return pick('coding-team', 1, 'L3: significant security, data-integrity, production or irreversible impact')
    if facts['architecture_dispute']:
        return pick('coding-team', 2, 'architecture conflict or design choice needs debate')
    if facts['multi_contract_change']:
        return pick('coding-team', 3, 'change spans multiple interface contracts, schemas or APIs')
    if facts['independent_issues'] >= 2:
        if superpowers:
            return pick('superpowers-dpa', 4, 'several independent issues to investigate (investigation only)')
        return pick('coding-team', 4, 'several independent issues; Superpowers unavailable')
    if facts['has_plan'] and facts['sequential_review_sufficient']:
        if superpowers:
            return pick('superpowers-sdd', 5, 'plan exists and sequential per-task review is sufficient')
        return pick('coding-team', 5, 'plan exists; Superpowers unavailable')
    if facts['level'] in ('L0', 'L1'):
        return pick('leader-direct', 6, 'small, low-impact task')
    return pick('coding-team', 7, 'no earlier rule matched at L2')
