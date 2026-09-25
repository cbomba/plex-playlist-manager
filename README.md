# Plex Playlist Manager

Preview and copy regular audio playlists to Plex Home and shared users on the same Plex Media Server. Built for Synology Web Station with Python's standard library: no Flask, PlexAPI, pip packages, or extra application server.

**Early release.** Preview has been exercised on a Synology installation. Sync has automated tests using a simulated Plex server; broad real-server compatibility is not yet established. Start with one disposable playlist and one recipient. Python 3.9-compatible syntax is retained for Web Station, but use a supported Python runtime where your NAS allows it.

## Features

- Lists audio playlists; smart playlists remain visible and disabled.
- Discovers Plex Home and Music-library shared users, merging duplicates.
- Compares ordered ratingKeys, including repeated tracks, across paginated responses.
- Preview reports CREATE, UPDATE, UP TO DATE, or ERROR using exact title matches.
- Optional Sync Selected creates or updates recipient playlists and verifies the result.
- Plex tokens remain on the server, never in page content or URLs sent to the browser.
- No background syncing, analytics, external scripts, or cloud hosting dependency.

## Before installing

This is an administrator tool, **not a public website**. It has no built-in login. Anyone who can reach it can inspect playlists and users, and—when sync is enabled—request playlist changes. Restrict it to trusted administrators through your network and/or an authenticated reverse proxy. Never expose it directly to the internet. Sharing this source repository does not publish your Plex server or credentials.

Updates replace the recipient playlist's tracks and order with the source. Plex operations are not transactional. If a request fails, the result may be partial; inspect the playlist and preview again. Do not edit a destination simultaneously in another Plex client.

## Requirements

- Synology Web Station with a Python WSGI profile and Apache/mod_wsgi, or a compatible Unix WSGI host.
- Python 3.9 or newer with standard-library `fcntl` support (Linux/macOS; native Windows is unsupported).
- Access from the WSGI worker to your Plex server and HTTPS access to `plex.tv`.
- The server owner's Plex token, stored in a protected file readable by the WSGI service.
- Recipients already invited to the server and allowed to access the source Music library.

## Synology setup

1. Download this repository. Put `plex.wsgi` in the application directory you will configure in Web Station. Keep configuration and credentials **outside the web root**.
2. Copy `config.example.json` to `/etc/plex-playlist-manager/config.json`, or choose another protected path and change `CONFIG_FILE` near the top of `plex.wsgi`. Hosts that support process environment variables can instead set `PLEX_PLAYLIST_CONFIG` to that absolute path.
3. Put only the owner token in the configured `token_file`. Give the Web Station service account read access, but do not grant access to all users. Do not commit this file. See Plex's [token documentation](https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/) for obtaining a token.
4. Adjust `plex_url` and `server_name`. The default URL assumes Plex and Web Station run on the same NAS. Keep `enable_sync` set to `false` initially.
5. In Web Station, select the Python WSGI service/profile and map an alias such as `/plex-playlists/` to this application's `plex.wsgi`. The WSGI callable is named `application`. Use your existing working Python profile if you have one; Synology's exact labels vary by package version. No pip packages are required. Confirm that Web Station **executes** the WSGI file rather than serving its source as a download.
6. Restrict access to trusted administrators and open the alias. Choose a regular playlist and recipient, then click **Preview Sync**.
7. To permit writes, set `"enable_sync": true` in the protected configuration and reload the WSGI application through Web Station. Run a fresh preview, review CREATE/UPDATE results, and click **Sync Selected**.

Configuration is read when the WSGI module loads; restart/reload the application after changes. A missing configuration file uses defaults with sync disabled. Invalid JSON or inaccessible configuration can prevent startup.

### Configuration reference

| Setting | Purpose |
| --- | --- |
| `plex_url` | Base URL of your Plex Media Server, without a path prefix. Default `http://127.0.0.1:32400`. |
| `server_name` | Display name in the header. |
| `token_file` | Protected plain-text owner token file. |
| `preferences_file` | Optional absolute path to Plex's `Preferences.xml`; when nonempty, its `PlexOnlineToken` is used instead of `token_file`. Do not broaden access to Plex's entire private configuration just for this app. |
| `enable_sync` | Boolean; defaults to `false`. Set `true` only after preview and access controls are checked. |

An optional Synology Preferences path may resemble `/volume1/Plex/Library/Application Support/Plex Media Server/Preferences.xml`, but it varies by installation. A dedicated token file avoids requiring the WSGI worker to read that broader configuration.

## How preview and sync work

Source playlists are read using the owner token. Recipient access is obtained from this server's shared-server tokens or a Home-user authentication switch followed by a server-resource lookup. PIN-protected Home users without an available shared-server token are reported as unsupported.

Preview matches exact titles. Multiple matching destination playlists, smart destinations, and unsupported items produce errors. Missing playlists produce CREATE; different ordered tracks produce UPDATE. Empty sources cannot create a new playlist with the supported API, but can empty an existing playlist when syncing an update.

A preview authorizes only its reviewed source/user pair through a signed ticket that expires after 15 minutes. Sync rechecks the current contents and recipient visibility. Changed previews are refused, and already identical playlists are left alone. A process lock serializes app sync operations on the host.

Updates preserve the destination playlist ID: new entries are appended and verified before the old `playlistItemID` entries are removed. Final ordered track IDs are checked. A write failure reports **CHECK REQUIRED** and stops the remaining batch; there are no automatic write retries or rollback. Run a new preview after inspecting the destination. The application cannot prevent edits by other Plex clients during a sync.

Only regular audio playlists on the same server are supported. Artwork, descriptions, sharing rules, and smart filters are not copied. Title matching is not a persistent source/destination relationship: renaming a source can cause a later CREATE.

## Tests

```sh
python3 -m unittest discover -s tests -v
```

Tests use simulated responses and do not require a token or contact Plex. They cover comparison, pagination, authentication flow, signed tickets, stale previews, creation, updates, duplicates, access failures, and partial writes. Python 3.9 grammar is checked by the test loader. CI runs the suite on multiple Python versions on Linux.

## Troubleshooting

- **Preview-only banner:** intentional until `enable_sync` is true and the WSGI worker reloads.
- **HTTP 401/403:** check the owner token, recipient invitation, and library access. Never post tokens or raw credential-bearing responses in an issue.
- **Home user requires authentication:** PIN entry is not implemented; use a supported recipient with server access.
- **Playlist changed after preview:** run Preview Sync again.
- **CHECK REQUIRED:** a write may have partly completed. Inspect the destination, then preview again. Do not blindly repeat a failed sync.
- **Generic startup/read error:** check the Web Station log, configuration path, token-file permissions, and connectivity. Redact private data before sharing logs.

## Contributing and license

Bug reports and pull requests are welcome. Include Python/Web Station/Plex versions, the failing operation, and sanitized reproduction steps. Use synthetic playlist/user names in screenshots and tests.

MIT licensed; see [LICENSE](LICENSE). This is an independent project, not affiliated with Plex or Synology. Direct playlist API behavior was cross-checked against the [PlexAPI playlist implementation](https://python-plexapi.readthedocs.io/en/latest/_modules/plexapi/playlist.html); PlexAPI is not a runtime dependency.
