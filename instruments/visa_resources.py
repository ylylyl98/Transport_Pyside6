from __future__ import annotations

import re
from collections.abc import Iterable


_GPIB_RESOURCE = re.compile(r"^GPIB(?P<interface>\d+)(?P<tail>::.+)$", re.IGNORECASE)


def alternate_gpib_resources(address: str, resources: Iterable[str]) -> tuple[str, ...]:
    """Return GPIB resources for the same device on another interface number.

    A resource is considered the same device only when everything after the
    GPIB interface number matches.  For example, GPIB0::23::INSTR is a safe
    alternative for GPIB1::23::INSTR, while GPIB0::24::INSTR is not.
    """
    configured = str(address or "").strip()
    match = _GPIB_RESOURCE.fullmatch(configured)
    if match is None:
        return ()

    configured_key = configured.casefold()
    tail_key = match.group("tail").casefold()
    matches: list[str] = []
    seen: set[str] = set()
    for resource in resources:
        candidate = str(resource).strip()
        candidate_match = _GPIB_RESOURCE.fullmatch(candidate)
        candidate_key = candidate.casefold()
        if (
            candidate_match is None
            or candidate_key == configured_key
            or candidate_match.group("tail").casefold() != tail_key
            or candidate_key in seen
        ):
            continue
        seen.add(candidate_key)
        matches.append(candidate)
    return tuple(matches)


def resolve_gpib_resource(address: str, resources: Iterable[str]) -> str:
    """Resolve a stale GPIB interface number when there is one safe match."""
    configured = str(address or "").strip()
    discovered = tuple(str(resource).strip() for resource in resources)

    for resource in discovered:
        if resource.casefold() == configured.casefold():
            return resource

    alternatives = alternate_gpib_resources(configured, discovered)
    if len(alternatives) == 1:
        return alternatives[0]
    return configured
