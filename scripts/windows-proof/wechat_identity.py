"""Account-bound evidence for local message direction; no display-name guesses.

The producer proposes an ID from the active account directory, corroborates it
against the contact table and an already-observed one-to-one conversation, and
records that proof in private metadata. Consumers all use this validator.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import re

IDENTIFIER = re.compile(r'[A-Za-z0-9_@.-]{1,256}\Z')
DIGEST = re.compile(r'[0-9a-f]{64}\Z')
SYSTEM_PEERS = {'filehelper', 'weixin', 'fmessage', 'medianote', 'floatbottle', 'newsapp',
                'notifymessage', 'brandsessionholder', 'officialaccounts'}
METHOD = 'private_peer_account_match'


def valid_sender(value):
    return isinstance(value, str) and IDENTIFIER.fullmatch(value) is not None and not value.endswith('@chatroom')


def account_candidates(root):
    """Directory conventions are candidates, never identity verification."""
    root = Path(root)
    if root.name != 'db_storage':
        return ()
    name = root.parent.name
    candidates = {name} if valid_sender(name) else set()
    suffix = re.fullmatch(r'(.+)_([0-9a-fA-F]{4})', name)
    if suffix and valid_sender(suffix[1]):
        candidates.add(suffix[1])
    return tuple(sorted(candidates))


def unknown_identity(binding_digest, reason='insufficient_evidence'):
    return {'schema_version': 1, 'state': 'unknown', 'binding_digest': binding_digest, 'reason': reason}


def establish_identity(binding_digest, candidates, contact_matches, conversations, observed_senders):
    """Only already-collected normal private-message sender IDs are examined."""
    unknown = unknown_identity(binding_digest)
    if not isinstance(binding_digest, str) or DIGEST.fullmatch(binding_digest) is None:
        return unknown
    candidates = {value for value in candidates if valid_sender(value)}
    matched = {value for value in contact_matches if valid_sender(value)} & candidates
    if len(matched) != 1:
        return unknown_identity(binding_digest, 'account_candidate_not_unique')
    candidate = next(iter(matched))
    if candidate in SYSTEM_PEERS or candidate.startswith('gh_'):
        return unknown
    proofs = []
    for chat in conversations:
        if not isinstance(chat, dict):
            continue
        peer, alias = chat.get('chat_id'), chat.get('alias')
        if (not valid_sender(peer) or peer == candidate or peer in SYSTEM_PEERS
                or peer.startswith('gh_') or not isinstance(alias, str)):
            continue
        senders = observed_senders.get(alias, ())
        if not isinstance(senders, (tuple, list, set)) or any(not valid_sender(value) for value in senders):
            continue
        other = set(senders) - {peer}
        if other and other != {candidate}:
            return unknown_identity(binding_digest, 'private_sender_conflict')
        if other == {candidate}:
            proofs.append((alias, peer))
    if not proofs:
        return unknown
    alias, peer = sorted(proofs)[0]
    return {'schema_version': 1, 'state': 'verified', 'self_sender_id': candidate,
            'method': METHOD, 'binding_digest': binding_digest,
            'verified_at': datetime.now(timezone.utc).isoformat(),
            'proof': {'account_candidate_matched': True, 'contact_candidate_matched': True,
                      'private_non_peer_unique': True, 'private_peer_id': peer, 'private_alias': alias}}


def trusted_self_identity(source, binding_digest):
    if (not isinstance(source, dict) or not isinstance(binding_digest, str)
            or DIGEST.fullmatch(binding_digest) is None or source.get('binding_digest') != binding_digest):
        return None
    identity = source.get('identity')
    if (not isinstance(identity, dict) or type(identity.get('schema_version')) is not int
            or identity['schema_version'] != 1 or identity.get('state') != 'verified'
            or identity.get('method') != METHOD or identity.get('binding_digest') != binding_digest):
        return None
    identifier, proof = identity.get('self_sender_id'), identity.get('proof')
    if (not valid_sender(identifier) or identifier in SYSTEM_PEERS or identifier.startswith('gh_')
            or not isinstance(proof, dict)
            or any(proof.get(flag) is not True for flag in ('account_candidate_matched',
                    'contact_candidate_matched', 'private_non_peer_unique'))):
        return None
    peer = proof.get('private_peer_id')
    if not valid_sender(peer) or peer == identifier or peer in SYSTEM_PEERS or peer.startswith('gh_'):
        return None
    return identifier


def message_direction(source, binding_digest, sender_id):
    own = trusted_self_identity(source, binding_digest)
    if own is None or not valid_sender(sender_id):
        return 'unknown'
    return 'outgoing' if sender_id == own else 'incoming'
