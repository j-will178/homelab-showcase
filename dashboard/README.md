# Rack Dashboard

A full-screen slideshow for the rack monitor, served by the OptiPlex. It rotates through screens every 5 minutes, and you can jump between them with the keyboard.

**Sections.** The site has a home page and one area per section, all on port 8080:

| URL | What |
|---|---|
| `/home/` | Home page: one tile per section with live lines (drive health, honeypot sessions, what each telescope observes now). Keys **1**–**9** open a section. |
| `/rack/` | This slideshow (the screens below). **Home** key or the *Home* item in the screen bar goes back. |
| `/space/` | Where Hubble and Webb are and what they're scheduled to observe: a top-down view (Sun to the left, each telescope's pointing line with its ▲/▼ tilt and its allowed zone) and a whole-sky map (current target, a fading trail through recent ones with names, the ecliptic, Milky Way plane and Sun limits), plus a card per telescope (now, next 3, last 6). Refreshes every 30 s; **Home** / **R** keys go to the home page / rack dashboard. Data: *Space data* below. |
| `/` | Signpost: a bare `/` goes to `/home/`; `/` with options (the old links, e.g. `/?projection=robinson`) goes to `/rack/` with them, so existing bookmarks and the kiosk keep working. |

Section pages use `<base href="../">`, so they share the files in `static/` (CSS, JS, `vendor/`, `config.json`) and call `api/…` exactly as before; the static demo works the same under its GitHub Pages subpath.

| # | Screen | Data source |
|---|---|---|
| 1 | **WAN noise**: live map of inbound traffic OPNsense blocks | OPNsense filterlog → syslog UDP 5514 |
| 2 | **Drive health**: SMART status of every drive in the rack | Agents on each machine → `POST /api/smart` (every 15 min) |
| 3 | **Compute**: CPU (with a per-core strip), memory, GPU, network, disk I/O, top processes by CPU and by memory (memory summed per program, e.g. `svchost ×71`), uptime, pending reboot, with charts of the last hour | Agents on each machine → `POST /api/metrics` (every 10 s) |
| 4 | **Scheduled tasks**: every task in Task Scheduler's `\Homelab\` folder: state, last/next run, overdue; for backup mirror jobs also files/bytes copied and removed, failures and a 30-run history; for the Kopia cloud job, files changed/unchanged, unreadable files and snapshot size | Agents on Windows hosts, about once a minute, in the metrics posts |
| 5 | **Deployments**: branch, commit, last check/change, result and git version per host, plus a **Deploy all** button | The same metrics posts carry each host's updater status |
| 6 | **Honeypot**: Cowrie SSH/Telnet honeypot, last 24 h: sessions, login attempts, who got a (fake) shell and a summary of what they tried to do (tags + pattern seen N times), blocked downloads, where attackers connect from, top usernames/passwords/commands/countries/SSH clients, live feed, **Export events (CSV)** button (up to 30 days) | Simulated (`honeypot.py`'s `Simulator`, Cowrie-format events through the real parser) until cloud-ops posts the first real events, then the real honeypot for good |
| 7 | **Attack patterns**: honeypot shell sessions over 30 days grouped by the script they ran: leaderboard (24 h / 30 d, IPs, countries, botnet vs single source, first/last seen, 14-day trend), a rotating spotlight (summary, the full normalized script in a scrolling box, credentials, source IPs with country and session count, payload hosts), patterns new in 24 h, and commands no rule recognizes yet. **Click** a pattern (table or new list) to pin it in the spotlight, **↑/↓** to step through, **Esc** or click again to resume; a pin ends after 10 min without interaction, and interacting holds the screen in the rotation | Same honeypot data |

```
OPNsense ──syslog/UDP 5514──────────────┐
                                        ▼
SMART agents ──HTTP POST /api/smart──▶ collector.py (OptiPlex :8080) ──▶ browser (kiosk on rack screen)
  (homeshare, backup-compute, OptiPlex, OPNsense)
```

Everything is served locally (no CDNs). The only Python dependency is `maxminddb`.

## Keys and URL options

| Key | Action |
|---|---|
| **→** / **Space** / **PageDown** | Next screen |
| **←** / **PageUp** | Previous screen |
| **Home** | Back to the home page |
| **1**–**9** | Jump to screen (1 noise, 2 drives, 3 compute, 4 tasks, 5 deployments, 6 honeypot, 7 attack patterns) |
| **R** | Pause / resume rotation |
| **F** | Fullscreen |
| **P** / **Shift+P** | Next / previous map projection (noise screen) |
| **H** | Hide the map side panel (noise screen) |

Screens change with a **halftone wave**: a band of dots riding a moving wave/noise surface, shaded by its slope, sweeps across behind a drifting dark fog; the fog fades in and out while the dots stay crisp, and the screen switches underneath at full cover (~2.2 s). `?transition=fade` or `?transition=none` turn it down or off; it also drops to a plain switch automatically if the OS asks for reduced motion.

URL: `?interval=120` (seconds per screen, `0` = no rotation) · `?screen=smart` / `compute` / `tasks` / `deploy` / `honeypot` / `patterns` (start screen) · `?projection=mollweide` · `?fade=120` · `?home=lat,lon` · `?transition=wave|fade|none` · `?daynight=0` · `?demo=1`

Wall display: `chromium --kiosk "http://optiplex-management.home.arpa:8080/rack/?projection=robinson"` (the old URL without `rack/` still works).

The map shows **day and night** from the real sun position: a faint glow centred under the sun, twilight in 3° steps from sunset to astronomical night, a dotted terminator line and a small sun marker. It updates every minute; toggle it with **Day / night** in the panel.

The noise map keeps running while another screen is showing, so pings don't pause. The screen indicator at the bottom fades after a few seconds and comes back on mouse move or keypress.

## Try it in demo mode

```bash
python3 collector.py --demo     # simulated traffic + four fake hosts' drives
# open http://localhost:8080
```

## Deployment

On the OptiPlex, this folder is deployed automatically by [`deploy/`](../deploy/) to `/opt/rack-dashboard` and runs as `rack-dashboard.service`. The same deploy migrated the old `noise-map.service`. It also installs the OptiPlex's own SMART agent (`rack-smart-agent.timer`) and generates the agent token at `/etc/rack-dashboard/agent-token`.

Local flags (`--home`, a different `--iface`, host order) go in a drop-in, so deploys never overwrite them:
```bash
sudo systemctl edit rack-dashboard
# [Service]
# ExecStart=
# ExecStart=/opt/rack-dashboard/.venv/bin/python /opt/rack-dashboard/collector.py --db /opt/rack-dashboard/dbip-city-lite.mmdb --iface igb0 --smart-order opnsense,optiplex-management,homeshare,backup-compute --home LAT,LON
```

### GeoIP database

**DB-IP City Lite** (free, no account, CC BY 4.0, attribution shown on the page): download the current month's `.mmdb.gz` from https://db-ip.com/db/download/ip-to-city-lite, then:
```bash
gunzip dbip-city-lite-*.mmdb.gz && sudo mv dbip-city-lite-*.mmdb /opt/rack-dashboard/dbip-city-lite.mmdb
sudo systemctl restart rack-dashboard
```
It's updated monthly. **MaxMind GeoLite2-City** also works (free account): add `--attribution maxmind`.

### OPNsense syslog (noise screen)

1. **System → Settings → Logging → Remote → +**: Enabled ✓ · Transport **UDP(4)** · Applications **filter (filterlog)** · Hostname: the OptiPlex's IP · Port **5514**
2. **Firewall → Settings → Advanced**: enable **"Log packets matched from the default block rules"**.
3. On the OptiPlex, allow UDP 5514 from the firewall only, and TCP 8080 from your LAN.

### SMART agents (drive health screen)

See [`agents/README.md`](agents/README.md): Windows (homeshare and 2), OPNsense, and the OptiPlex (installed automatically).

## Compute screen

One card per host, refreshed every 5 s from samples the agents send every 10 seconds (each averaged over its 10 s):

- **CPU**: utilisation with a 1-hour chart, a strip with one cell per logical CPU (40 on backup-compute), load average (Linux/FreeBSD), and temperature where the OS exposes it natively (optiplex-management, opnsense; not Windows yet).
- **Memory**: used/total with chart and bar; swap (Linux/FreeBSD) or commit charge (Windows).
- **GPU**: only shown if `nvidia-smi` is present. Utilisation, VRAM, temperature, power, and **encoder/decoder** use (Jellyfin transcodes).
- **Network**: in/out chart and the busiest NICs with link speed. **Disk I/O**: read/write chart, totals, busiest disk.
- **Top processes** by CPU, as a share of the whole machine, so they compare directly with the CPU %.
- Badges: **reboot pending** (Windows Update / CBS, Debian `reboot-required`), **agent outdated**, **stale** (no sample for 60 s).

Warnings: CPU ≥ 95 %; memory ≥ 90 % (critical 97 %); swap ≥ 50 % used; CPU temperature ≥ 85 °C (critical 95 °C); GPU ≥ 90 °C. Rules are in `metrics.py`.

History is a fixed 360-point buffer per host (1 hour at 10 s) **in memory only**: it can't grow, and it starts over when the dashboard restarts.

## Scheduled tasks screen

One card per task in Task Scheduler's **`\Homelab\`** folder on any Windows host (put a task there and it shows up). Mirror jobs from `backups/` add their own results: last run (succeeded / warnings / failed / aborted), files and bytes copied and removed, failures with the first error lines, and a bar per run for the last 30 runs (height = data copied).

Health: **failed** if the last run failed or was aborted; **overdue** if it hasn't run within its longest normal gap + 10% + 1 h. For mirror jobs that gap comes from the job's schedule in `backups/tasks.json` (e.g. 31 days for monthly on day 1, 17 for days 1 and 15), which the card also shows ("daily at 02:00"); other tasks fall back to an estimate from their last and next run; **running**, **disabled**, or **not run yet**. Tasks that aren't mirror jobs use Task Scheduler's last result (0 = ok). See `backups/README.md`.

### Static demo (GitHub Pages)

`python3 dashboard/tools/build_static_demo.py OUT_DIR` builds a backend-free copy of the dashboard. It's the real UI plus `demo-api.js`, which answers `api/*` from a snapshot of the collector's own demo data, replays the compute history every 10 s, and plays a rollout when *Deploy all* is pressed. The noise map simulates its traffic in the browser already. Used by the public showcase (see the top-level README). `?speed=5` plays it 5× faster; `dashboard/tools/record_demo.py` uses that to record the README's animated hero.

### Credential relay

`/api/creds/key` and `/api/creds/secret` (agent token required) relay the backup share credential between hosts: a public key one way, ciphertext the other. The dashboard never sees the password (see `backups/README.md`, *Share credential*). Stored in `creds.json` in the state directory (mode 600).

## Deployments screen

Every host updates itself from `main` on its own (OptiPlex every 2 min, the others every 15 min). To roll out **now**, press **Deploy all** on the Deployments screen, or from any LAN machine:

```bash
curl -X POST -H 'X-Rack-Deploy: 1' http://optiplex-management.home.arpa:8080/api/deploy
```

Each host's metrics loop sees the request within 10 s and brings its normal update forward: optiplex-management starts `homelab-deploy`; homeshare, backup-compute and opnsense clear their 15-minute stamp, so their every-minute job does update + SMART within a minute. One row per host, from a status file each host's updater writes after every run (`/var/lib/homelab-deploy/status.json`, `deploy-status.json`):

- **Branch** and **commit** (with its subject and age). The commit is amber if it differs from optiplex-management's: that host is the dashboard and checks every 2 min, so its commit is the reference.
- **Last check** (with schedule and when the next one is due) and **last change** (when it last actually installed something).
- **Result**: up to date / deployed, or *failed* (fetch or deploy error) / *rejected* (a changed file failed its syntax check and wasn't installed).
- **Git** client version and agent version.
- While a host's updater is working: **running 1m 39s** and the step it's on (*pulling from GitHub*, *applying scheduled tasks*, *running tests and restarting what changed*), so a long run doesn't look stuck. After an hour it says *stuck?*.
- After **Deploy all**: per-host progress (*updating…* → *done*), and the header counts how many have finished.

There is no login: the request can't run anything, it only makes hosts do their normal update from `main` sooner. It's limited to once a minute, and the `X-Rack-Deploy` header stops other websites from triggering it through your browser. The last request is kept in the state directory, so it survives the dashboard restarting during its own deploy.

## Drive health rules

Each drive gets **OK / Warning / Critical / Unknown**. A host card takes its worst drive's level. A host turns **stale** (dimmed, amber) when it hasn't reported in 45 minutes (`--smart-stale`).

| | Warning | Critical |
|---|---|---|
| SMART overall health | attribute below threshold in the past · self-test errors | **FAILED** · pre-fail attribute at threshold |
| ATA device error log | **new** entries since the drive was first seen (old entries show as a grey note) | – |
| SATA HDD/SSD | reallocated sectors > 0 · reported uncorrectable > 0 · CRC errors > 0 (cable/backplane) | pending > 0 · offline uncorrectable > 0 · reallocated ≥ 100 |
| SAS | grown defects > 0 | uncorrected read/write/verify errors > 0 · grown defects ≥ 100 |
| NVMe | – | critical warning flags · spare below threshold · media errors > 0 |
| SSD wear | ≥ 75 % of rated endurance used | ≥ 90 % |
| Temperature: drive reports its rated max (SCT / device statistics) | 10 °C below the rated max | at the rated max |
| Temperature: otherwise | HDD 50 °C · SSD 60 °C · NVMe 70 °C | HDD 58 °C · SSD 70 °C · NVMe 80 °C |
| Windows | disk HealthStatus *Warning* | *Unhealthy* · uncorrected errors > 0 |

Rules live in `smart.py`. Agents send raw `smartctl` JSON and the collector evaluates it on read, so a rule change applies to existing data as soon as it's deployed.

Agent notes (e.g. a device `smartctl` can't open) are shown under the host but don't change its color.

**Error-log baselines.** A drive's device error log never clears, so a drive with a few errors from years ago would stay amber forever. The dashboard records each drive's error count (by serial) the first time it sees the drive, in `/var/lib/rack-dashboard/baselines.json`. It warns only when the count goes up. After you've looked into a warning and want to accept the new count as normal, re-baseline:
```bash
sudo systemctl stop rack-dashboard
sudo python3 -c "import json; p='/var/lib/rack-dashboard/baselines.json'; d=json.load(open(p)); d.pop('SERIAL', None); json.dump(d, open(p,'w'))"   # or delete the file to re-baseline every drive
sudo systemctl start rack-dashboard
```

## Options

| Flag | Default | Meaning |
|---|---|---|
| `--db PATH` | – | GeoIP `.mmdb` (required unless `--demo`) |
| `--iface` | `igb0` | WAN interface to show (`''` = all interfaces) |
| `--udp-port` / `--http-port` | 5514 / 8080 | Listen ports |
| `--interval` | 300 | Default seconds per screen (`0` = no rotation) |
| `--token-file` | `$CREDENTIALS_DIRECTORY/agent-token` | SMART agents' bearer token. No token = `/api/smart` rejects reports |
| `--state-dir` | `$STATE_DIRECTORY` or `./state` | Where the latest report per host is kept |
| `--smart-order` | – | Host display order, e.g. `opnsense,optiplex-management,homeshare,backup-compute` |
| `--smart-stale` | 45 | Minutes without a report before a host is marked stale |
| `--home lat,lon` | – | Default home for map arcs (or tick **Arcs to home** and click the map) |
| `--include-pass` | off | Also show inbound traffic that was allowed |
| `--dedupe` | 3 s | Ignore repeats of the same source + port within this window |
| `--max-rate` | 40/s | Cap on map events sent to browsers during floods |
| `--attribution` | `dbip` | `dbip`, `maxmind` or `none` (page footer credit) |
| `--demo` | off | Simulated data, nothing else needed |

## Notes

- **Keep the page on your LAN.** It lists attacker IPs, probed ports, and every drive's serial number.
- Hosts that stop reporting disappear after 7 days.
- Map locations are approximate (city level at best). Much of the noise comes from cloud providers, so expect hot spots at data-centre cities.
- The collector logs map counters every 5 minutes and one line per SMART report: `journalctl -u rack-dashboard`.

## Honeypot screen

`honeypot.py` turns [Cowrie](https://github.com/cowrie/cowrie) JSON log lines into events (`parse_cowrie`) and keeps the last 24 h in memory (`HoneypotStore`) for the Honeypot screen (`GET /api/honeypot`, every 3 s while on screen). `HoneypotDB` keeps **30 days** in SQLite (every raw event plus one row per session; older rows are deleted hourly) for the Attack patterns screen (`GET /api/honeypot/patterns`).

**Session summaries and patterns** (`honeypot_rules.py`, no AI):
- `normalize` replaces what bots vary per run (URLs, IPs, SSH keys, base64, random names, the numbers inside `$((…))` arithmetic, and the payload file names a session downloads) with placeholders; the hash of the normalized command sequence is the session's **pattern**, so one botnet's runs group together however often it rotates servers and file names.
- `RULES` map commands to tags (`recon`, `persistence`, `cover-tracks`, `malware-drop`, `execution`, `miner-hunt`, `credential-access`, `iot-probe`, `drop-site`, `honeypot-check`, `upload`) and phrases, which make the summary sentence. `honeypot-check` marks honeypot detectors: shell-fidelity tests (random arithmetic, `$$`, functions, subshells), then `/proc/self/stat` against `/proc/self/status` (`Pid`/`NSpid`) for a real process and a container. Cowrie fails that last step, so these sessions usually end there. Mirai-style `/bin/busybox WORD` marker words (e.g. `HISILICON`) are named in the summary; they differ per botnet variant, so variants stay separate patterns. Commands no rule matches are listed on the Attack patterns screen: that's the to-do list for new rules.
- Fingerprints, tags and summaries are computed from the stored commands when read, never stored, so improving a rule re-labels the whole 30-day history.

**Uploads:** bots that push a file over SCP or SFTP (`scp -t /bin/<name>`) instead of downloading one get a `cowrie.session.file_upload` event. Unlike downloads, these succeed: Cowrie keeps the file on the honeypot as `/var/lib/cowrie/downloads/<sha256>`. The dashboard shows them in the feed (`upload`, target path and hash), in the *Download attempts* tile, as the `upload` tag, and per pattern under *Uploaded files* (SHA-256, file name, sessions). The files themselves stay on the honeypot and are gone at the weekly rebuild; only the hashes reach the dashboard.

**Space data** (`space.py`, `GET /api/space`): where Hubble and Webb are and what they're scheduled to observe, fetched by the collector on a timer and cached in `state/space.json` (viewers never call these sites):

| Data | Source | How often |
|---|---|---|
| Targets: name, pointing RA/Dec, instrument, start/end, execution status, visit type, and the program's title, PI and science category (3 days back, 1 ahead) | STScI's [Space Telescope Live](https://spacetelescopelive.org) back end: `GET /api/get/<webb\|hubble>` with the header `endpoint: schedule/next24hours/<UTC time>` (hour without a leading zero, 10-minute steps), four 24-hour windows per telescope. Webb's records are full visits (program included); Hubble's are flat (`targetName`, times, RA/Dec, instrument, comma-separated target lists shown as `first (+N)`), so each Hubble visit on screen gets its program from `endpoint: <observation id>`, once (at most 10 per refresh, cached in `state/space.json`). Undocumented (worked out from the site's own requests), public, no key; windows too far ahead get 403. | hourly |
| Same, if Space Telescope Live fails | [NASA ACROSS](https://app.across.sciencecloud.nasa.gov/about) API: the published HST and JWST weekly schedules, normalized (no program titles). `sources.schedule:*.via` says which one was used; `error` lists both failures. | hourly |
| Hubble's orbit | [CelesTrak](https://celestrak.org) TLE for NORAD 20580, propagated here (two-body + J2; a picture, not a tracker) | every 12 h |
| Webb's position | [JPL Horizons](https://ssd.jpl.nasa.gov/horizons/) vectors for NAIF −170, Earth-centred ecliptic, interpolated | daily |
| What each target is (type, distance, other names) | CDS [Sesame](https://cds.unistra.fr/cgi-bin/Sesame) resolves the name, [SIMBAD](https://simbad.cds.unistra.fr/simbad/) TAP gives type, redshift and the median measured distance. Names that don't resolve (working names like `ABELL2744-PAR-3`) get SIMBAD's most-studied object within 12′ as the *nearest known object*; a name that resolves more than 1° from the scheduled position is ignored. | once per target |
| A two-sentence summary | Wikipedia page summary (only pages about an astronomical object), credited and linked | once per target |
| Why it's observed | Comes with the Space Telescope Live schedule (title, PI, category); otherwise [MAST](https://mast.stsci.edu): the program's title and PI from its earlier observations. Linked to the STScI program page. | once per program |
| What that sky looks like | CDS [hips2fits](https://alasky.cds.unistra.fr/hips-image-services/hips2fits): 256 px DSS2 colour cutout, ~10′ across, served from `state/space-img/` at `/api/space/image?k=…` (cached files only). A survey image, not the telescope's own. | once per target |

Target facts are fetched in the background (current targets first, then next and recent, at most 6 new per 5 minutes), cached for good in `state/space-info.json` (3000 targets, 400 images kept), and attached to each visit in `/api/space` as `info`; a lookup that fails backs off for 6 h. In the page, each telescope card shows the info panel for its current target; clicking a row in its list shows that target instead, and after 2 minutes without a click it goes back to the current one.

Each source fails on its own (retried after 15 min) and the last good data stays; `sources` in the reply shows each one's last success and error. Exposures of one target are merged into visits; the reply has each telescope's position (ecliptic longitude/latitude; altitude or distance), its `current` visit, the 6 `recent` ones and the 3 `next`, each with RA/Dec, ecliptic direction and angle from the Sun, plus the Sun's position and each telescope's Sun-angle limits (Hubble ≥ 50°, Webb 85–135°). These are **planned** pointings, not telemetry. `--space off` turns it off; `--demo` and the static demo use generated targets that respect the Sun limits.

**CSV export:** the **Export events (CSV)** button on the Honeypot screen, or `GET /api/honeypot/export.csv`, downloads every stored event (up to 30 days; `?days=N` for less), oldest first, one row per event: `time_utc, session, event, src_ip, country_code, country, city, protocol, username, password, command, url, client_version, duration_s, file_name, sha256` (`file_name`/`sha256` on `upload` rows; `url` there is the SCP target path). It's what the dashboard stores (fields capped and stripped of control characters by `parse_cowrie`), not Cowrie's raw JSON, which stays on the honeypot. Attacker text that starts with `=`, `+`, `-` or `@` gets a leading `'`, so Excel and LibreOffice never run it as a formula; strip that `'` if you load the file with code. The file is UTF-8 with a BOM and streams in pages, so a 30-day export doesn't load everything into memory. The static demo ships a 24 h simulated file instead.

**The data is simulated for now** (and kept in RAM only, so it can never mix with the real history on disk). `Simulator` writes Cowrie-format lines (same event ids and fields, and Cowrie's default user database: `root` gets in with any password except `root` and `123456`) and feeds them through the same parser, so the parsing, analysis and aggregation are already the real ones. Its scripts vary payload hosts, file names and keys like real botnets, and rarer "campaigns" start at different times (including during the last day), so grouping and "new in 24 h" are exercised. The screen says *simulated data* until a real source is connected. Simulated attackers use documentation IP ranges and `example.net`.

Everything on this screen is typed by attackers, so it's treated as hostile: fields are length-limited and stripped of control characters when parsed, unknown event ids are dropped, the page escapes everything, and download URLs are shown defanged (`hxxp://`) and never as links.

**Real data:** cloud-ops' puller (`cloud-ops/honeypot/hp-pull`) reads Cowrie's log through the bastion and posts it to `POST /api/honeypot/ingest` (agent token, at most 2 MB and 20 000 lines per post; a batch id makes retries harmless). The lines are attacker-controlled and only ever go through `parse_cowrie`. `HoneypotFeed` shows simulated data until the first real lines arrive, then switches for good to `state/honeypot.db`; after a restart that file's presence keeps it real, and the last 24 h are reloaded from it. Every post also carries cloud-ops' status and the honeypot's health snapshot (`clean_health` keeps only known fields, typed and capped: it comes from a machine attackers may control). They drive the chips on the Honeypot screen: pull state, mode (`lab-debug` / `lab` / `public`), Cowrie and port problems, current image, last rebuild. **D**, or clicking a health chip, opens the diagnostics view: the snapshot's facts, in `lab-debug` the honeypot's logs, and the last lines of a failed rebuild. `--demo` has no state directory, so it can't switch. `honeypot-reset` holds a token: a new value in git wipes the stored history once (going public cleared the lab test sessions); the token is recorded in the database, so restarts don't wipe again.
