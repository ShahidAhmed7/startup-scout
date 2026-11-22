"""The three opportunity tracks, in display order, and every name each one goes by."""

MARKETS = ("bangladesh", "global", "service")

CONCEPT = {"bangladesh": "concept_bd", "global": "concept_global", "service": "concept_service"}
SHORT = {"bangladesh": "bd", "global": "gl", "service": "sv"}  # shortlist row keys
RANK = {"bangladesh": "rank_bd", "global": "rank_global", "service": "rank_service"}
PLAY = {"bangladesh": "bangladesh_play", "global": "global_play", "service": "service_play"}
LABEL = {"bangladesh": "Bangladesh", "global": "Global", "service": "Service"}
ROW_LABEL = {"bangladesh": "BD", "global": "Global", "service": "Service"}
SECTION = {
    "bangladesh": "For Bangladesh",
    "global": "Global from Bangladesh",
    "service": "Service from Bangladesh",
}
CARD_HEADING = {
    "bangladesh": "In Bangladesh",
    "global": "Abroad from Bangladesh",
    "service": "Service from Bangladesh",
}
PROMPT_NAME = {
    "bangladesh": "Bangladesh",
    "global": "global (a product sold abroad from Bangladesh)",
    "service": "service (done-for-you work for clients, delivered through software by a team in Bangladesh)",
}


def present(j: dict) -> list[str]:
    """Tracks a stored score has. Scores from before the service track have only Bangladesh and global."""
    return [m for m in MARKETS if j.get(m) is not None]


def value(j: dict, market: str) -> float:
    return float(j.get(market) or 0)
