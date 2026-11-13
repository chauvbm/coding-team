"""Coding-team on/off policy state and the /coding-team command grammar.

State is user-level JSON (schema v1). This module never dispatches agents and
never guesses a chat id: callers pass an already-resolved trusted id or None.
"""
from __future__ import annotations

from datetime import datetime, timezone
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


def save_state(path, state):
    validate_state(state)
    ct_storage.atomic_write(path, (json.dumps(state, ensure_ascii=False, indent=2) + '\n').encode('utf-8'))


def _now():
    return datetime.now(timezone.utc).isoformat()


def initialize_state(path, default=None):
    """Create state (default ON) or optionally change only global_policy. Returns (state, created)."""
    if default is not None:
        default = str(default).upper()
        if default not in ('ON', 'OFF'):
            raise ValueError('default must be on or off')
    with ct_storage.locked(path):
        if not Path(path).exists():
            state = {'schema_version': 1, 'global_policy': default or 'ON', 'chat_policies': {}, 'updated_at': _now()}
            save_state(path, state)
            return state, True
        state = load_state(path)
        if default is not None and state['global_policy'] != default:
            state['global_policy'] = default
            state['updated_at'] = _now()
            save_state(path, state)
        return state, False


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
        before = json.dumps(state, sort_keys=True)
        if scope == 'global':
            state['global_policy'] = value
        elif value == 'RESET':
            state['chat_policies'].pop(chat_id, None)
        else:
            state['chat_policies'][chat_id] = value
        if json.dumps(state, sort_keys=True) != before:
            state['updated_at'] = _now()
            save_state(path, state)
        return describe(state, chat_id)
