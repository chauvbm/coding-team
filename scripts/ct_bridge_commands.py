"""Shared, data-only catalog for the public inter-app chat controls."""
from __future__ import annotations

import re

import ct_policy

_COMMAND = re.compile(
    r'(?:/coding-team|/team-coding|\$coding-team)[ \t]+([a-z]+)(?:[ \t]+([a-z]+))?(?:[ \t]+([a-z]+))?',
    re.IGNORECASE | re.ASCII,
)
_APPS = {'agy': 'antigravity', 'claude': 'claude-code',
         'codex': 'codex'}
_FAMILY = re.compile(
    r'(?:/coding-team|/team-coding|\$coding-team)[ \t]+(agy|claude|codex)(?:\b|[ \t])',
    re.IGNORECASE | re.ASCII,
)
_HELP_LINES = (
    '/coding-team <app> on|off — in the Leader app, enable or pause agy, claude or codex',
    '/coding-team <app> auto on|off — in the Leader app, enable the target and choose automatic or manual delivery',
    '/coding-team listen — in the worker app, join the invitation or resume the paired chat',
    '/coding-team status — show this chat and worktree connection status',
    '/coding-team help — show commands, supported pairs, and an example',
)
_SUPPORTED_PAIRS = (('codex', 'antigravity'),)


def parse_public(text: str) -> tuple[str | None, str] | None:
    """Parse one complete ASCII command; prose and multiline content are data."""
    if not isinstance(text, str) or not text.isascii() or '\n' in text or '\r' in text:
        return None
    value = text.strip(' \t')
    match = _COMMAND.fullmatch(value)
    if match is None:
        return None
    first, second, third = (part.lower() if part else None for part in match.groups())
    if first in ('listen', 'status', 'help') and second is None and third is None:
        return (None, first)
    target = _APPS.get(first)
    if target is None:
        return None
    if second in ('on', 'off') and third is None:
        return target, second
    if second == 'auto' and third in ('on', 'off'):
        return target, 'auto_' + third
    return None


def is_public_family(text: str) -> bool:
    """Recognize target-prefixed syntax, including malformed known-target forms.

    Callers can route malformed target commands to the bridge and return help.
    Legacy ``antigravity`` commands intentionally remain outside this family.
    """
    return (isinstance(text, str) and text.isascii() and '\n' not in text and '\r' not in text
            and _FAMILY.match(text.strip(' \t')) is not None)


def supports_pair(leader: str, target: str) -> bool:
    return (leader, target) in _SUPPORTED_PAIRS


def help_view(platform: str, component: str) -> dict:
    if platform not in ('claude-code', 'codex', 'antigravity') or component not in (
            'leader', 'worker', 'native'):
        raise ValueError('unsupported platform or component')
    available = []
    if component in ('leader', 'native'):
        available = [pair for pair in _SUPPORTED_PAIRS if pair[0] == platform]
    elif component == 'worker':
        available = [pair for pair in _SUPPORTED_PAIRS if pair[1] == platform]
    example = '/coding-team status'
    if available:
        example = ('On Windows, send /coding-team agy auto on in Codex, then /coding-team listen '
                   'in Antigravity on the same Git worktree.')
    return {
        'help': list(_HELP_LINES),
        'supported_pairs': list(_SUPPORTED_PAIRS),
        'available_pairs': available,
        'example': example,
        'policy': list(ct_policy.HELP_LINES),
    }
