import base64
import fcntl
import hashlib
import hmac
import tempfile
import time
import html
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Keep this file outside the web root. Set PLEX_PLAYLIST_CONFIG or edit
# CONFIG_FILE for installations that cannot pass environment variables.
CONFIG_FILE = os.environ.get("PLEX_PLAYLIST_CONFIG", "/etc/plex-playlist-manager/config.json")
try:
    with open(CONFIG_FILE, encoding="utf-8") as config_file:
        SETTINGS = json.load(config_file)
except FileNotFoundError:
    SETTINGS = {}

PLEX_URL = SETTINGS.get("plex_url", "http://127.0.0.1:32400").rstrip("/")
PREFERENCES_FILE = SETTINGS.get("preferences_file", "")
TOKEN_FILE = SETTINGS.get("token_file", "/etc/plex-playlist-manager/token")
SERVER_NAME = SETTINGS.get("server_name", "Plex Media Server")
ENABLE_SYNC = SETTINGS.get("enable_sync", False) is True
CLIENT_ID = "plex-playlist-manager"
PRODUCT = "Plex Playlist Manager"
VERSION = "0.1.0"


# ---------------------------------------------------------------------------
# Plex helpers
# ---------------------------------------------------------------------------

def get_plex_token():
    if PREFERENCES_FILE:
        root = ET.parse(PREFERENCES_FILE).getroot()
        token = root.get("PlexOnlineToken", "").strip()
    else:
        with open(TOKEN_FILE, encoding="utf-8") as f:
            token = f.read().strip()
    if not token:
        raise PlexError("Plex token is missing. Check the server-side configuration.")
    return token


class PlexError(Exception):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request(url, token=None, accept=None, method="GET", write=False):
    parsed = urllib.parse.urlsplit(url)
    local = urllib.parse.urlsplit(PLEX_URL)
    home_switch = (parsed.scheme == "https" and parsed.netloc == "plex.tv"
                   and re.fullmatch(r"/api/home/users/[0-9]+/switch", parsed.path))
    local_write = ((parsed.scheme, parsed.netloc) == (local.scheme, local.netloc) and write and (
        (method == "POST" and parsed.path == "/playlists") or
        (method == "PUT" and re.fullmatch(r"/playlists/[0-9]+/items", parsed.path)) or
        (method == "DELETE" and re.fullmatch(r"/playlists/[0-9]+/items/[0-9]+", parsed.path))))
    if method != "GET" and not (method == "POST" and home_switch) and not local_write:
        raise PlexError("This Plex write is not permitted.")
    if (parsed.scheme, parsed.netloc) not in {
        (local.scheme, local.netloc), ("https", "plex.tv")
    }:
        raise PlexError("Unexpected Plex endpoint.")
    headers = {
        "X-Plex-Product": PRODUCT,
        "X-Plex-Version": VERSION,
        "X-Plex-Client-Identifier": CLIENT_ID,
        "X-Plex-Platform": "Synology",
    }

    if token:
        headers["X-Plex-Token"] = token

    if accept:
        headers["Accept"] = accept

    req = urllib.request.Request(url, headers=headers, method=method,
                                 data=b"" if method == "POST" else None)
    try:
        with urllib.request.build_opener(NoRedirect).open(req, timeout=15) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        raise PlexError("Plex returned HTTP %s." % exc.code) from None
    except Exception:
        raise PlexError("Could not reach Plex. Check connectivity and account access.") from None


def get_server_identity(token):
    data = request(
        PLEX_URL + "/identity",
        token=token,
        accept="application/xml",
    )

    root = ET.fromstring(data)

    return {
        "name": SERVER_NAME,
        "version": root.attrib.get("version", "Unknown"),
        "machineIdentifier": root.attrib.get(
            "machineIdentifier", ""
        ),
    }


def get_playlists(token):
    items = collection("/playlists?playlistType=audio", token)

    playlists = []

    for item in items:
        # We only want regular audio playlists.
        if item.attrib.get("playlistType") != "audio":
            continue

        is_smart = item.attrib.get("smart") == "1"

        playlists.append(
            {
                "ratingKey": item.attrib.get("ratingKey", ""),
                "title": item.attrib.get("title", "Untitled"),
                "tracks": int(item.attrib.get("leafCount", "0")),
                "duration": int(item.attrib.get("duration", "0")),
                "smart": is_smart,
            }
        )

    playlists.sort(
        key=lambda x: x["title"].lower()
    )

    return playlists


def get_home_users(token):
    data = request(
        "https://plex.tv/api/v2/home/users",
        token=token,
        accept="application/json",
    )

    payload = json.loads(data.decode("utf-8"))

    users = []

    for user in payload.get("users", []):
        if user.get("admin"):
            continue

        users.append(
            {
                "id": str(user.get("id", "")),
                "uuid": user.get("uuid", ""),
                "title": (
                    user.get("friendlyName")
                    or user.get("title")
                    or user.get("username")
                    or "Unknown"
                ),
                "username": user.get("username", ""),
                "type": "Plex Home",
                "protected": bool(user.get("protected")),
            }
        )

    return users

def get_shared_users(token, machine_identifier):
    """
    Return users who have access to the Music library on this
    specific Plex Media Server.

    Access tokens stay in server-side request memory only.
    """

    url = (
        "https://plex.tv/api/servers/"
        + urllib.parse.quote(machine_identifier, safe="")
        + "/shared_servers"
    )

    data = request(
        url,
        token=token,
        accept="application/xml",
    )

    root = ET.fromstring(data)

    users = []

    for shared in root.iter("SharedServer"):
        # Determine whether this user has Music access.
        music_sections = []

        for section in shared.iter("Section"):
            section_type = (
                section.attrib.get("type", "").lower()
            )

            section_title = (
                section.attrib.get("title", "").lower()
            )

            shared_value = (
                section.attrib.get("shared", "0").lower()
            )

            is_shared = shared_value in (
                "1",
                "true",
                "yes",
            )

            if not is_shared:
                continue

            if (
                section_type == "artist"
                or section_title == "music"
            ):
                music_sections.append(
                    section.attrib.get("title", "Music")
                )

        if not music_sections:
            continue

        username = (
            shared.attrib.get("username")
            or shared.attrib.get("email")
            or "Unknown"
        )

        users.append(
            {
                "id": str(
                    shared.attrib.get("userID")
                    or shared.attrib.get("id")
                    or ""
                ),
                "uuid": "",
                "title": username,
                "username": username,
                "type": "Shared User",
                "protected": False,
                "music": True,
                "access_token": shared.attrib.get("accessToken", ""),
            }
        )

    return users


def merge_users(home_users, shared_users):
    """
    Combine Home and shared users without displaying the same
    Plex account twice.

    Plex Home takes precedence when a user appears in both.
    """

    merged = []
    for incoming in home_users + shared_users:
        user = dict(incoming)
        match = next((old for old in merged if
                      (user.get("id") and old.get("id") == user["id"]) or
                      (user.get("username") and old.get("username", "").casefold()
                       == user["username"].casefold())), None)
        if match is not None:
            if user.get("access_token"):
                match["access_token"] = user["access_token"]
        else:
            merged.append(user)
    return sorted(merged, key=lambda u: (u["type"] != "Plex Home", u["title"].lower()))


def collection(path, token):
    """Read complete containers, preserving order and duplicate tracks."""
    items = []
    total = None
    for unused in range(1000):
        sep = "&" if "?" in path else "?"
        url = PLEX_URL + path + sep + urllib.parse.urlencode({
            "X-Plex-Container-Start": len(items), "X-Plex-Container-Size": 200})
        root = ET.fromstring(request(url, token, "application/xml"))
        if root.tag != "MediaContainer":
            raise PlexError("Unexpected Plex response.")
        batch = list(root)
        if int(root.get("offset", str(len(items)))) != len(items):
            raise PlexError("Plex returned an inconsistent page.")
        current_total = int(root.get("totalSize", str(len(batch))))
        if total is None:
            total = current_total
        if current_total != total:
            raise PlexError("Playlist changed during preview. Try again.")
        items.extend(batch)
        if len(items) == total:
            return items
        if not batch or len(items) > total:
            raise PlexError("Plex returned an incomplete collection.")
    raise PlexError("Plex collection is too large to preview.")


def playlist_keys(key, token):
    if not str(key).isdigit():
        raise PlexError("Invalid playlist ID.")
    items = collection("/playlists/%s/items" % key, token)
    if any(item.tag != "Track" or not item.get("ratingKey", "").isdigit()
           for item in items):
        raise PlexError("Playlist contains unsupported or unidentified items.")
    return [item.get("ratingKey") for item in items]


def destination_token(user, owner_token, machine):
    token = user.get("access_token")
    if not token:
        if user["type"] != "Plex Home" or not user["id"].isdigit():
            raise PlexError("No destination server access token is available.")
        if user.get("protected"):
            raise PlexError("PIN-protected Home user requires authentication; preview unavailable.")
        root = ET.fromstring(request(
            "https://plex.tv/api/home/users/%s/switch" % user["id"],
            owner_token, "application/xml", method="POST"))
        account_token = root.get("authenticationToken") or root.get("authToken")
        if not account_token:
            raise PlexError("Home switch did not return an account token.")
        resources = ET.fromstring(request(
            "https://plex.tv/api/resources?includeHttps=1", account_token, "application/xml"))
        token = next((d.get("accessToken") for d in resources.iter("Device")
                      if d.get("clientIdentifier") == machine and d.get("accessToken")), None)
    if not token or token == owner_token:
        raise PlexError("Could not establish a separate destination account.")
    return token


def preview(playlists, users, selected_playlists, selected_users, owner_token, machine):
    sources = {p["ratingKey"]: p for p in playlists}
    destinations = {u["id"]: u for u in users}
    selected_playlists = list(dict.fromkeys(selected_playlists))
    selected_users = list(dict.fromkeys(selected_users))
    if not selected_playlists or not selected_users or len(selected_playlists)*len(selected_users) > 200:
        raise PlexError("Select at least one playlist and user, up to 200 combinations.")
    if any(k not in sources for k in selected_playlists) or any(k not in destinations for k in selected_users):
        raise PlexError("Selection is no longer available. Refresh the page.")
    source_items = {}
    for key in selected_playlists:
        try:
            if sources[key]["smart"]:
                raise PlexError("Smart playlists cannot be synced.")
            source_items[key] = playlist_keys(key, owner_token)
        except Exception as exc:
            source_items[key] = safe_error(exc)
    rows = []
    for uid in selected_users:
        user = destinations[uid]
        try:
            token = destination_token(user, owner_token, machine)
            target = collection("/playlists?playlistType=audio", token)
            user_error = None
        except Exception as exc:
            user_error = safe_error(exc)
        for key in selected_playlists:
            source = sources[key]
            row = {"playlist": source["title"], "user": user["title"], "status": "ERROR", "detail": ""}
            try:
                keys = source_items[key]
                if isinstance(keys, str):
                    raise PlexError(keys)
                if user_error:
                    raise PlexError(user_error)
                matches = [p for p in target if p.tag == "Playlist" and p.get("title") == source["title"]]
                if len(matches) > 1:
                    raise PlexError("Multiple destination playlists have this title; match is ambiguous.")
                if matches and matches[0].get("smart") == "1":
                    raise PlexError("Destination with this title is a protected smart playlist.")
                other = []
                if not matches:
                    if not keys:
                        raise PlexError("Empty source playlist cannot be created using the supported API.")
                    row.update(status="CREATE", detail="No matching title; %s source tracks." % len(keys))
                else:
                    other = playlist_keys(matches[0].get("ratingKey", ""), token)
                    row.update(status="UP TO DATE" if keys == other else "UPDATE",
                               detail="%s source / %s destination tracks; compared in order." % (len(keys), len(other)))
                row["source_id"] = key
                row["user_id"] = uid
                row["fingerprint"] = fingerprint(source["title"], keys,
                    matches[0].get("ratingKey", "") if matches else None, other)
            except Exception as exc:
                row["detail"] = safe_error(exc)
            rows.append(row)
    return rows


def fingerprint(title, keys, target_id, other):
    payload = json.dumps([title, keys, target_id, other], separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def sign_ticket(row, owner_token, machine):
    payload = {k: row[k] for k in ("source_id", "user_id", "fingerprint")}
    payload.update(expires=int(time.time()) + 900, machine=machine)
    raw = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode()
    digest = hmac.new(owner_token.encode(), ("playlist-sync-v2:" + raw).encode(), hashlib.sha256).hexdigest()
    return raw + "." + digest


def read_ticket(ticket, owner_token, machine):
    try:
        raw, digest = ticket.split(".")
        expected = hmac.new(owner_token.encode(), ("playlist-sync-v2:" + raw).encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(digest, expected):
            raise ValueError()
        payload = json.loads(base64.urlsafe_b64decode(raw))
        if payload["machine"] != machine or payload["expires"] < time.time():
            raise ValueError()
        return payload
    except Exception:
        raise PlexError("Preview expired or is invalid. Run Preview Sync again.") from None


def read_target_items(key, token):
    items = collection("/playlists/%s/items" % key, token)
    if any(i.tag != "Track" or not i.get("ratingKey", "").isdigit()
           or not i.get("playlistItemID", "").isdigit() for i in items):
        raise PlexError("Destination entries do not have valid track and playlist item IDs.")
    if len({i.get("playlistItemID") for i in items}) != len(items):
        raise PlexError("Destination playlist item IDs are ambiguous.")
    return items


def keys_of(items):
    return [i.get("ratingKey") for i in items]


def add_tracks(target_id, keys, token, machine, title=None):
    """Bound request URLs; never retry an uncertain write automatically."""
    for offset in range(0, len(keys), 100):
        batch = keys[offset:offset + 100]
        uri = "server://%s/com.plexapp.plugins.library/library/metadata/%s" % (machine, ",".join(batch))
        params = {"uri": uri}
        creating = target_id is None
        if creating:
            params.update(type="audio", title=title, smart=0)
        path = "/playlists" if creating else "/playlists/%s/items" % target_id
        data = request(PLEX_URL + path + "?" + urllib.parse.urlencode(params), token,
                       "application/xml", method="POST" if creating else "PUT", write=True)
        if creating:
            root = ET.fromstring(data)
            created = root.find("Playlist")
            if created is None or not created.get("ratingKey", "").isdigit():
                raise PlexError("Creation response could not be verified. Preview again before retrying.")
            target_id = created.get("ratingKey")
    return target_id


def sync_pair(ticket, owner_token, machine, users):
    if not ENABLE_SYNC:
        raise PlexError("Sync is disabled in server configuration. Preview remains available.")
    plan = read_ticket(ticket, owner_token, machine)
    # flock spans Web Station workers and threads and releases after a crash.
    lock_path = os.path.join(tempfile.gettempdir(), CLIENT_ID + "-sync.lock")
    with open(lock_path, "a") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise PlexError("Another sync is running. Wait for it to finish and preview again.")
        return sync_pair_locked(plan, owner_token, machine, users)


def sync_pair_locked(plan, owner_token, machine, users):
    source = next((p for p in get_playlists(owner_token) if p["ratingKey"] == plan["source_id"]), None)
    user = next((u for u in users if u["id"] == plan["user_id"]), None)
    if source is None or user is None or source["smart"]:
        raise PlexError("Playlist or user is unavailable. Preview again.")
    row = dict(playlist=source["title"], user=user["title"], status="ERROR", detail="")
    attempted = False
    try:
        keys = playlist_keys(source["ratingKey"], owner_token)
        token = destination_token(user, owner_token, machine)
        targets = collection("/playlists?playlistType=audio", token)
        matches = [p for p in targets if p.tag == "Playlist" and p.get("title") == source["title"]]
        if len(matches) > 1 or (matches and matches[0].get("smart") == "1"):
            raise PlexError("Destination is ambiguous or smart. Preview again.")
        target_id = matches[0].get("ratingKey", "") if matches else None
        if target_id == source["ratingKey"]:
            raise PlexError("Destination resolved to the source playlist. Check recipient authentication; no changes made.")
        if target_id is not None and not target_id.isdigit():
            raise PlexError("Invalid destination ID.")
        old_items = read_target_items(target_id, token) if target_id else []
        old_keys = keys_of(old_items)
        if target_id and old_keys == keys:
            row.update(status="UP TO DATE", detail="Ordered tracks already match. No changes made.")
            return row
        if fingerprint(source["title"], keys, target_id, old_keys) != plan["fingerprint"]:
            raise PlexError("Playlist changed after preview. No changes made; preview again.")
        if not target_id and not keys:
            raise PlexError("Empty playlists cannot be created with this API.")
        # Confirm each source track is visible as this recipient before any write.
        for offset in range(0, len(keys), 100):
            batch = list(dict.fromkeys(keys[offset:offset + 100]))
            visible = collection("/library/metadata/" + ",".join(batch), token)
            if {i.get("ratingKey") for i in visible if i.tag == "Track"} != set(batch):
                raise PlexError("Destination user cannot access every source track. No changes made.")
        creating = target_id is None
        if creating:
            attempted = True
            target_id = add_tracks(None, keys, token, machine, source["title"])
        else:
            # Append and verify before removing originals. No automatic rollback
            # can be safe if another client is editing the playlist concurrently.
            if keys:
                attempted = True
                add_tracks(target_id, keys, token, machine)
            combined = read_target_items(target_id, token)
            if keys_of(combined) != old_keys + keys or [i.get("playlistItemID") for i in combined[:len(old_items)]] != [i.get("playlistItemID") for i in old_items]:
                raise PlexError("Could not verify appended tracks. Original entries were not removed.")
            for old in old_items:
                attempted = True
                request(PLEX_URL + "/playlists/%s/items/%s" % (target_id, old.get("playlistItemID")),
                        token, method="DELETE", write=True)
        if playlist_keys(target_id, token) != keys:
            raise PlexError("Final track order did not match the source.")
        row.update(status="CREATED" if creating else "UPDATED",
                   detail="Verified %s tracks in source order." % len(keys))
    except Exception as exc:
        row["detail"] = safe_error(exc)
        if attempted:
            row["status"] = "CHECK REQUIRED"
            row["detail"] += " Changes may be partial. Inspect the destination and run Preview Sync before retrying."
    return row


def safe_error(exc):
    return str(exc) if isinstance(exc, PlexError) else "Unable to read Plex data. Check access and retry."


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def format_duration(milliseconds):
    if not milliseconds:
        return ""

    minutes = milliseconds // 60000

    hours = minutes // 60
    minutes = minutes % 60

    if hours:
        return f"{hours}h {minutes}m"

    return f"{minutes}m"


def page(playlists, users, server):
    playlist_cards = []

    for playlist in playlists:
        title = html.escape(playlist["title"])
        rating_key = html.escape(playlist["ratingKey"])
        duration = format_duration(playlist["duration"])

        meta = f'{playlist["tracks"]:,} tracks'

        if duration:
            meta += f" • {duration}"

        if playlist["smart"]:
            meta += " • Smart Playlist"

            playlist_cards.append(
                f"""
                <label class="card playlist-card smart-card">
                    <input
                        type="checkbox"
                        disabled
                    >
                    <span class="checkmark"></span>

                    <span class="card-content">
                        <span class="card-title">
                            {title}
                            <span class="smart-badge">(SMART)</span>
                        </span>

                        <span class="card-meta">
                            {meta}
                        </span>
                    </span>
                </label>
                """
            )

        else:
            playlist_cards.append(
                f"""
                <label class="card playlist-card">
                    <input
                        type="checkbox"
                        name="playlist"
                        value="{rating_key}"
                    >
                    <span class="checkmark"></span>

                    <span class="card-content">
                        <span class="card-title">{title}</span>
                        <span class="card-meta">{meta}</span>
                    </span>
                </label>
                """
            )

    user_cards = []

    for user in users:
        title = html.escape(user["title"])
        username = html.escape(user["username"])
        user_id = html.escape(user["id"])

        detail = user["type"]

        if username and username != user["title"]:
            detail += f" • {username}"

        user_cards.append(
            f"""
            <label class="card user-card">
                <input
                    type="checkbox"
                    name="user"
                    value="{user_id}"
                >
                <span class="checkmark"></span>
                <span class="avatar">
                    {title[:1].upper()}
                </span>
                <span class="card-content">
                    <span class="card-title">{title}</span>
                    <span class="card-meta">{html.escape(detail)}</span>
                </span>
            </label>
            """
        )

    playlist_html = "\n".join(playlist_cards)

    if not playlist_html:
        playlist_html = """
            <div class="empty">
                No regular audio playlists found.
            </div>
        """

    user_html = "\n".join(user_cards)

    if not user_html:
        user_html = """
            <div class="empty">
                No eligible Plex Home users found.
            </div>
        """

    server_name = html.escape(server["name"])
    server_version = html.escape(server["version"])

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta
    name="viewport"
    content="width=device-width, initial-scale=1"
>
<title>Plex Playlist Manager</title>

<style>
    * {{
        box-sizing: border-box;
    }}

    body {{
        margin: 0;
        background: #f4f6f8;
        color: #1f2937;
        font-family:
            -apple-system,
            BlinkMacSystemFont,
            "Segoe UI",
            Roboto,
            Helvetica,
            Arial,
            sans-serif;
    }}

    header {{
        background: #151515;
        color: white;
        padding: 24px 32px;
    }}

    .header-inner {{
        max-width: 1200px;
        margin: auto;
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 20px;
    }}

    h1 {{
        margin: 0;
        font-size: 25px;
        font-weight: 650;
    }}

    .plex-accent {{
        color: #e5a00d;
    }}

    .server {{
        font-size: 13px;
        color: #b6bbc3;
        text-align: right;
    }}

    .connected {{
        color: #65c466;
        font-weight: 600;
    }}

    main {{
        max-width: 1200px;
        margin: 30px auto;
        padding: 0 24px 50px;
    }}

    .intro {{
        margin-bottom: 25px;
    }}

    .intro h2 {{
        margin: 0 0 6px;
        font-size: 22px;
    }}

    .intro p {{
        margin: 0;
        color: #6b7280;
    }}

    .layout {{
        display: grid;
        grid-template-columns: 1.35fr 1fr;
        gap: 24px;
    }}

    .panel {{
        background: white;
        border: 1px solid #e2e5e9;
        border-radius: 12px;
        overflow: hidden;
        box-shadow: 0 2px 7px rgba(0,0,0,.04);
    }}

    .panel-header {{
        padding: 20px 22px 16px;
        border-bottom: 1px solid #e8eaed;
    }}

    .panel-header-row {{
        display: flex;
        justify-content: space-between;
        align-items: center;
    }}

    .panel h3 {{
        margin: 0;
        font-size: 17px;
    }}

    .count {{
        color: #7b818a;
        font-size: 13px;
    }}

    .search {{
        width: 100%;
        margin-top: 15px;
        padding: 10px 12px;
        border: 1px solid #d6dae0;
        border-radius: 7px;
        font-size: 14px;
        outline: none;
    }}

    .search:focus {{
        border-color: #e5a00d;
    }}

    .cards {{
        padding: 10px;
        max-height: 560px;
        overflow-y: auto;
    }}

    .card {{
        display: flex;
        align-items: center;
        gap: 13px;
        padding: 13px;
        margin: 2px 0;
        border-radius: 8px;
        cursor: pointer;
        transition: background .12s ease;
    }}

    .card:hover {{
        background: #f6f7f8;
    }}

    .card input {{
        width: 18px;
        height: 18px;
        accent-color: #e5a00d;
        flex: 0 0 auto;
    }}

    .card-content {{
        min-width: 0;
        display: flex;
        flex-direction: column;
        gap: 3px;
    }}

    .card-title {{
        font-size: 14px;
        font-weight: 600;
        line-height: 1.3;
    }}

    .card-meta {{
        color: #818791;
        font-size: 12px;
    }}

    .avatar {{
        width: 35px;
        height: 35px;
        flex: 0 0 35px;
        border-radius: 50%;
        background: #30343a;
        color: #e5a00d;
        display: flex;
        align-items: center;
        justify-content: center;
        font-weight: 700;
    }}

    .actions {{
        margin-top: 24px;
        background: white;
        border: 1px solid #e2e5e9;
        border-radius: 12px;
        padding: 18px 22px;
        display: flex;
        align-items: center;
        justify-content: space-between;
        box-shadow: 0 2px 7px rgba(0,0,0,.04);
    }}

    .selection {{
        color: #6b7280;
        font-size: 13px;
    }}

    button {{
        border: 0;
        border-radius: 7px;
        padding: 11px 20px;
        font-size: 14px;
        font-weight: 650;
        cursor: pointer;
    }}

    .preview {{
        background: #e5a00d;
        color: #151515;
    }}

    .preview:disabled {{
        opacity: .4;
        cursor: not-allowed;
    }}

    .notice {{
        max-width: 1200px;
        margin: 0 auto 22px;
        padding: 12px 16px;
        background: #fff8e6;
        border: 1px solid #f0d48c;
        border-radius: 8px;
        color: #735a13;
        font-size: 13px;
    }}

    .empty {{
        padding: 30px;
        text-align: center;
        color: #818791;
    }}

    @media (max-width: 800px) {{
        .layout {{
            grid-template-columns: 1fr;
        }}

        .header-inner {{
            align-items: flex-start;
        }}

        .actions {{
            gap: 15px;
            flex-direction: column;
            align-items: stretch;
        }}

        button {{
            width: 100%;
        }}
    }}

    .smart-card {{
        opacity: .7;
        cursor: default;
    }}

    .smart-card:hover {{
        background: transparent;
    }}

    .smart-badge {{
        display: inline-block;
        margin-left: 7px;
        padding: 2px 6px;
        border-radius: 4px;
        background: #eee4ca;
        color: #8b6500;
        font-size: 9px;
        font-weight: 700;
        vertical-align: 2px;
        letter-spacing: .4px;
    }}
</style>
</head>

<body>

<header>
    <div class="header-inner">
        <h1>
            <span class="plex-accent">Plex</span>
            Playlist Manager
        </h1>

        <div class="server">
            <span class="connected">● Connected</span><br>
            {server_name} • Plex {server_version}
        </div>
    </div>
</header>

<main>

    <div class="notice">
        {"Preview first, then Sync Selected to apply changes. Updates replace destination tracks and order to match the source. Smart playlists are protected." if ENABLE_SYNC else "Preview-only mode. Sync is disabled in server configuration. No playlists will be changed."}
    </div>

    <div class="intro">
        <h2>Share your playlists</h2>
        <p>
            Select playlists and the Plex users you want
            to receive them.
        </p>
    </div>

    <div class="layout">

        <section class="panel">
            <div class="panel-header">
                <div class="panel-header-row">
                    <h3>Your Playlists</h3>
                    <span class="count">
                        {len(playlists)} playlists
                    </span>
                </div>

                <input
                    id="playlistSearch"
                    class="search"
                    type="search"
                    placeholder="Search playlists..."
                >
            </div>

            <div id="playlistCards" class="cards">
                {playlist_html}
            </div>
        </section>

        <section class="panel">
            <div class="panel-header">
                <div class="panel-header-row">
                    <h3>Plex Users</h3>
                    <span class="count">
                        {len(users)} {"user" if len(users) == 1 else "users"}
                    </span>
                </div>
            </div>

            <div class="cards">
                {user_html}
            </div>
        </section>

    </div>

    <div class="actions">
        <div id="selectionText" class="selection">
            Select at least one playlist and one user.
        </div>

        <button
            id="previewButton"
            class="preview"
            disabled
        >
            Preview Sync
        </button>
    </div>
    <button id="syncButton" class="preview" style="margin-top:18px" hidden disabled>Sync Selected</button>
    <section id="previewResults" class="panel" style="margin-top:24px;padding:22px" aria-live="polite" hidden></section>

</main>

<script>
const playlistChecks =
    document.querySelectorAll('input[name="playlist"]');

const userChecks =
    document.querySelectorAll('input[name="user"]');

const button =
    document.getElementById("previewButton");

const selectionText =
    document.getElementById("selectionText");

function updateSelection() {{
    const playlists =
        document.querySelectorAll(
            'input[name="playlist"]:checked'
        ).length;

    const users =
        document.querySelectorAll(
            'input[name="user"]:checked'
        ).length;

    button.disabled = !(playlists && users);

    if (!playlists || !users) {{
        selectionText.textContent =
            "Select at least one playlist and one user.";
        return;
    }}

    selectionText.textContent =
        playlists + " playlist" +
        (playlists === 1 ? "" : "s") +
        " selected • " +
        users + " user" +
        (users === 1 ? "" : "s") +
        " selected";
}}

playlistChecks.forEach(
    el => el.addEventListener("change", updateSelection)
);

userChecks.forEach(
    el => el.addEventListener("change", updateSelection)
);

document
    .getElementById("playlistSearch")
    .addEventListener("input", function() {{
        const search = this.value.toLowerCase();

        document
            .querySelectorAll(".playlist-card")
            .forEach(card => {{
                card.style.display =
                    card.textContent
                        .toLowerCase()
                        .includes(search)
                    ? "flex"
                    : "none";
            }});
    }});

let busy = false;
let reviewedRows = [];
const syncButton = document.getElementById("syncButton");
const results = document.getElementById("previewResults");
const checks = [...playlistChecks, ...userChecks];
function setBusy(value) {{
    busy = value;
    checks.forEach(el => el.disabled = value);
    button.disabled = value;
    syncButton.disabled = value;
    if (!value) updateSelection();
}}
function showRow(row) {{
    const line = document.createElement("p");
    const status = document.createElement("strong");
    status.textContent = row.status + " — ";
    line.append(status, document.createTextNode(row.playlist + " → " + row.user + ": " + row.detail));
    results.appendChild(line);
}}
async function sendAction(payload) {{
    const response = await fetch(window.location.pathname, {{
        method: "POST",
        headers: {{"Content-Type": "application/json", "X-Preview-Request": "1"}},
        body: JSON.stringify(payload)
    }});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Request failed.");
    return data;
}}
button.addEventListener("click", async function() {{
    if (busy) return;
    setBusy(true);
    reviewedRows = [];
    syncButton.hidden = true;
    results.hidden = false;
    results.textContent = "Comparing playlists… No changes are being made.";
    try {{
        const data = await sendAction({{
            action: "preview",
            playlists: [...playlistChecks].filter(el => el.checked).map(el => el.value),
            users: [...userChecks].filter(el => el.checked).map(el => el.value)
        }});
        results.textContent = "Preview only — no changes made. Updates replace destination tracks and order. Matches use exact titles.";
        data.rows.forEach(showRow);
        reviewedRows = data.rows.filter(row => row.ticket);
        syncButton.hidden = !reviewedRows.length;
        syncButton.textContent = "Sync Selected (" + reviewedRows.length + ")";
    }} catch (error) {{
        results.textContent = "Preview could not finish. " + error.message;
    }} finally {{ setBusy(false); }}
}});
syncButton.addEventListener("click", async function() {{
    if (busy || !reviewedRows.length) return;
    setBusy(true);
    const pending = reviewedRows;
    reviewedRows = [];
    syncButton.hidden = true;
    results.textContent = "Syncing reviewed selections. Keep this page open.";
    try {{
        for (let i = 0; i < pending.length; i++) {{
            syncButton.textContent = "Syncing " + (i + 1) + " of " + pending.length;
            const data = await sendAction({{action: "sync", ticket: pending[i].ticket}});
            showRow(data.row);
            if (data.row.status === "CHECK REQUIRED") {{
                results.appendChild(document.createTextNode("Remaining selections were stopped. Inspect the result and preview again."));
                return;
            }}
        }}
        results.appendChild(document.createTextNode("Sync finished. Review each result above."));
    }} catch (error) {{
        results.appendChild(document.createTextNode("Sync stopped: " + error.message + " A request may have completed. Preview again before retrying."));
    }} finally {{ setBusy(false); }}
}});
checks.forEach(el => el.addEventListener("change", () => {{
    reviewedRows = [];
    syncButton.hidden = true;
    results.hidden = true;
}}));
</script>

</body>
</html>
"""


# ---------------------------------------------------------------------------
# Error page
# ---------------------------------------------------------------------------

def error_page(message):
    safe = html.escape(str(message))

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Plex Playlist Manager - Error</title>
<style>
body {{
    font-family: sans-serif;
    background: #f4f6f8;
    padding: 40px;
}}
.box {{
    max-width: 800px;
    margin: auto;
    background: white;
    padding: 25px;
    border-radius: 10px;
    border: 1px solid #ddd;
}}
h1 {{
    color: #b42318;
}}
pre {{
    white-space: pre-wrap;
}}
</style>
</head>
<body>
<div class="box">
<h1>Plex Playlist Manager</h1>
<p>The application encountered an error:</p>
<pre>{safe}</pre>
</div>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# WSGI entry point
# ---------------------------------------------------------------------------

def application(environ, start_response):
    is_preview = environ.get("REQUEST_METHOD", "GET") == "POST"
    content_type = "application/json" if is_preview else "text/html; charset=utf-8"
    try:
        if environ.get("REQUEST_METHOD", "GET") not in ("GET", "POST"):
            raise PlexError("Only GET and preview POST are supported.")
        selection = None
        if is_preview:
            if environ.get("HTTP_X_PREVIEW_REQUEST") != "1" or environ.get("CONTENT_TYPE", "").split(";")[0] != "application/json":
                raise PlexError("Invalid preview request.")
            size = int(environ.get("CONTENT_LENGTH") or "0")
            if not 0 < size <= 16384:
                raise PlexError("Invalid preview request size.")
            selection = json.loads(environ["wsgi.input"].read(size).decode("utf-8"))
            if not isinstance(selection, dict) or selection.get("action") not in ("preview", "sync"):
                raise PlexError("Invalid action.")
            if selection["action"] == "sync" and not isinstance(selection.get("ticket"), str):
                raise PlexError("Run Preview Sync first.")
            for field in (("playlists", "users") if selection["action"] == "preview" else ()):
                if not isinstance(selection.get(field), list) or any(not isinstance(x, str) or not x.isdigit() for x in selection[field]):
                    raise PlexError("Invalid selection.")
        token = get_plex_token()

        server = get_server_identity(token)
        playlists = get_playlists(token)

        home_users = get_home_users(token)

        shared_users = get_shared_users(
            token,
            server["machineIdentifier"],
        )

        users = merge_users(
            home_users,
            shared_users,
        )

        if is_preview:
            machine = server["machineIdentifier"]
            if selection["action"] == "preview":
                rows = preview(playlists, users, selection["playlists"], selection["users"], token, machine)
                for row in rows:
                    if ENABLE_SYNC and row["status"] in ("CREATE", "UPDATE"):
                        row["ticket"] = sign_ticket(row, token, machine)
                body = json.dumps({"rows": rows}).encode("utf-8")
            else:
                row = sync_pair(selection["ticket"], token, machine, users)
                body = json.dumps({"row": row}).encode("utf-8")
        else:
            body = page(playlists, users, server).encode("utf-8")
        status = "200 OK"
    except Exception as exc:
        message = safe_error(exc)
        body = (json.dumps({"error": message}) if is_preview else error_page(message)).encode("utf-8")
        status = "400 Bad Request" if isinstance(exc, PlexError) else "500 Internal Server Error"

    start_response(
        status,
        [
            ("Content-Type", content_type),
            ("X-Content-Type-Options", "nosniff"),
            ("Content-Length", str(len(body))),
            ("Cache-Control", "no-store"),
        ],
    )

    return [body]
