"""Registry of extraction profiles. Profile modules call register() on import."""

PROFILES: list = []


def register(profile) -> None:
    if profile not in PROFILES:
        PROFILES.append(profile)


def get(name: str):
    return next((p for p in PROFILES if p.name == name), None)
