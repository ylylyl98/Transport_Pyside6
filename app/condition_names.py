"""Unique names for newly added and duplicated recipe rows."""
import re


def unique_condition_name(existing, source=None, *, prefix="Con"):
    used = {str(name).strip().casefold() for name in existing if name is not None}
    base = re.sub(r"(?:\s+copy)+$", "", str(source or "").strip(), flags=re.IGNORECASE).strip()
    if not base or re.fullmatch(r"Con\d+", base, flags=re.IGNORECASE):
        index = 1
        while f"{prefix}{index}".casefold() in used:
            index += 1
        return f"{prefix}{index}"
    # Repeated duplication of a generated suffix advances the same family.
    numbered = re.fullmatch(r"(.+?) (\d+)", base)
    if numbered and numbered[1].casefold() in used:
        base = numbered[1]
    index = 2
    while f"{base} {index}".casefold() in used:
        index += 1
    return f"{base} {index}"
