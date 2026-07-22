"""Live football-score lookups backed by API-Football."""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from typing import Any

import requests

API_BASE_URL = "https://v3.football.api-sports.io"
WIKIPEDIA_API_URL = "https://en.wikipedia.org/w/api.php"
DEFAULT_CACHE_TTL_SECONDS = 45.0
DEFAULT_TOURNAMENT_CACHE_TTL_SECONDS = 6 * 60 * 60.0


class FootballNotConfiguredError(RuntimeError):
    """No API-Football key has been configured."""


class FootballUnavailableError(RuntimeError):
    """The football provider did not return usable live-score data."""


class TournamentResultUnavailableError(RuntimeError):
    """A current FIFA World Cup result could not be confirmed."""


@dataclass(frozen=True)
class _CachedMatches:
    expires_at: float
    matches: list[dict[str, Any]]


@dataclass(frozen=True)
class _CachedWinner:
    expires_at: float
    winner: str


@dataclass(frozen=True)
class _CachedTopScorer:
    expires_at: float
    player: str
    goals: int


class FootballService:
    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        cache_ttl_seconds: float = DEFAULT_CACHE_TTL_SECONDS,
        request_timeout_seconds: float = 5.0,
    ) -> None:
        self.api_key = (
            api_key if api_key is not None else os.environ.get("FOOTBALL_API_KEY", "")
        ).strip()
        self.base_url = (
            base_url
            if base_url is not None
            else os.environ.get("FOOTBALL_API_BASE_URL", API_BASE_URL)
        ).rstrip("/")
        self.cache_ttl_seconds = max(0.0, cache_ttl_seconds)
        self.request_timeout_seconds = request_timeout_seconds
        self._cache: _CachedMatches | None = None
        self._lock = threading.Lock()

    def live_matches(self) -> list[dict[str, Any]]:
        """Return live matches, cached briefly to respect the provider quota."""
        if not self.api_key:
            raise FootballNotConfiguredError("FOOTBALL_API_KEY is not configured")

        now = time.monotonic()
        with self._lock:
            if self._cache and self._cache.expires_at > now:
                return list(self._cache.matches)

        try:
            response = requests.get(
                f"{self.base_url}/fixtures",
                params={"live": "all"},
                headers={"x-apisports-key": self.api_key},
                timeout=self.request_timeout_seconds,
            )
            response.raise_for_status()
            body = response.json()
            matches = body.get("response")
            if not isinstance(matches, list):
                raise FootballUnavailableError("Football provider returned invalid data")
            normalized = [
                match for match in matches if isinstance(match, dict) and _is_live(match)
            ]
        except FootballUnavailableError:
            raise
        except (requests.RequestException, ValueError, TypeError) as exc:
            raise FootballUnavailableError("Could not retrieve live football data") from exc

        with self._lock:
            self._cache = _CachedMatches(
                expires_at=time.monotonic() + self.cache_ttl_seconds,
                matches=normalized,
            )
        return list(normalized)


class FifaTournamentService:
    """Read published FIFA World Cup winner information without an API key."""

    def __init__(
        self,
        *,
        cache_ttl_seconds: float = DEFAULT_TOURNAMENT_CACHE_TTL_SECONDS,
        request_timeout_seconds: float = 5.0,
    ) -> None:
        self.cache_ttl_seconds = max(0.0, cache_ttl_seconds)
        self.request_timeout_seconds = request_timeout_seconds
        self._cache: dict[int, _CachedWinner] = {}
        self._top_scorer_cache: dict[int, _CachedTopScorer] = {}
        self._lock = threading.Lock()

    def world_cup_winner(self, year: int) -> str:
        if not 1930 <= year <= 2100:
            raise TournamentResultUnavailableError("Invalid World Cup year")

        now = time.monotonic()
        with self._lock:
            cached = self._cache.get(year)
            if cached and cached.expires_at > now:
                return cached.winner

        try:
            response = requests.get(
                WIKIPEDIA_API_URL,
                params={
                    "action": "query",
                    "prop": "extracts",
                    "exintro": 1,
                    "explaintext": 1,
                    "redirects": 1,
                    "titles": f"{year} FIFA World Cup",
                    "format": "json",
                },
                headers={"User-Agent": "NiNO-demo/1.0 (local voice assistant)"},
                timeout=self.request_timeout_seconds,
            )
            response.raise_for_status()
            pages = response.json().get("query", {}).get("pages", {})
            if not isinstance(pages, dict) or not pages:
                raise TournamentResultUnavailableError("Tournament page was unavailable")
            extract = str(next(iter(pages.values())).get("extract") or "")
            winner = _winner_from_summary(extract)
            if not winner:
                raise TournamentResultUnavailableError("Tournament winner was not published")
        except TournamentResultUnavailableError:
            raise
        except (requests.RequestException, ValueError, TypeError, AttributeError) as exc:
            raise TournamentResultUnavailableError(
                "Could not retrieve tournament information"
            ) from exc

        with self._lock:
            self._cache[year] = _CachedWinner(
                expires_at=time.monotonic() + self.cache_ttl_seconds,
                winner=winner,
            )
        return winner

    def world_cup_top_scorer(self, year: int) -> tuple[str, int]:
        if not 1930 <= year <= 2100:
            raise TournamentResultUnavailableError("Invalid World Cup year")

        now = time.monotonic()
        with self._lock:
            cached = self._top_scorer_cache.get(year)
            if cached and cached.expires_at > now:
                return cached.player, cached.goals

        try:
            goalscorers_html = self._world_cup_goalscorers_html(year)
            top_scorer = _top_scorer_from_goalscorers_html(goalscorers_html)
            if top_scorer is None:
                raise TournamentResultUnavailableError("Top scorer was not published")
        except TournamentResultUnavailableError:
            raise
        except (requests.RequestException, ValueError, TypeError, AttributeError) as exc:
            raise TournamentResultUnavailableError(
                "Could not retrieve tournament information"
            ) from exc

        player, goals = top_scorer
        with self._lock:
            self._top_scorer_cache[year] = _CachedTopScorer(
                expires_at=time.monotonic() + self.cache_ttl_seconds,
                player=player,
                goals=goals,
            )
        return player, goals

    def _world_cup_extract(self, year: int, *, intro_only: bool) -> str:
        params: dict[str, object] = {
            "action": "query",
            "prop": "extracts",
            "explaintext": 1,
            "redirects": 1,
            "titles": f"{year} FIFA World Cup",
            "format": "json",
        }
        if intro_only:
            params["exintro"] = 1
        response = requests.get(
            WIKIPEDIA_API_URL,
            params=params,
            headers={"User-Agent": "NiNO-demo/1.0 (local voice assistant)"},
            timeout=self.request_timeout_seconds,
        )
        response.raise_for_status()
        pages = response.json().get("query", {}).get("pages", {})
        if not isinstance(pages, dict) or not pages:
            raise TournamentResultUnavailableError("Tournament page was unavailable")
        extract = str(next(iter(pages.values())).get("extract") or "")
        if not extract:
            raise TournamentResultUnavailableError("Tournament information was unavailable")
        return extract

    def _world_cup_goalscorers_html(self, year: int) -> str:
        title = f"{year} FIFA World Cup"
        headers = {"User-Agent": "NiNO-demo/1.0 (local voice assistant)"}
        sections_response = requests.get(
            WIKIPEDIA_API_URL,
            params={"action": "parse", "page": title, "prop": "sections", "format": "json"},
            headers=headers,
            timeout=self.request_timeout_seconds,
        )
        sections_response.raise_for_status()
        sections = sections_response.json().get("parse", {}).get("sections", [])
        if not isinstance(sections, list):
            raise TournamentResultUnavailableError("Tournament statistics were unavailable")
        section = next(
            (
                item
                for item in sections
                if isinstance(item, dict)
                and str(item.get("line") or "").strip().lower() == "goalscorers"
            ),
            None,
        )
        if not isinstance(section, dict) or not section.get("index"):
            raise TournamentResultUnavailableError("Tournament scorer list was unavailable")

        scores_response = requests.get(
            WIKIPEDIA_API_URL,
            params={
                "action": "parse",
                "page": title,
                "section": section["index"],
                "prop": "text",
                "format": "json",
            },
            headers=headers,
            timeout=self.request_timeout_seconds,
        )
        scores_response.raise_for_status()
        html = scores_response.json().get("parse", {}).get("text", {}).get("*")
        if not isinstance(html, str) or not html:
            raise TournamentResultUnavailableError("Tournament scorer list was unavailable")
        return html


def _winner_from_summary(summary: str) -> str | None:
    import re

    match = re.search(
        r"\bwith ([A-Z][A-Za-z .'-]{1,60}?) winning the championship\b", summary
    )
    if not match:
        match = re.search(r"\bwon by ([A-Z][A-Za-z .'-]{1,60}?)(?:[.,;]|$)", summary)
    return match.group(1).strip() if match else None


def _top_scorer_from_goalscorers_html(html: str) -> tuple[str, int] | None:
    import html as html_parser
    import re

    goal_block = re.search(
        r"<p><b>(\d+) goals</b>.*?(?=<p><b>\d+ goals</b>|$)",
        html,
        re.DOTALL,
    )
    if not goal_block:
        return None
    titles = re.findall(r'title="([^"]+)"', goal_block.group(0))
    player = next(
        (
            html_parser.unescape(title).strip()
            for title in titles
            if "national football team" not in title.lower()
        ),
        None,
    )
    return (player, int(goal_block.group(1))) if player else None


def _is_live(match: dict[str, Any]) -> bool:
    fixture = match.get("fixture")
    status = fixture.get("status") if isinstance(fixture, dict) else None
    short = str(status.get("short") or "").upper() if isinstance(status, dict) else ""
    return short in {"1H", "HT", "2H", "ET", "BT", "P", "LIVE", "INT"}


def live_football_voice_reply(matches: list[dict[str, Any]]) -> str:
    """Turn the provider payload into a short, spoken live-score update."""
    if not matches:
        return "There are no live football matches in the current score feed right now."

    summaries = [_spoken_match(match) for match in matches[:2]]
    if len(summaries) == 1:
        return f"Live football update: {summaries[0]}"
    return f"Live football updates: {summaries[0]} Also, {summaries[1]}"


def _spoken_match(match: dict[str, Any]) -> str:
    teams = match.get("teams") if isinstance(match.get("teams"), dict) else {}
    goals = match.get("goals") if isinstance(match.get("goals"), dict) else {}
    league = match.get("league") if isinstance(match.get("league"), dict) else {}
    fixture = match.get("fixture") if isinstance(match.get("fixture"), dict) else {}
    status = fixture.get("status") if isinstance(fixture.get("status"), dict) else {}

    home = str((teams.get("home") or {}).get("name") or "Home") if isinstance(
        teams.get("home"), dict
    ) else "Home"
    away = str((teams.get("away") or {}).get("name") or "Away") if isinstance(
        teams.get("away"), dict
    ) else "Away"
    home_goals = _score(goals.get("home"))
    away_goals = _score(goals.get("away"))
    competition = str(league.get("name") or "the current competition")
    phase = _spoken_phase(status)
    return f"{home} {home_goals}, {away} {away_goals}, {phase} in {competition}."


def _score(value: object) -> int:
    try:
        return int(value) if value is not None else 0
    except (TypeError, ValueError):
        return 0


def _spoken_phase(status: dict[str, Any]) -> str:
    short = str(status.get("short") or "").upper()
    if short == "HT":
        return "at half time"
    elapsed = status.get("elapsed")
    try:
        return f"in the {_ordinal(int(elapsed))} minute"
    except (TypeError, ValueError):
        return "live"


def _ordinal(value: int) -> str:
    if 10 <= value % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(value % 10, "th")
    return f"{value}{suffix}"


_FOOTBALL_SERVICE: FootballService | None = None
_FIFA_TOURNAMENT_SERVICE: FifaTournamentService | None = None


def get_football_service() -> FootballService:
    global _FOOTBALL_SERVICE
    if _FOOTBALL_SERVICE is None:
        _FOOTBALL_SERVICE = FootballService()
    return _FOOTBALL_SERVICE


def get_fifa_tournament_service() -> FifaTournamentService:
    global _FIFA_TOURNAMENT_SERVICE
    if _FIFA_TOURNAMENT_SERVICE is None:
        _FIFA_TOURNAMENT_SERVICE = FifaTournamentService()
    return _FIFA_TOURNAMENT_SERVICE
