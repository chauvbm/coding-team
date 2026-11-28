"""Managed role contract and strict structured work assignment validation."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
from types import MappingProxyType
from typing import Literal, Mapping

import ct_roles

_SHA256 = re.compile(r'[0-9a-f]{64}', re.ASCII)
_HEAD = re.compile(r'(?:[0-9a-f]{40}|[0-9a-f]{64})', re.ASCII)
_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}', re.ASCII)
_ASSIGNMENT_KEYS = frozenset({
    'variant', 'work_item_id', 'depends_on', 'snapshot', 'read_paths', 'write_paths',
    'check_outputs', 'test_paths', 'review_scope', 'required_model',
})


@dataclass(frozen=True)
class RoleContract:
    source_root: Path
    roles: Mapping[str, Mapping]
    contract_digest: str


@dataclass(frozen=True)
class RoleAssignment:
    variant: str
    work_item_id: str
    depends_on: Mapping[str, str]
    snapshot: Mapping[str, object]
    read_paths: tuple[str, ...]
    write_paths: tuple[str, ...]
    check_outputs: tuple[str, ...]
    test_paths: tuple[str, ...]
    review_scope: tuple[str, ...] | None
    required_model: str | None
    role: str
    capabilities: tuple[str, ...]
    desktop_guarantees: Mapping[str, str]
    contract_digest: str
    verified_model: str


def load_contract(source_root: Path) -> RoleContract:
    """Load the managed role catalog and bind the contract to its source bytes."""
    root = Path(source_root).resolve()
    roles_dir = root / 'agents' / 'roles'
    roles = ct_roles.load_roles(roles_dir)
    digest = hashlib.sha256()
    for path in [roles_dir / 'roles.json', *(roles_dir / f'{name}.md' for name in sorted(roles))]:
        rel = path.relative_to(root).as_posix().encode('utf-8')
        raw = path.read_bytes()
        digest.update(len(rel).to_bytes(4, 'big'))
        digest.update(rel)
        digest.update(len(raw).to_bytes(8, 'big'))
        digest.update(raw)
    frozen_roles = MappingProxyType({
        role: MappingProxyType({
            **data,
            'variants': MappingProxyType({key: tuple(value)
                                          for key, value in data['variants'].items()}),
        }) for role, data in roles.items()
    })
    return RoleContract(root, frozen_roles, digest.hexdigest())


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise ValueError(reason)


def _identifier(value, label):
    _require(isinstance(value, str) and _ID.fullmatch(value) is not None,
             f'{label} must be a plain identifier')
    return value


def _path_list(value, label):
    _require(isinstance(value, list), f'{label} must be a list')
    result = []
    seen = set()
    for item in value:
        _require(isinstance(item, str) and item and '\x00' not in item
                 and '\\' not in item and ':' not in item
                 and not PurePosixPath(item).is_absolute()
                 and not PureWindowsPath(item).is_absolute()
                 and all(part not in ('', '.', '..') for part in item.split('/')),
                 f'{label} contains an unsafe relative path')
        _require(item not in seen, f'{label} contains duplicates')
        seen.add(item)
        result.append(item)
    return tuple(result)


def _snapshot(value):
    _require(isinstance(value, dict) and set(value) == {'head', 'inputs'},
             'snapshot must contain exactly head and inputs')
    head = value['head']
    _require(isinstance(head, str) and _HEAD.fullmatch(head) is not None,
             'snapshot head must be a full Git object ID')
    inputs = value['inputs']
    _require(isinstance(inputs, dict), 'snapshot inputs must be a mapping')
    normalized = {}
    for path, digest in inputs.items():
        checked = _path_list([path], 'snapshot inputs')[0]
        _require(digest == 'ABSENT' or
                 isinstance(digest, str) and _SHA256.fullmatch(digest) is not None,
                 'snapshot input must be SHA-256 or ABSENT')
        normalized[checked] = digest
    return MappingProxyType({'head': head, 'inputs': MappingProxyType(normalized)})


def validate_assignment(contract: RoleContract, raw: dict, *,
                        verified_model: str | None) -> RoleAssignment:
    """Validate an untrusted handoff against managed role capabilities."""
    _require(isinstance(contract, RoleContract), 'invalid managed role contract')
    _require(isinstance(raw, dict) and _ASSIGNMENT_KEYS >= raw.keys(),
             'assignment must be a mapping with known fields')
    required = {'variant', 'work_item_id', 'snapshot', 'read_paths', 'write_paths',
                'check_outputs', 'test_paths'}
    _require(required <= raw.keys(), 'assignment is missing required fields')
    variant = _identifier(raw['variant'], 'variant')
    role = next((name for name, data in contract.roles.items()
                 if variant in data['variants']), None)
    _require(role is not None, 'unknown role variant')
    caps = tuple(contract.roles[role]['variants'][variant])
    # An existing Desktop chat runs the handoff skill, not a generated native
    # role with an allowlist. File and command restrictions here are advisory.
    guarantees = ct_roles.guarantees(caps, {'agent_format': 'antigravity-md',
                                          'tool_allowlist_enforced': False})
    work_item_id = _identifier(raw['work_item_id'], 'work_item_id')
    depends = raw.get('depends_on', {})
    _require(isinstance(depends, dict), 'depends_on must be a mapping')
    checked_depends = {}
    for key, acceptance in depends.items():
        _identifier(key, 'dependency work_item_id')
        _identifier(acceptance, 'dependency acceptance_id')
        _require(key != work_item_id, 'work item cannot depend on itself')
        checked_depends[key] = acceptance
    snapshot = _snapshot(raw['snapshot'])
    read_paths = _path_list(raw['read_paths'], 'read_paths')
    write_paths = _path_list(raw['write_paths'], 'write_paths')
    check_outputs = _path_list(raw['check_outputs'], 'check_outputs')
    test_paths = _path_list(raw['test_paths'], 'test_paths')
    review_scope = None
    if 'review_scope' in raw:
        review_scope = _path_list(raw['review_scope'], 'review_scope')
        _require(any(cap == 'read' for cap in caps), 'role cannot receive review scope')
        _require(set(review_scope) <= set(read_paths), 'review scope must be within read_paths')

    if 'write:assigned' in caps:
        _require(bool(write_paths), 'write-capable role requires an assigned write scope')
        _require(not check_outputs, 'only reviewer-verify may receive check outputs')
    elif 'write:tests' in caps:
        _require(bool(write_paths) and bool(test_paths), 'test engineer requires a test write scope')
        _require(set(write_paths) <= set(test_paths), 'test engineer writes must be within test_paths')
        _require(not check_outputs, 'only reviewer-verify may receive check outputs')
    else:
        _require(not write_paths, 'read-only role cannot receive write scope')

    if variant == 'ct-reviewer-verify':
        _require('shell:checks' in caps, 'reviewer-verify requires check capability')
        _require(not write_paths and not test_paths, 'reviewer-verify is READ-only')
    else:
        _require(not check_outputs, 'only reviewer-verify may receive check outputs')
    if variant == 'ct-reviewer':
        _require(not check_outputs and not test_paths,
                 'reviewer without verification cannot receive check outputs')
    _require('delegate' not in caps and 'agent' not in caps,
             'role catalog must not grant self-delegation')

    required_model = raw.get('required_model')
    if required_model is not None:
        _require(isinstance(required_model, str) and required_model.strip() == required_model
                 and bool(required_model), 'required_model must be a non-empty string')
        _require(verified_model == required_model,
                 'required model is not verified by trusted runtime metadata')
    verified = verified_model if isinstance(verified_model, str) and verified_model else 'unverified'
    return RoleAssignment(variant, work_item_id, MappingProxyType(checked_depends), snapshot, read_paths,
                          write_paths, check_outputs, test_paths, review_scope,
                          required_model, role, caps, MappingProxyType(guarantees),
                          contract.contract_digest, verified)


def assignment_kind(assignment: RoleAssignment) -> Literal['READ', 'EDIT']:
    if not isinstance(assignment, RoleAssignment):
        raise ValueError('expected a validated RoleAssignment')
    return 'EDIT' if any(cap.startswith('write:') for cap in assignment.capabilities) else 'READ'


def assignment_to_dict(assignment: RoleAssignment, *, include_derived=True) -> dict:
    """Return an explicit canonical mapping with only validated/derived fields."""
    if not isinstance(assignment, RoleAssignment):
        raise ValueError('expected a validated RoleAssignment')
    raw = {
        'variant': assignment.variant,
        'work_item_id': assignment.work_item_id,
        'depends_on': dict(sorted(assignment.depends_on.items())),
        'snapshot': {'head': assignment.snapshot['head'],
                     'inputs': dict(sorted(assignment.snapshot['inputs'].items()))},
        'read_paths': list(assignment.read_paths),
        'write_paths': list(assignment.write_paths),
        'check_outputs': list(assignment.check_outputs),
        'test_paths': list(assignment.test_paths),
        **({'review_scope': list(assignment.review_scope)}
           if assignment.review_scope is not None else {}),
        **({'required_model': assignment.required_model}
           if assignment.required_model is not None else {}),
    }
    if not include_derived:
        return raw
    return {**raw, 'role': assignment.role,
        'capabilities': list(assignment.capabilities),
        'desktop_guarantees': dict(assignment.desktop_guarantees),
        'contract_digest': assignment.contract_digest,
        'verified_model': assignment.verified_model,
    }


def canonical_assignment_json(assignment: RoleAssignment) -> str:
    return json.dumps(assignment_to_dict(assignment, include_derived=False), sort_keys=True, separators=(',', ':'))
