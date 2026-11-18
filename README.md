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
  sets aren't games and are left out.
- **Art** comes from libretro's thumbnails (box art, title screen,
  screenshot), matched by a normalized title, as `sources.norm` and `loose`
  describe. Each picture is fetched once, shrunk to 400 px and kept in the
  data volume. Later scans only fetch what's new.
- **Facts** (year, genre, players, developer, publisher) come from
  libretro-database's `metadat`. Coverage is good for cartridge systems and
  patchy for disc systems.
- **Ratings** come from IGDB (critics' average and IGDB users, all eras) and
  RAWG (the Metascore: Metacritic has no API of its own, and only covers
  about 1995 on), as `ratings.py` describes. Each system's platform ids are
  `igdb` and `rawg` in `systems.yaml`. The keys (`IGDB_CLIENT_ID` and
  `IGDB_CLIENT_SECRET`, a Twitch app's; `RAWG_KEY`) come from the
  `brick-games-ratings` Secret, sealed in brick-k8s-secrets. Without them
  there are no ratings and everything else works. Lists are kept in
  `DATA/ratings` and fetched again weekly; a failed fetch keeps the last list.
  The page shows them in each game's details, the best one (Metascore, else
  critics, else users) on its card, and sorts by it under "Best rated".
- **The page** (`python -m brick_games web`) serves `catalog.json`, the art,
  `/health`, `/ready` and `/metrics`. It's read-only and has no ROM access.
- **Plays on**: each system's `devices` in `systems.yaml`, as `good`, `ok` or
  `no`. They're measured on the Retroid Pocket 2+ (homelab-context
  `docs/retroid.md`) and taken from brick-retro's cores for brick9000. The
  RG476H entries are expected values until it arrives. Edit the YAML and the
  next scan picks it up.
- **Manuals**: a search link to the Internet Archive for now. Hosted manuals
  are a follow-up.

A scan that can't reach libretro still lists every game, without art. The
catalog is written whole and then renamed into place.

## Caching

The catalog changes at most once a night and the art almost never, so
browsers keep both and ask only "has it changed?" (sizes as of 2026-11: a
1.6 MB catalog of about 5,000 games, about 13,800 pictures of about 70 kB).

| What | Cache-Control | Validator | Compressed |
| --- | --- | --- | --- |
| `/` | `no-cache` | strong ETag | gzip |
| `/api/catalog` | `no-cache` | strong ETag + Last-Modified | gzip (1.6 MB to ~0.3 MB) |
| `/media/...png?v=<hash>` | `public, max-age=31536000, immutable` | ETag + Last-Modified | no (PNG) |
| `/media/...png` (no `v`) | `public, max-age=604800` | ETag + Last-Modified | no |

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
- `/metrics`: `games_web_requests_total{route,status}` (304s against 200s)
  and `games_web_response_bytes_total{route,encoding}`.

## Run it

```bash
poetry install
poetry run pytest
ROMS_ROOT=/path/to/roms DATA=/tmp/games poetry run python -m brick_games scan
DATA=/tmp/games poetry run python -m brick_games web   # http://localhost:8080
```

## Deploy

`k8s_config/` is rendered by Argo CD (brick-k8s-config `argocd/apps/brick-games.yaml`).
A merge to main is a deploy. The image is `whitepatrick/brick-games`, built by
Jenkins' `build-image` job (brick-cicd-config `jenkins/lib/build.groovy`). A
release bumps `version` in `pyproject.toml` and `newTag` in
`k8s_config/kustomization.yaml`, on a `release/<version>` branch that
`build-branches` builds.

- **Scan:** `brick-games-scan` CronJob, 03:30 every night, at most 4 hours, on
  brick2000 (`chuck.io/storage-node`), with the ROMs as a read-only hostPath.
  It pushes `games_catalog_*` (`with_rating` among them) and
  `games_art_fetches` to the Pushgateway.
- **Web:** `brick-games-web`, one replica on brick2000, with the data volume
  read-only.
- **Network:** default-deny. The scan reaches only DNS, traces, the
  Pushgateway and HTTPS to the internet (private ranges excluded). The page
  is reachable only from Traefik and Prometheus.

## DOS games

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
