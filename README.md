# brick-games

The catalog of every game ROM in the brick homelab, at
**https://games.brick.nozdormu.cloud** (LAN only): search, filter by system,
device and genre, box art, year/genre/players, and which of the household's
devices play each system (brick-k8s-config#108).

## How it works

- **The scan** (`python -m brick_games scan`, a nightly CronJob on brick2000)
  reads `/extra/games/roms` read-only. It looks only at file names, never
  contents. For each system in `brick_games/systems.yaml`, it keeps the files
  with the system's extensions and makes one entry per game: regions,
  revisions and formats of the same title fold together, discs are counted,
  and arcade clones go under their parent set (FinalBurn Neo's dat). BIOS
  sets aren't games and are left out. A dump named only by its disc
  (`Wii/Xenoblade Chronicles/SX4E01.wbfs`, Nintendont's `game.iso`) goes by
  its folder's name.
- **The library** (`/extra/brick-games` on brick2000's drive, `library.py`)
  keeps every game's originals: `<system>/media/<Title>/box.png`,
  `title.png`, `snap.png` and `manual.pdf`, with `<system>/media.json`
  recording each game's folder (a game keeps it for good; a later game with
  the same title gets `Title [<id>]`). It's outside `/extra/games` on purpose,
  so the Samba `games` share and brick9000's ROM sync never see it and
  nothing reaches the handhelds. The scan writes it; the page only reads it.
  `/extra/brick-games/<system>/` has room for more per system later (saves:
  brick-games#19).
- **Art** comes from libretro's thumbnail repos on GitHub
  (`libretro-thumbnails/<System_Name>`: box art, title screen, screenshot;
  `thumbnails.libretro.com` has served an empty index since 2026-11-20),
  matched by a normalized title, as `sources.norm` and `loose` describe. A
  repo's file list is one git-tree call (folder by folder when it's too big,
  as FBNeo's is), kept in `DATA/sources` and revalidated by ETag, so a
  quiet night costs none of GitHub's 60 calls an hour. Each picture is
  fetched once into the library as the original PNG; a scan downloads for at
  most 2 hours (`ART_BUDGET`) and stops for the night if GitHub rate limits
  it, so the first fill (about 15,000 pictures, 2-3 GB) takes a night or
  two. `GITHUB_TOKEN`, if set, lifts the limits; nothing needs it.
  A system with `fetch: false` in `systems.yaml` gets no new downloads and
  keeps the art already cached: arcade, since 2026-11-21 (2,121 games; its
  fill is brick-games#22).
- **Facts** (year, genre, players, developer, publisher) come from
  libretro-database's `metadat`. Coverage is good for cartridge systems and
  patchy for disc systems.
- **Ratings** come from IGDB (critics' average and IGDB users, all eras) and
  RAWG (the Metascore: Metacritic has no API of its own, and only covers
  about 1995 on), as `ratings.py` describes. Each system's platform ids are
  `igdb` and `rawg` in `systems.yaml`. The keys (`IGDB_CLIENT_ID` and
  `IGDB_CLIENT_SECRET`, a Twitch app's; `RAWG_KEY`) come from the
  `brick-games-ratings` Secret, sealed in brick-k8s-secrets. Without them
  there are no ratings and everything else works. RAWG is skipped for now
  (the user, 2026-11-18): the Secret has no `RAWG_KEY`. Lists are kept in
  `DATA/ratings` and fetched again weekly.
- **A ratings source never fails the scan** (the user, 2026-11-18): no
  connection, a bad key, or an odd answer is a warning in the log, the last
  list is kept, and the source isn't asked again that run. It's pushed as
  `games_ratings_fetches{source,result="error"}` and brick9000's board shows
  it in amber (brick-cicd-config, `checks.py`). Nothing alerts or pages on it.
  The page shows them in each game's details. "Best rated" and the rating
  filter (90, 80, 70 or 60 and up, rated, not rated) use a score only from
  10 reviews up, weighted toward the catalog's average as if it had 10 more
  average reviews (the user, 2026-11-18): a lone critic's 100 doesn't top
  the list. The card shows the score that counts, or a dimmed one under the
  floor; the details show the weighted "Ranked" score.
- **The page** (`python -m brick_games web`) serves `catalog.json`, the art,
  the manuals, `/health`, `/ready` and `/metrics` (Caching, below). It reads
  the library read-only and has no ROM access.
- **Plays on**: each system's `devices` in `systems.yaml`, as `good`, `ok` or
  `no`. They're measured on the Retroid Pocket 2+ (homelab-context
  `docs/retroid.md`) and taken from brick-retro's cores for brick9000. The
  RG476H entries are expected values until it arrives. Edit the YAML and the
  next scan picks it up.
- **Manuals** (brick-games#1) come from the Internet Archive's per-system
  manual sets (`manuals` in `systems.yaml`, one PDF per game, named like the
  ROMs; Neo Geo by short name), matched by exact name, as `manuals.py`
  describes. Each is fetched once into the game's library folder and opens
  from the game's details ("Manual (PDF)"); a game without one keeps the
  Archive search link. (Until 0.7.0 they were kept in the data volume; a scan
  moves each one into the library, `games_manual_fetches{result="moved"}`.) A scan downloads for at most an hour, so the first fill (about 1,400
  manuals, 5 GB, as of 2026-11-18) takes a night or two. No sets for DS (its
  names run together), Game Gear, PC Engine CD or arcade. Failures never fail
  the scan; they're pushed as `games_manual_fetches{result}`.
- **Guides** (the user, 2026-11-19): for PS1, PS2, NES, SNES, GBA, Game Boy,
  Dreamcast, GameCube, Genesis, Sega CD, 32X, Saturn, Game Gear and DOS,
  each game's details link its GameFAQs page (the Guides tab lists them) and
  its StrategyWiki walkthrough when there is one. GameFAQs refuses scripted
  requests (Cloudflare), so its guides aren't read: Wikidata gives each
  game's GameFAQs id (P4769) and StrategyWiki page (P9075), by the system's
  platform items (`wikidata` in `systems.yaml`), matched by name like
  ratings (`guides.py`). A game Wikidata doesn't know gets a GameFAQs search
  link. Kept in `DATA/guides` and asked again weekly; failures never fail the
  scan (`games_guide_fetches{result}`). Measured 2026-11-19: 1,831 of 2,805
  games get their GameFAQs page (GameCube 80 of 85, PlayStation 62 of 70,
  NES 577 of 701); Wikidata knows few DOS, 32X and Sega CD games, so most of
  those get the search link.
- **Local strategy guides** (brick-games#21, the user, 2026-11-21): every
  game ranked 80 and up (the page's Ranked score) gets its StrategyWiki
  walkthrough kept on the LAN (`strategy.py`): the main page and every
  subpage its Table of Contents links, as text, in the game's library folder
  (`guide.md`, and `guide.json` with the source, license, grade and an index
  of every heading) and copied to `DATA/strategy` for the page. The page is
  the Wikidata link, or the game's title if StrategyWiki files it under the
  system's platform (`strategywiki` in `systems.yaml`). The grade is
  StrategyWiki's completion stage: complete (stages 3-5, the game can be
  finished with it), partial (2), stub (0-1). StrategyWiki is CC BY-SA 4.0,
  so each guide starts with its source and license. GameFAQs guides are
  never downloaded (their authors' copyright, and GameFAQs forbids it); one
  saved by hand into a game's library folder as `guide-gamefaqs.txt` is
  added and indexed, whatever the game's rank. Guides are fetched for at most
  30 minutes a scan and refreshed monthly; a failure keeps the old one
  (`games_strategy_guides{result}`, `games_guide_grades{grade}`).
  The game's details have READ THE GUIDE: its contents, a search ("where are
  you?") over its sections, and a warning on a partial guide or a stub. For
  an AI session: `/api/guides?q=<title>`, `/api/guides/<id>`,
  `/api/guides/<id>/text` and `/api/guides/<id>/search?q=<words>` (the
  `game-guide` skill in homelab-context).
  **Filing GameFAQs guides you save** (`brick_games/guide_filer.py`, on the
  laptop, standard library only): `--wanted` lists the games ranked 80+
  without a complete guide, best first, with their GameFAQs links (each
  scan writes the list: `/api/guides/wanted`). Run it with no arguments and
  it watches `~/Downloads`: open a guide in the browser, save it as text, and
  it's matched to the game by its file name (the system's name settles a
  title two systems share; otherwise it asks), copied over ssh to the game's
  folder on brick2000 as `guide-gamefaqs.txt` (an older one kept, dated) and
  moved to `~/Downloads/filed/`. Nothing downloads from GameFAQs.
- **Save sync** (brick-games#19, the user, 2026-11-21): brick9000, the
  Retroid and the RG476H share their saves (`saves.py`). Battery saves
  (RetroArch `.srm`) and PlayStation memory cards (DuckStation's
  `<game>_1.mcd`, PCSX-ReARMed's `.srm`: both a raw 128 KiB card) sync;
  save states never do (they're tied to a core's version).
  - **The agent** (`saves_agent.py`, standard library only) runs on each
    device: brick9000's `brick-saves.timer` every 10 minutes
    (brick-cicd-config), a handheld from the laptop over adb when it's
    plugged in (`python3 brick_games/saves_agent.py --device rp2plus --adb
    <serial>`). Where each device keeps its saves is `saves.yaml`. It
    matches a save to a game by name, uploads a changed one with the
    version it started from, and writes the current version only over a
    save the device hasn't changed since its last sync.
  - **Kafka decides, the user picks:** every upload is an event on
    `saves.events` (one KRaft broker, `kafka.yaml`), keyed by game, so one
    referee decides each game's events in order. A first save becomes
    current; one that started from the current version moves it forward.
    When devices have different saves, the user picks which to keep in the
    game's details (the user: "present me with which version to keep"):
    until then the current version stays and no device is overwritten. The
    header shows how many games wait for a pick. No version is ever deleted.
  - **Stored** in the library: `/extra/brick-games/<system>/saves/<Title>/`
    (`state.json`, and `history/` with every version by its SHA-256);
    outside the Samba share and brick9000's ROM sync. The topic is kept for
    good, a second record.
  - **The page:** each game's details show its saves: each device's copy
    (in sync, behind, in conflict), the history with downloads, and the
    pick when there's a conflict. The API is `/api/saves` (saves_web.py),
    LAN only and without a login, like the page.
  - Metrics: `games_saves_events_total{type,outcome}`, `games_saves_conflicts`,
    `games_saves_games`, `games_saves_last_sync_timestamp_seconds{device}`.
- **The game details** close with CLOSE, Escape, or a click outside them.

A scan that can't reach a source still lists every game and the art and
manuals already kept: only new downloads wait. Without the drive (the library
missing, or not on the ROMs' filesystem: `library.on_drive`) the scan fails
and leaves the catalog as it was. The catalog is written whole and then
renamed into place.

## Caching

Three tiers between the drive and a browser. The drive (a USB disk, exFAT)
is slow to seek, can spin down and can be unplugged, so the page never waits
on it for art and only once per manual.

| Tier | Where | Holds | Filled by |
| --- | --- | --- | --- |
| Origin | the library, `/extra/brick-games` (drive) | every original: PNG art (2-3 GB), manuals (4.7 GB, up to ~9 GB) | the scan |
| Hot | the data volume (brick2000's NVMe) | every picture as a small AVIF (`DATA/media`, ~150 MB); the manuals people open (`DATA/manual-cache`, at most `MANUAL_CACHE_MB`, 2 GiB, least recently opened dropped first) | the scan (art, rebuilt when an original is newer); the first request (a manual: copied whole, then served) |
| Browser | each device | everything it has seen | versioned URLs, below |

- **Art** is served only from NVMe; the catalog lists a picture only when its
  AVIF is there.
- **A manual** is served from the manual cache, or copied in from the library
  first (`cache.py`). The copy keeps the original's mtime, so its ETag and
  `?v=` don't change. With the drive offline, cached manuals still open and
  the rest answer `503` with `Retry-After`.
- Left out on purpose: a shared HTTP cache in front (Varnish, Caddy's or
  Traefik's), since immutable URLs and the browser's cache already cover a
  household's traffic; and an in-memory cache, since the kernel's page cache
  over NVMe is one already (the pod has 256 MiB).

The catalog changes at most once a night and the art almost never, so
browsers keep both and ask only "has it changed?" (sizes as of 2026-11: a
1.6 MB catalog of about 5,000 games, about 13,800 pictures of about 10 kB).

| What | Cache-Control | Validator | Compressed |
| --- | --- | --- | --- |
| `/` | `no-cache` | strong ETag | gzip |
| `/api/catalog` | `no-cache` | strong ETag + Last-Modified | gzip (1.6 MB to ~0.3 MB) |
| `/media/...avif?v=<hash>`, `/media/.../manuals/<id>.pdf?v=<hash>` | `public, max-age=31536000, immutable` | ETag + Last-Modified | no (AVIF, PDF) |
| the same without `v` | `public, max-age=604800` | ETag + Last-Modified | no |

- `no-cache` means "revalidate first": the browser sends `If-None-Match`
  and gets a bodiless `304` until the file changes.
- The web app reads, hashes and gzips `catalog.json` once per version of the
  file (it checks the open file's inode, mtime and size), not per request.
  Each encoding has its own ETag (`"<hash>"`, `"<hash>-gz"`) and either one
  revalidates; responses say `Vary: Accept-Encoding`.
- The scan writes each picture's URL with `?v=` and the first 8 hex digits of
  its SHA-256, so a changed picture is a new URL and the old one can be
  cached for good. It leaves `catalog.json` alone when nothing but its
  `generated` time would change, so the ETag survives quiet nights.
- Nothing in front compresses or caches: the front door's Caddy and Traefik
  pass the headers through, so compression is done here, once.
- `/metrics`: `games_web_requests_total{route,status}` (304s against 200s),
  `games_web_response_bytes_total{route,encoding}`,
  `games_media_requests_total{kind,tier}` (kind art or manual; tier hot,
  origin, offline or missing: how often the drive is on the path),
  `games_manual_cache_bytes` against `games_manual_cache_limit_bytes`, and
  `games_manual_origin_copy_seconds`.

## Run it

```bash
poetry install
poetry run pytest
mkdir -p /tmp/games-library   # on the same filesystem as the ROMs (library.on_drive)
ROMS_ROOT=/path/to/roms DATA=/tmp/games LIBRARY=/tmp/games-library poetry run python -m brick_games scan
DATA=/tmp/games LIBRARY=/tmp/games-library poetry run python -m brick_games web   # http://localhost:8080
```

## Deploy

`k8s_config/` is rendered by Argo CD (brick-k8s-config `argocd/apps/brick-games.yaml`).
A merge to main is a deploy. The image is `whitepatrick/brick-games`, built by
Jenkins' `build-image` job (brick-cicd-config `jenkins/lib/build.groovy`). A
release bumps `version` in `pyproject.toml` and `newTag` in
`k8s_config/kustomization.yaml`, on a `release/<version>` branch that
`build-branches` builds.

- **Scan:** `brick-games-scan` CronJob, 03:30 every night, at most 4 hours, on
  brick2000 (`chuck.io/storage-node`), with the ROMs as a read-only hostPath
  and the library (`/extra/brick-games`) read-write. It pushes
  `games_catalog_*` (`with_rating` among them), `games_art_fetches`
  (`result`: ok, missing, error, deferred, built), `games_ratings_fetches`
  (`source`: igdb, rawg; `result`: fetched, cached, error, no_key; counts of
  systems) and `games_manual_fetches` (`result`: ok, cached, moved, deferred,
  skipped, error, none; counts of games) to the Pushgateway.
- **Web:** `brick-games-web`, one replica on brick2000, with the data volume
  (its caches) and the drive's root read-only with HostToContainer, so a
  remounted drive shows up without a restart.
- **uid 1000:** the drive is exFAT, mounted for uid 1000 only, so both pods
  run as 1000 (65532 until 0.7.0); an `own-data` init container gives the
  data volume's files to 1000 (a no-op once it has).
- **Network:** default-deny. The scan reaches only DNS, traces, the
  Pushgateway and HTTPS to the internet (private ranges excluded). The page
  is reachable only from Traefik and Prometheus.

## DOS games

The 250 DOS games are in the catalog as their own system (`dos`, folder
`/extra/games/roms/dos`, since 0.6.0), playing on brick9000 and the RG476H.

brick9000 and the RG476H play DOS games with DOSBox Pure, one zip per game,
and neither has a keyboard. `brick_games/dos.py` makes each zip start its game
straight away (brick-games#5): it copies the zip with an `AUTOBOOT.DBP`, the
file DOSBox Pure's own "auto start" option writes, naming the program from
`brick_games/dos_programs.yaml`. An entry with `dos_yml` also gets a `DOS.YML`
with those DOSBox Pure settings (`cpu_cycles`, `mem_ems`, `run_input`).

```sh
python3 -m brick_games.dos SRC DST   # SRC untouched; unlisted zips are copied unchanged
```

On brick2000 (2026-11-16): `/extra/games/roms/dos` is the built set and
`/extra/games/dos-original` the zips as they came. Games that came packed
(floppy installers, split archives) were installed once into
`/extra/games/dos-work/fixed/<name>/` and zipped from there: from other copies
in `/extra/games/dos-collection`, with 7z, or by running the game's own
installer headless in DOSBox Pure. Every listed pick was booted headless in
DOSBox Pure and checked from a screenshot.

The 23 games not listed don't start as they are and keep the start menu: CD
games without a CD image (Full Throttle, Loom CD, Rayman, Dark Forces, Arena,
Crusader No Remorse, Stonekeep), files missing from the zip (Aces of the
Pacific, Raptor, Pirates Gold, Wizardry 7, Strike Commander, Might and Magic 3
disk 2), and a few that need more digging (Albion, Commander Keen 1, F-117A,
Fallout's paths, Screamer and Toonstruck's DOS/4GW, Privateer and Zone 66's
memory, Theme Park, Crystal Caves). Some listed games still stop once for a
key, a sound setup or a manual question; DOSBox Pure's on-screen keyboard is
on L3.
