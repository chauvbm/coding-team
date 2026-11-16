"""Coding-team on/off policy state and the /coding-team command grammar.

State is user-level JSON (schema v1). This module never dispatches agents and
never guesses a chat id: callers pass an already-resolved trusted id or None.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

import ct_storage

HELP_LINES = [
    '/coding-team status - show the global policy, this chat policy and the effective policy',
    '/coding-team help - show this help',
    '/coding-team on - turn coding-team on for this chat only',
    '/coding-team off - turn coding-team off for this chat only',
    '/coding-team on --global - set the global default to ON (chat overrides are kept)',
    '/coding-team off --global - set the global default to OFF (chat overrides are kept)',
    '/coding-team reset - remove this chat\'s override so it follows the global default',
]

PREFIXES = ('/coding-team', '$coding-team')
COMMANDS = {
    'status': ('status', None),
    'help': ('help', None),
    'on': ('chat', 'ON'),
    'off': ('chat', 'OFF'),
    'on --global': ('global', 'ON'),
    'off --global': ('global', 'OFF'),
    'reset': ('chat', 'RESET'),
}
LEGACY = {
    'on all': 'on --global',
    'off all': 'off --global',
    'inherit': 'reset',
}
EPOCH_FIELD = 'policy_epoch_v1'


@dataclass(frozen=True)
class PolicyStamp:
    effective: str | None
    chat_epoch: int
    global_epoch: int | None


class PolicyWriterIncompatible(ValueError):
    reason = 'POLICY_WRITER_INCOMPATIBLE'

    def __init__(self):
        super().__init__(self.reason)


class LegacyCommandError(ValueError):
    """Old command syntax; `.hint` names the replacement."""

    def __init__(self, message, hint):
        super().__init__(message)
        self.hint = hint


def _chat_id(value):
    """Return a usable chat id, or None for empty/whitespace/padded values."""
    if isinstance(value, str) and value and value == value.strip():
        return value
    return None


def validate_state(state):
    if not isinstance(state, dict) or type(state.get('schema_version')) is not int or state['schema_version'] != 1:
        raise ValueError('policy state must be an object with schema_version=1')
    if state.get('global_policy') not in ('ON', 'OFF'):
        raise ValueError('global_policy must be ON or OFF')
    chats = state.get('chat_policies')
    if not isinstance(chats, dict) or any(_chat_id(k) is None or v not in ('ON', 'OFF') for k, v in chats.items()):
        raise ValueError('chat_policies must map actual chat IDs to ON or OFF')


def load_state(path):
    state = json.loads(Path(path).read_text(encoding='utf-8'))
    validate_state(state)
    return state


def _state_bytes(state):
    validate_state(state)
    return (json.dumps(state, ensure_ascii=False, indent=2) + '\n').encode('utf-8')


def save_state(path, state):
    ct_storage.atomic_write(path, _state_bytes(state))


def _policy_fingerprint(state):
    native = {key: state[key] for key in ('schema_version', 'global_policy', 'chat_policies', 'updated_at')
              if key in state}
    return hashlib.sha256(json.dumps(native, sort_keys=True, separators=(',', ':')).encode('ascii')).hexdigest()


def _epoch_metadata(state, require=False):
    if EPOCH_FIELD not in state and not require:
        return None
    metadata = state.get(EPOCH_FIELD)
    if (not isinstance(metadata, dict) or set(metadata) != {'global', 'chats', 'fingerprint'}
            or type(metadata['global']) is not int or metadata['global'] < 0
            or not isinstance(metadata['chats'], dict)
            or any(_chat_id(key) is None or type(epoch) is not int or epoch < 0
                   for key, epoch in metadata['chats'].items())
            or metadata['fingerprint'] != _policy_fingerprint(state)):
        raise PolicyWriterIncompatible()
    return metadata


def policy_stamp(path: Path, chat_id: str, *, require_epochs: bool = False) -> PolicyStamp:
    """Read the policy and applicable epochs while the caller holds the native lock."""
    state = load_state(path)
    metadata = _epoch_metadata(state, require_epochs)
    chat_id = _chat_id(chat_id)
    effective = describe(state, chat_id)['effective_policy']
    inherited = chat_id is not None and chat_id not in state['chat_policies']
    return PolicyStamp(
        effective,
        metadata['chats'].get(chat_id, 0) if metadata else 0,
        (metadata['global'] if metadata else 0) if inherited else None,
    )


def initialize_policy_epochs(path: Path) -> None:
    """Install legacy epoch zero under the caller's native policy lock."""
    state = load_state(path)
    if _epoch_metadata(state) is None:
        _write_initial_epochs(path, state)


def repair_policy_epochs(path: Path) -> None:
    """Restore valid epochs under the managed installer fence and native lock."""
    state = load_state(path)
    try:
        if _epoch_metadata(state, require=True) is not None:
            return
    except PolicyWriterIncompatible:
        _write_initial_epochs(path, state)


def _write_initial_epochs(path, state):
    state[EPOCH_FIELD] = {'global': 0, 'chats': {}, 'fingerprint': _policy_fingerprint(state)}
    save_state(path, state)


def _native_epochs(state):
    try:
        return _epoch_metadata(state)
    except PolicyWriterIncompatible:
        # Native policy commands remain available, but cannot heal a legacy writer's change.
        return {'incompatible': True}


def _update_native_metadata(state, metadata, chat_id=None):
    state['updated_at'] = _now()
    if metadata is not None:
        if 'incompatible' not in metadata:
            if chat_id is None:
                metadata['global'] += 1
            else:
                metadata['chats'][chat_id] = metadata['chats'].get(chat_id, 0) + 1
            metadata['fingerprint'] = _policy_fingerprint(state)
        state[EPOCH_FIELD] = metadata


def _save_native_change(path, state, metadata, chat_id=None):
    _update_native_metadata(state, metadata, chat_id)
    save_state(path, state)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _normalize_default(default):
    if default is not None:
        default = str(default).upper()
        if default not in ('ON', 'OFF'):
            raise ValueError('default must be on or off')
    return default


def _initialization(path, default):
    if not Path(path).exists():
        state = {'schema_version': 1, 'global_policy': default or 'ON', 'chat_policies': {}, 'updated_at': _now()}
        return state, True, True
    state = load_state(path)
    changed = default is not None and state['global_policy'] != default
    if changed:
        metadata = _native_epochs(state)
        state['global_policy'] = default
        _update_native_metadata(state, metadata)
    return state, False, changed


def prepare_initialization(path, default=None):
    """Stage exact initializer bytes under the caller's native lock, or None for a no-op."""
    state, _, changed = _initialization(path, _normalize_default(default))
    return _state_bytes(state) if changed else None


def initialize_state(path, default=None):
    """Create state (default ON) or optionally change only global_policy. Returns (state, created)."""
    default = _normalize_default(default)
    with ct_storage.locked(path):
        state, created, changed = _initialization(path, default)
        if changed:
            save_state(path, state)
        return state, created


def describe(state, chat_id):
    chat_id = _chat_id(chat_id)
    chat = state['chat_policies'].get(chat_id, 'INHERIT') if chat_id else 'UNKNOWN'
    effective = None if chat == 'UNKNOWN' else state['global_policy'] if chat == 'INHERIT' else chat
    return {'global_policy': state['global_policy'], 'chat_id': chat_id, 'chat_policy': chat, 'effective_policy': effective}


def parse_command(text):
    if not isinstance(text, str):
        raise ValueError('command must be text')
    stripped = text.strip()
    if '\n' in stripped or '\r' in stripped:
        raise ValueError('unrecognized complete coding-team command')
    words = ' '.join(stripped.lower().split())
    if words.startswith('/coding team ') or words == '/coding team':
        rest = words[len('/coding team'):].strip()
        replacement = LEGACY.get(rest, rest)
        raise LegacyCommandError('legacy command syntax', 'use /coding-team ' + replacement)
    for prefix in PREFIXES:
        if words.startswith(prefix + ' '):
            rest = words[len(prefix) + 1:]
            if rest in LEGACY:
                raise LegacyCommandError('legacy command syntax', 'use /coding-team ' + LEGACY[rest])
            if rest in COMMANDS:
                return COMMANDS[rest]
            break
    raise ValueError('unrecognized complete coding-team command')


def apply_command(path, text, chat_id):
    scope, value = parse_command(text)
    if scope == 'help':
        return {'help': HELP_LINES}
    chat_id = _chat_id(chat_id)
    if scope == 'status':
        return describe(load_state(path), chat_id)
    if scope == 'chat' and chat_id is None:
        raise ValueError('chat command requires a trusted chat id; use --global or pass --chat-id')
    with ct_storage.locked(path):
        state = load_state(path)
        metadata = _native_epochs(state)
        before = state['global_policy'] if scope == 'global' else state['chat_policies'].get(chat_id)
        if scope == 'global':
            state['global_policy'] = value
        elif value == 'RESET':
            state['chat_policies'].pop(chat_id, None)
        else:
            state['chat_policies'][chat_id] = value
        if before != (None if value == 'RESET' else value):
            _save_native_change(path, state, metadata, chat_id if scope == 'chat' else None)
        return describe(state, chat_id)
