"""Plex Media Server library client (Movies / Shows)."""

from __future__ import annotations

import re
from urllib.parse import quote

from app import __version__
from app.models import MediaItem
from app.providers import HttpClient

# Plex library section types we turn into wallpapers. Music/photo sections are ignored.
_SECTION_TYPES = {"movie", "show"}
_TYPE_IDS = {"movie": "1", "show": "2"}
_PARSE_TYPES = frozenset({"movie", "show"})
_IMDB_RE = re.compile(r"(tt\d{5,})", re.IGNORECASE)
# tmdb://438631 and legacy com.plexapp.agents.themoviedb://438631?lang=en
_TMDB_RE = re.compile(r"(?:tmdb|themoviedb)://(\d+)", re.IGNORECASE)


def _normalize_url(url: str) -> str:
    """Plex users often paste ``192.168.1.10:32400``; assume HTTP for them."""
    text = (url or "").strip().rstrip("/")
    if text and not text.lower().startswith(("http://", "https://")):
        text = f"http://{text}"
    return text


def _as_list(value) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _float(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _section_ids(value) -> list[str]:
    if isinstance(value, (list, tuple, set)):
        parts = [str(part).strip() for part in value]
    else:
        parts = [part.strip() for part in str(value or "").split(",")]
    return [part for part in parts if part]


def _watch_state(raw: dict) -> str:
    """Map Plex movie/show progress to the suite's normalized watch states.

    Plex reports per-video ``viewOffset`` (milliseconds) for resume state and
    per-show ``viewedLeafCount`` / ``leafCount`` for episode progress. Shows do
    not get a single ``viewOffset``, so aggregate leaf counts matter.
    """
    if _int(raw.get("viewCount")) > 0:
        return "watched"
    if _int(raw.get("viewOffset")) > 0:
        return "partial"
    viewed = _int(raw.get("viewedLeafCount"))
    leaves = _int(raw.get("leafCount"))
    if viewed > 0:
        if leaves > 0 and viewed >= leaves:
            return "watched"
        return "partial"
    return "unwatched"


def _year_from_plex(raw: dict) -> int | None:
    year = raw.get("year")
    if year is not None and str(year).strip() != "":
        try:
            return int(year)
        except (TypeError, ValueError):
            pass
    date = str(raw.get("originallyAvailableAt") or "")
    if len(date) >= 4 and date[:4].isdigit():
        return int(date[:4])
    return None


def _genres_from_plex(raw: dict) -> list[str]:
    out: list[str] = []
    for entry in _as_list(raw.get("Genre")):
        if isinstance(entry, dict):
            name = entry.get("tag") or entry.get("title") or entry.get("name")
        else:
            name = entry
        text = str(name or "").strip()
        if text and text not in out:
            out.append(text)
    return out


def _runtime_from_ms(duration_ms) -> str:
    millis = _int(duration_ms)
    if millis <= 0:
        return ""
    minutes = int(millis / 1000 / 60)
    hours, mins = divmod(minutes, 60)
    return f"{hours}h {mins}m" if hours else f"{mins}m"


def _ids_from_plex(raw: dict) -> tuple[str | None, str | None]:
    """External ids from new ``Guid`` entries or the older single ``guid`` field."""
    candidates: list[str] = []
    for entry in _as_list(raw.get("Guid")):
        value = entry.get("id") if isinstance(entry, dict) else entry
        if value:
            candidates.append(str(value))
    if raw.get("guid"):
        candidates.append(str(raw.get("guid")))
    tmdb_id: str | None = None
    imdb_id: str | None = None
    for value in candidates:
        imdb = _IMDB_RE.search(value)
        if imdb and not imdb_id:
            imdb_id = imdb.group(1)
        tmdb = _TMDB_RE.search(value)
        if tmdb and not tmdb_id:
            tmdb_id = tmdb.group(1)
    return tmdb_id, imdb_id


def _title_from_plex(raw: dict) -> str:
    # Whole movies/shows only: Plex "title" is already what the chrome wants.
    return str(raw.get("title") or raw.get("parentTitle") or "Untitled").strip() or "Untitled"


def _image_path(raw: dict, key: str) -> str:
    value = raw.get(key)
    return str(value).strip() if value else ""


class PlexProvider:
    name = "plex"

    def __init__(
        self,
        url: str = "",
        api_key: str = "",
        library_ids: str = "",
        client: HttpClient | None = None,
    ):
        self.url = _normalize_url(url)
        self.api_key = api_key or ""
        self.library_ids = _section_ids(library_ids)
        self.client = client or HttpClient()
        self._machine_identifier = ""
        self._machine_loaded = False

    def is_configured(self) -> bool:
        return bool(self.url and self.api_key)

    def auth_headers(self) -> dict[str, str]:
        """Plex token + client identity. Prefer headers over URL query strings."""
        return {
            "X-Plex-Token": self.api_key,
            "X-Plex-Client-Identifier": "wallpaparr",
            "X-Plex-Product": "Wallpaparr",
            "X-Plex-Version": __version__,
            "X-Plex-Device": "Server",
            "X-Plex-Device-Name": "Wallpaparr",
            "X-Plex-Platform": "Python",
        }

    def _headers(self) -> dict[str, str]:
        # Only JSON calls ask for JSON; image fetches reuse auth_headers().
        return {**self.auth_headers(), "Accept": "application/json"}

    def _get_json(self, path: str, params: dict | None = None) -> dict | list:
        return self.client.get_json(f"{self.url}{path}", headers=self._headers(), params=params)

    @staticmethod
    def _container(payload) -> dict:
        if isinstance(payload, dict):
            container = payload.get("MediaContainer")
            if isinstance(container, dict):
                return container
        return {}

    def _remember_identity(self, container: dict) -> None:
        machine = container.get("machineIdentifier")
        if machine:
            self._machine_identifier = str(machine)
            self._machine_loaded = True

    def test(self) -> dict:
        if not self.is_configured():
            return {"ok": False, "error": "Plex URL and token are required"}
        try:
            container = self._container(self._get_json("/"))
            self._remember_identity(container)
            # The root endpoint can be public; listing sections proves the token works.
            sections = self._load_sections()
            if self.library_ids and not sections:
                raise ValueError("Plex library section(s) not found: " + ", ".join(self.library_ids))
            return {
                "ok": True,
                "server": container.get("friendlyName") or "Plex",
                "version": container.get("version") or "",
                "machine_identifier": self._machine_identifier,
            }
        except Exception:
            # Some reverse proxies do not expose "/" but do expose /identity.
            try:
                container = self._container(self._get_json("/identity"))
                self._remember_identity(container)
                sections = self._load_sections()
                if self.library_ids and not sections:
                    raise ValueError("Plex library section(s) not found: " + ", ".join(self.library_ids))
                return {
                    "ok": True,
                    "server": "Plex",
                    "version": container.get("version") or "",
                    "machine_identifier": self._machine_identifier,
                }
            except Exception as exc:
                return {"ok": False, "error": str(exc)}

    def _load_sections(self) -> list[dict]:
        payload = self._get_json("/library/sections")
        container = self._container(payload)
        sections: list[dict] = []
        for entry in _as_list(container.get("Directory")):
            if not isinstance(entry, dict):
                continue
            key = str(entry.get("key") or "").strip()
            kind = str(entry.get("type") or "").strip().lower()
            if key and kind in _SECTION_TYPES:
                sections.append(
                    {
                        "key": key,
                        "type": kind,
                        "title": str(entry.get("title") or "").strip(),
                    }
                )
        if self.library_ids:
            wanted = set(self.library_ids)
            sections = [section for section in sections if section["key"] in wanted]
        return sections

    def _section_items(self, section: dict, limit: int) -> list[tuple[int, MediaItem]]:
        params = {
            "type": _TYPE_IDS.get(section["type"], "1"),
            "sort": "addedAt:desc",
            "includeGuids": "1",
            "X-Plex-Container-Start": "0",
            "X-Plex-Container-Size": str(max(limit, 1)),
        }
        payload = self._get_json(f"/library/sections/{quote(section['key'], safe='')}/all", params=params)
        return self._parse_metadata(self._container(payload))

    def _recently_added(self, limit: int) -> list[tuple[int, MediaItem]]:
        params = {
            "includeGuids": "1",
            "X-Plex-Container-Start": "0",
            "X-Plex-Container-Size": str(max(limit * 4, limit, 1)),
        }
        payload = self._get_json("/library/recentlyAdded", params=params)
        return self._parse_metadata(self._container(payload))

    def _parse_metadata(self, container: dict) -> list[tuple[int, MediaItem]]:
        rows: list[tuple[int, MediaItem]] = []
        for raw in _as_list(container.get("Metadata")):
            if not isinstance(raw, dict):
                continue
            parsed = self._parse(raw)
            if parsed:
                rows.append((_int(raw.get("addedAt")), parsed))
        return rows

    def list_items(self, limit: int = 40) -> list[MediaItem]:
        if not self.is_configured():
            return []
        rows: list[tuple[int, MediaItem]] = []
        errors: list[Exception] = []
        sections = []
        try:
            sections = self._load_sections()
        except Exception as exc:
            errors.append(exc)
        for section in sections:
            try:
                rows.extend(self._section_items(section, limit))
            except Exception as exc:
                errors.append(exc)
        if not sections and not self.library_ids:
            # No section list (old server, restricted token, or a proxy that
            # blocks /library/sections): fall back to the global recently-added feed.
            try:
                rows.extend(self._recently_added(limit))
            except Exception as exc:
                errors.append(exc)
        if not rows and errors:
            raise errors[-1]
        if not rows and self.library_ids and not sections and not errors:
            raise ValueError(
                "Plex library section(s) not found: " + ", ".join(self.library_ids)
            )
        rows.sort(key=lambda row: row[0], reverse=True)
        seen: set[str] = set()
        out: list[MediaItem] = []
        for _added_at, item in rows:
            key = item.plex_id or item.tmdb_id or item.imdb_id or f"{item.title}|{item.year}"
            if key in seen:
                continue
            seen.add(key)
            out.append(item)
            if len(out) >= limit:
                break
        return out

    def _machine_identifier_of(self) -> str:
        if self._machine_loaded:
            return self._machine_identifier
        self._machine_loaded = True
        for path in ("/identity", "/"):
            try:
                container = self._container(self._get_json(path))
            except Exception:
                continue
            self._remember_identity(container)
            if self._machine_identifier:
                break
        return self._machine_identifier

    def _action_url(self, item_id: str) -> str:
        """Plex Android TV deep link: opens the item's pre-play page."""
        machine = self._machine_identifier_of()
        if not machine:
            # No server id: launching the app is still better than a dead link.
            return "plex://"
        metadata_key = quote(f"/library/metadata/{item_id}", safe="")
        return f"plex://play/?metadataKey={metadata_key}&server={quote(machine, safe='')}"

    def _asset_url(self, path: str) -> str | None:
        text = (path or "").strip()
        if not text:
            return None
        if text.lower().startswith(("http://", "https://")):
            return text
        if "://" in text:
            return None
        if not text.startswith("/"):
            text = f"/{text}"
        return f"{self.url}{text}"

    def _artwork_urls(self, raw: dict) -> tuple[str | None, str | None]:
        """(backdrop, poster). Plex ``art`` is wide; ``thumb`` is the poster."""
        art = self._asset_url(_image_path(raw, "art"))
        thumb = self._asset_url(_image_path(raw, "thumb"))
        return art or thumb, thumb

    def _parse(self, raw: dict) -> MediaItem | None:
        kind = str(raw.get("type") or "").strip().lower()
        if kind not in _PARSE_TYPES:
            return None
        item_id = str(raw.get("ratingKey") or "").strip()
        if not item_id:
            key = str(raw.get("key") or "").strip().rstrip("/")
            item_id = key.split("/")[-1] if key else ""
        if not item_id:
            return None
        tmdb_id, imdb_id = _ids_from_plex(raw)
        backdrop_url, poster_url = self._artwork_urls(raw)
        return MediaItem(
            title=_title_from_plex(raw),
            year=_year_from_plex(raw),
            overview=str(raw.get("summary") or ""),
            rating=_float(raw.get("audienceRating") if raw.get("audienceRating") is not None else raw.get("rating")),
            genres=_genres_from_plex(raw),
            official_rating=str(raw.get("contentRating") or ""),
            runtime=_runtime_from_ms(raw.get("duration")),
            media_type="tv" if kind == "show" else "movie",
            watch_state=_watch_state(raw),
            library_state="in_library",
            availability="available",
            source="plex",
            plex_id=item_id,
            tmdb_id=tmdb_id,
            imdb_id=imdb_id,
            action_url=self._action_url(item_id),
            backdrop_url=backdrop_url,
            poster_url=poster_url,
        )
