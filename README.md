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
  It pushes `games_catalog_*` and `games_art_fetches` to the Pushgateway.
- **Web:** `brick-games-web`, one replica on brick2000, with the data volume
  read-only.
- **Network:** default-deny. The scan reaches only DNS, traces, the
  Pushgateway and HTTPS to the internet (private ranges excluded). The page
  is reachable only from Traefik and Prometheus.
