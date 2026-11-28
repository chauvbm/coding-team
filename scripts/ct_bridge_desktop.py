"""Plan preparation of open Desktop tabs from current bridge and UI observations."""
from __future__ import annotations

import re

from ct_bridge_control import connection_status


EPOCHS = ('leader_epoch', 'desktop_epoch', 'auto_epoch')
_IDENTIFIER = re.compile(r'[A-Za-z0-9_-]{1,200}')
_IDLE_STATES = ('AUTO_READY', 'WAITING')


def _is_id(value):
    return isinstance(value, str) and _IDENTIFIER.fullmatch(value) is not None


def _is_model(value):
    return value is None or (isinstance(value, str) and 0 < len(value) <= 160
                             and value == value.strip() and value.isprintable())


def _observed_tabs(rows):
    if not isinstance(rows, list):
        raise ValueError('INVALID_OPEN_TABS')
    tabs = {}
    for row in rows:
        if (not isinstance(row, dict) or row.keys() != {'chat_id', 'model', 'idle'}
                or not _is_id(row['chat_id']) or not _is_model(row['model'])
                or type(row['idle']) is not bool or row['chat_id'] in tabs):
            raise ValueError('INVALID_OPEN_TABS')
        tabs[row['chat_id']] = row
    return tabs


def prepare_worker(ctx, payload):
    required = {*EPOCHS, 'tabs'}
    if not required <= payload.keys() <= required | {'preferred_model', 'affinity_worker_id'}:
        raise ValueError('INVALID_PAYLOAD')
    if (any(type(payload[name]) is not int or payload[name] < 0 for name in EPOCHS)
            or payload['leader_epoch'] == 0):
        raise ValueError('INVALID_PAYLOAD')
    preferred = payload.get('preferred_model')
    affinity = payload.get('affinity_worker_id')
    if not _is_model(preferred) or (affinity is not None and not _is_id(affinity)):
        raise ValueError('INVALID_PAYLOAD')
    tabs = _observed_tabs(payload['tabs'])

    ctx.require_leader()
    status = connection_status(ctx, include_worker_chat_ids=True)
    if not status['ok']:
        raise ValueError(status['reason'])
    if status['role'] != 'leader':
        raise ValueError('NOT_LEADER')
    if any(status[name] != payload[name] for name in EPOCHS):
        raise ValueError('STALE_ACTIVATION')
    if status['effective_policy'] != 'ON':
        raise ValueError('NATIVE_POLICY_OFF')
    if not status['desktop_enabled']:
        raise ValueError('DESKTOP_OFF')
    if not status['auto_enabled']:
        raise ValueError('AUTO_OFF')

    workers = {worker['chat_id']: worker for worker in status['connections'][0]['workers']}
    candidates = []
    for chat_id, tab in tabs.items():
        worker = workers.get(chat_id)
        worker_id = worker['worker_id'] if worker else None
        readiness = worker['readiness'] if worker else 'UNPAIRED'
        if (not tab['idle'] or (worker and readiness not in _IDLE_STATES)
                or (affinity is not None and worker_id != affinity)):
            continue
        model = tab['model']
        needs_model = preferred is not None and model != preferred
        if readiness == 'UNPAIRED':
            action, rank = 'PAIR_REQUIRED', 4
        elif readiness == 'WAITING':
            action, rank = 'LISTEN_REQUIRED', 3
        elif model is None:
            action, rank = 'VERIFY_MODEL', 2
        elif needs_model:
            action, rank = 'SET_MODEL', 1
        else:
            action, rank = 'READY', 0
        target = {'chat_id': chat_id, 'worker_id': worker_id, 'listener': readiness,
                  'observed_model': model, 'preferred_model': preferred}
        candidates.append((rank, needs_model, chat_id, action, target))
    if not candidates:
        reason = 'AFFINITY_UNAVAILABLE' if affinity else 'NO_IDLE_TAB'
        return {'ok': False, 'state': 'NOT_READY', 'ready': False, 'reason': reason}
    _, _, _, action, target = min(candidates)
    return {'ok': True, 'state': action, 'ready': action == 'READY', 'target': target,
            'workspace': status['workspace'], **{name: status[name] for name in EPOCHS}}
