#!/usr/bin/env bash
# homelab-deploy: pull j-will178/homelab and deploy whatever changed.
#
# Runs on the Linux lab hosts from a systemd timer (see homelab-deploy.timer). What a host
# deploys depends on its role, from /etc/homelab/role:
#   dashboard (default, no file)  optiplex-management: the rack dashboard + its local agents
#   cloud-ops                     the cloud-ops VM: metrics agent + host baseline (cloud-ops/)
# The checkout at $REPO_DIR is a read-only mirror of origin/main: never edit
# files there, local changes are discarded on every deploy.
#
# Everything lives inside main() so bash parses the whole script before
# running it. This matters because `git reset` below can rewrite this file
# while it is executing. Right after the reset, the script re-execs itself
# (`--apply OLD NEW`) so the deploy is done by the NEW commit's version of
# this script, not the one that was already loaded in memory.

main() {
  set -euo pipefail

  local REPO_DIR="${REPO_DIR:-/opt/homelab}"
  local BRANCH="${BRANCH:-main}"
  local STATE_DIR="${STATE_DIR:-/var/lib/homelab-deploy}"
  local LOCK_FILE="${LOCK_FILE:-/run/homelab-deploy.lock}"
  ROLE=""
  [[ -r /etc/homelab/role ]] && ROLE="$(tr -d '[:space:]' < /etc/homelab/role)"
  ROLE="${ROLE:-dashboard}"
  case "$ROLE" in
    dashboard|cloud-ops) ;;
    *) log "unknown role '$ROLE' in /etc/homelab/role (expected dashboard or cloud-ops)"; return 1 ;;
  esac
  export ROLE

  mkdir -p "$STATE_DIR"

  # Second half of a deploy, run by the freshly checked-out script (the lock on
  # fd 9 is inherited across exec, so it is still held).
  if [[ "${1:-}" == "--apply" ]]; then
    cd "$REPO_DIR"
    apply "$2" "$3"
    return
  fi

  exec 9>"$LOCK_FILE"
  if ! flock -n 9; then
    log "another deploy is running, skipping"
    return 0
  fi

  cd "$REPO_DIR"
  if ! git fetch --quiet origin "$BRANCH"; then
    write_status failed "git fetch failed"
    return 1
  fi

  local new old
  new="$(git rev-parse "origin/$BRANCH")"
  old="$(cat "$STATE_DIR/deployed-rev" 2>/dev/null || true)"

  if [[ "$new" == "$old" ]]; then
    ensure_local_agent || log "$ROLE: WARNING local agent check failed"   # self-heal, cheap
    write_status ok "up to date"
    return 0
  fi
  # Don't retry a commit that already failed (it would restart services every
  # run). Push a fix, or force a retry: sudo rm /var/lib/homelab-deploy/failed-rev
  if [[ "$new" == "$(cat "$STATE_DIR/failed-rev" 2>/dev/null || true)" ]]; then
    write_status failed "${new:0:7} failed to deploy earlier; not retrying until main changes"
    return 0
  fi

  log "deploying ${old:0:7}${old:+ -> }${new:0:7}"
  mark_running "deploying ${new:0:7}"
  git reset --hard --quiet "$new"
  exec "$REPO_DIR/deploy/homelab-deploy.sh" --apply "$old" "$new"
}

# apply OLD NEW: deploy the projects that changed between OLD and NEW (NEW is
# checked out). Called via `homelab-deploy.sh --apply`, i.e. always by NEW's code.
apply() {
  local old="$1" new="$2"

  mark_running "running tests and restarting what changed"

  # Which paths changed? First run, or old commit no longer in history
  # (force-push): treat everything as changed.
  local changed
  if [[ -n "$old" ]] && git cat-file -e "$old^{commit}" 2>/dev/null; then
    changed="$(git diff --name-only "$old" "$new")"
  else
    log "no previous deploy recorded: deploying all projects"
    changed="__ALL__"
  fi

  local failed=0
  if [[ "$ROLE" == "dashboard" ]] && touches "$changed" "dashboard/"; then
    run_step dashboard "$changed" || failed=1
  fi
  if [[ "$ROLE" == "cloud-ops" ]] && { touches "$changed" "cloud-ops/" || touches "$changed" "honeypot/" || touches "$changed" "dashboard/agents/metrics_agent.py"; }; then
    run_step cloudops "$changed" || failed=1
  fi

  # Add future projects here: write deploy_<name> and rollback_<name>, then
  # if touches "$changed" "<folder>/"; then run_step <name> "$changed" || failed=1; fi

  if (( failed )); then
    echo "$new" > "$STATE_DIR/failed-rev"
    log "deploy of ${new:0:7} FAILED (rolled back where possible); won't retry this commit until main changes"
    write_status failed "deploy of ${new:0:7} failed (rolled back where possible)"
    return 1
  fi

  ensure_local_agent || log "$ROLE: WARNING local agent setup failed"

  rm -f "$STATE_DIR/failed-rev"
  echo "$new" > "$STATE_DIR/deployed-rev"
  log "deployed ${new:0:7}"
  write_status ok "deployed ${new:0:7}" changed
}

# mark_running PHASE: while deploying, record "running since" + the current step in
# status.json for the Deployments screen. write_status (end of every run) replaces the
# file, which clears both. Never fatal.
mark_running() {
  python3 - "$STATE_DIR/status.json" "$1" <<'PY' || true
import json, os, sys, time
path, phase = sys.argv[1:3]
try:
    st = json.load(open(path))
except (OSError, ValueError):
    st = {}
st.setdefault("running_since", int(time.time()))
st["phase"] = phase
with open(path + ".tmp", "w") as f:
    json.dump(st, f)
os.replace(path + ".tmp", path)
PY
}

# write_status RESULT MESSAGE [changed]: $STATE_DIR/status.json, read by the local
# metrics agent and shown on the dashboard's Deployments screen. Never fatal.
# (Uses main()'s locals STATE_DIR / BRANCH / REPO_DIR via bash's dynamic scoping.)
write_status() {
  local commit
  commit="$(cat "$STATE_DIR/deployed-rev" 2>/dev/null || true)"
  python3 - "$STATE_DIR/status.json" "$1" "$2" "${3:-}" "$commit" "$BRANCH" \
      "$(git --version 2>/dev/null | awk '{print $3}')" \
      "$( [[ -n "$commit" ]] && git -C "$REPO_DIR" log -1 --format='%ct%x09%s' "$commit" 2>/dev/null || true)" <<'PY' || true
import json, os, sys, time
path, result, msg, changed, commit, branch, git, log = sys.argv[1:9]
try:
    old = json.load(open(path))
except (OSError, ValueError):
    old = {}
now = int(time.time())
ct, _, subject = log.partition("\t")
st = {"checked_at": now, "changed_at": now if changed else old.get("changed_at"), "every_s": 120,
      "result": result, "message": msg[:200], "branch": branch, "commit": commit or None,
      "subject": subject[:120] or None, "committed_at": int(ct) if ct.isdigit() else None, "git": git or None}
with open(path + ".tmp", "w") as f:
    json.dump(st, f)
os.replace(path + ".tmp", path)
PY
}

log() { echo "homelab-deploy: $*"; }

# touches CHANGED PREFIX: true if any changed path starts with PREFIX
touches() {
  [[ "$1" == "__ALL__" ]] && return 0
  grep -q "^$2" <<< "$1"
}

# run_step NAME CHANGED: run deploy_NAME; on failure run rollback_NAME.
# NOTE: bash ignores `set -e` inside anything called from `f || x`, so the
# deploy_/rollback_ functions must check every step explicitly (`|| return 1`).
run_step() {
  local name="$1" changed="$2"
  if ! "deploy_$name" "$changed"; then
    log "$name: deploy failed, rolling back"
    "rollback_$name" || log "$name: rollback FAILED, needs a look"
    return 1
  fi
}

# ---- dashboard (rack-dashboard.service, formerly noise-map) -----------------
DB_DST="/opt/rack-dashboard"
DB_UNIT="rack-dashboard.service"
# /api/smart only exists in the dashboard, so the old noise-map can't pass this check.
DB_HEALTH_URL="${DASHBOARD_HEALTH_URL:-http://127.0.0.1:8080/api/smart}"
DB_TOKEN="/etc/rack-dashboard/agent-token"
DB_AGENT_UNITS=(rack-smart-agent.service rack-smart-agent.timer rack-metrics-agent.service)
OLD_DST="/opt/noise-map"
OLD_UNIT="noise-map.service"
# Local-only files in $DB_DST that must survive a sync.
DB_KEEP=(--exclude '.venv/' --exclude '*.mmdb' --exclude '__pycache__/' --exclude '.snapshot-complete')

deploy_dashboard() {
  local changed="$1"
  local src="$REPO_DIR/dashboard" prev="$STATE_DIR/dashboard.prev" first_install=0 f

  # Clear any old snapshot first, so a failure in step 1 can't trigger a
  # rollback to a stale copy.
  rm -rf "$prev"

  # 1. Sanity check before touching anything live.
  for f in collector.py smart.py metrics.py creds.py agents/smart_agent.py agents/metrics_agent.py; do
    python3 -c 'import ast,sys; ast.parse(open(sys.argv[1]).read())' "$src/$f" \
      || { log "dashboard: $f has a syntax error, not deploying"; return 1; }
  done
  if [[ -d "$src/tests" ]]; then
    ( cd "$src" && python3 -m unittest discover -s tests -q 2>&1 ) \
      || { log "dashboard: unit tests failed, not deploying"; return 1; }
  fi

  # 2. First install: venv, and carry the GeoIP database over from noise-map.
  if [[ ! -x "$DB_DST/.venv/bin/python" ]]; then
    log "dashboard: first install in $DB_DST"
    first_install=1
    mkdir -p "$DB_DST" || return 1
    python3 -m venv "$DB_DST/.venv" || return 1
    "$DB_DST/.venv/bin/pip" install --quiet -r "$src/requirements.txt" || return 1
    if ! compgen -G "$DB_DST/*.mmdb" >/dev/null && compgen -G "$OLD_DST/*.mmdb" >/dev/null; then
      cp -p "$OLD_DST"/*.mmdb "$DB_DST/" || return 1
      log "dashboard: copied GeoIP database from $OLD_DST"
    fi
  fi

  # 3. Agent token (created once; agents on other machines need a copy).
  if [[ ! -s "$DB_TOKEN" ]]; then
    install -d -m 0700 "$(dirname "$DB_TOKEN")" || return 1
    ( umask 077; python3 -c 'import secrets; print(secrets.token_hex(32))' > "$DB_TOKEN" ) || return 1
    log "dashboard: generated agent token at $DB_TOKEN"
  fi

  # 4. Snapshot the current deployment for rollback.
  mkdir -p "$prev" || return 1
  rsync -a "${DB_KEEP[@]}" "$DB_DST/" "$prev/" || return 1
  touch "$prev/.snapshot-complete" || return 1

  # 5. Sync code.
  rsync -a --delete "${DB_KEEP[@]}" "$src/" "$DB_DST/" || return 1

  # 6. Dependencies, only when requirements.txt changed (first install already did it).
  if (( ! first_install )) && touches "$changed" "dashboard/requirements.txt"; then
    log "dashboard: updating venv"
    "$DB_DST/.venv/bin/pip" install --quiet -r "$DB_DST/requirements.txt" || return 1
  fi

  # 7. Unit file: install when missing or when it changed in git. Local flags
  #    belong in a drop-in (`systemctl edit rack-dashboard`), which this never touches.
  if [[ ! -f "/etc/systemd/system/$DB_UNIT" ]] || touches "$changed" "dashboard/$DB_UNIT"; then
    log "dashboard: installing $DB_UNIT"
    install -m 0644 "$src/$DB_UNIT" "/etc/systemd/system/$DB_UNIT" || return 1
    # Carry over a noise-map drop-in (e.g. --home) on migration, with paths updated.
    if [[ -d "/etc/systemd/system/$OLD_UNIT.d" && ! -d "/etc/systemd/system/$DB_UNIT.d" ]]; then
      cp -r "/etc/systemd/system/$OLD_UNIT.d" "/etc/systemd/system/$DB_UNIT.d" || return 1
      sed -i "s#$OLD_DST#$DB_DST#g" "/etc/systemd/system/$DB_UNIT.d/"*.conf || return 1
      log "dashboard: copied $OLD_UNIT drop-in to $DB_UNIT"
    fi
    systemctl daemon-reload || return 1
    systemctl enable --quiet "$DB_UNIT" || return 1
  fi

  # 8. Migration: stop the old noise-map service (it holds ports 8080/5514).
  if systemctl is-active --quiet "$OLD_UNIT" 2>/dev/null; then
    log "dashboard: stopping $OLD_UNIT (replaced by $DB_UNIT)"
    touch "$STATE_DIR/noise-map-stopped" || return 1
    systemctl stop "$OLD_UNIT" || return 1
  fi

  # 9. Restart and health check.
  systemctl restart "$DB_UNIT" || return 1
  wait_healthy "$DB_HEALTH_URL" "$DB_UNIT" || { log "dashboard: not healthy after restart"; return 1; }
  log "dashboard: healthy"

  # 10. Migration done: keep noise-map from coming back at boot.
  if [[ -f "$STATE_DIR/noise-map-stopped" ]]; then
    systemctl disable --quiet "$OLD_UNIT" 2>/dev/null || true
    rm -f "$STATE_DIR/noise-map-stopped"
    log "dashboard: migration from noise-map complete ($OLD_DST left in place; delete it when you're happy)"
  fi

  # 11. This machine's own agents are checked by apply() after every deploy
  #     (and on every run with nothing new), so a failure there can self-heal.
  return 0
}

install_local_agent() {
  # Make the local agent units match $DB_DST/agents and be running. Idempotent:
  # compares file contents, so it does nothing (and restarts nothing) when all is well.
  local u need=0
  [[ -d "$DB_DST/agents" ]] || return 0          # dashboard not installed yet
  for u in "${DB_AGENT_UNITS[@]}"; do
    [[ -f "$DB_DST/agents/$u" ]] || { log "dashboard: $u missing from $DB_DST/agents"; return 1; }
    if ! cmp -s "$DB_DST/agents/$u" "/etc/systemd/system/$u"; then
      install -m 0644 "$DB_DST/agents/$u" "/etc/systemd/system/$u" || return 1
      need=1
    fi
  done
  # Metrics used to be a 1-minute timer; it's now a long-running service.
  if [[ -f /etc/systemd/system/rack-metrics-agent.timer ]]; then
    systemctl disable --now --quiet rack-metrics-agent.timer || true
    rm -f /etc/systemd/system/rack-metrics-agent.timer
    need=1
  fi
  if (( need )); then
    systemctl daemon-reload || return 1
    log "dashboard: local agent units installed/updated (SMART every 15 min, metrics every 10 s)"
  fi
  systemctl is-enabled --quiet rack-smart-agent.timer || systemctl enable --quiet rack-smart-agent.timer || return 1
  systemctl is-active --quiet rack-smart-agent.timer || systemctl start rack-smart-agent.timer || return 1
  systemctl is-enabled --quiet rack-metrics-agent.service || systemctl enable --quiet rack-metrics-agent.service || return 1
  if (( need )); then
    systemctl restart rack-metrics-agent.service || return 1
  elif ! systemctl is-active --quiet rack-metrics-agent.service; then
    log "dashboard: metrics agent was not running, starting it"
    systemctl start rack-metrics-agent.service || return 1
  fi
}

rollback_dashboard() {
  local prev="$STATE_DIR/dashboard.prev" restored_url=""
  if [[ -f "$prev/.snapshot-complete" ]]; then
    rsync -a --delete "${DB_KEEP[@]}" "$prev/" "$DB_DST/" || return 1
  fi
  # Failed during migration: put noise-map back exactly as it was.
  if [[ -f "$STATE_DIR/noise-map-stopped" ]]; then
    log "dashboard: restoring $OLD_UNIT"
    systemctl stop "$DB_UNIT" 2>/dev/null || true
    systemctl disable --quiet "$DB_UNIT" 2>/dev/null || true
    systemctl start "$OLD_UNIT" || return 1
    restored_url="http://127.0.0.1:8080/config.json"   # noise-map has no /api/smart
  elif [[ -f "$prev/.snapshot-complete" ]]; then
    systemctl restart "$DB_UNIT" || return 1
  else
    log "dashboard: failed before anything live changed, nothing to roll back"
    return 0
  fi
  local unit="$DB_UNIT"
  [[ -f "$STATE_DIR/noise-map-stopped" ]] && unit="$OLD_UNIT"
  if wait_healthy "${restored_url:-$DB_HEALTH_URL}" "$unit"; then
    log "dashboard: rollback healthy"
  else
    log "dashboard: rollback ALSO unhealthy, check: journalctl -u $DB_UNIT -u $OLD_UNIT"
    rm -f "$STATE_DIR/noise-map-stopped"
    return 1
  fi
  rm -f "$STATE_DIR/noise-map-stopped"
}

# ---- cloud-ops (the VM on backup-compute; see cloud-ops/README.md) ----------
CO_SRC_FILES=(rack-metrics-agent.service nftables.conf 50-cloud-ops-sshd.conf 20auto-upgrades 52unattended-reboot)
# Honeypot operations (cloud-ops/honeypot/): file -> "destination mode"
declare -A HP_DEST=(
  [hp-pull]="/usr/local/bin/hp-pull 0755"
  [hp-rebuild]="/usr/local/sbin/hp-rebuild 0755"
  [hp-secrets]="/usr/local/sbin/hp-secrets 0755"
  [hp-pull.service]="/etc/systemd/system/hp-pull.service 0644"
  [hp-rebuild.service]="/etc/systemd/system/hp-rebuild.service 0644"
  [hp-rebuild.timer]="/etc/systemd/system/hp-rebuild.timer 0644"
  [hp-refresh.service]="/etc/systemd/system/hp-refresh.service 0644"
  [hp-refresh.timer]="/etc/systemd/system/hp-refresh.timer 0644"
)
CO_AGENT="/opt/rack-agent/metrics_agent.py"
CO_TOKEN="/etc/rack-agent/token"
declare -A CO_DEST=(
  [rack-metrics-agent.service]=/etc/systemd/system/rack-metrics-agent.service
  [nftables.conf]=/etc/nftables.conf
  [50-cloud-ops-sshd.conf]=/etc/ssh/sshd_config.d/50-cloud-ops.conf
  [20auto-upgrades]=/etc/apt/apt.conf.d/20auto-upgrades
  [52unattended-reboot]=/etc/apt/apt.conf.d/52unattended-reboot
)

deploy_cloudops() {
  local src="$REPO_DIR/cloud-ops/files" prev="$STATE_DIR/cloudops.prev" f dst missing=()

  rm -rf "$prev"
  # 1. Checks before touching anything live.
  python3 -c 'import ast,sys; ast.parse(open(sys.argv[1]).read())' "$REPO_DIR/dashboard/agents/metrics_agent.py" \
    || { log "cloud-ops: metrics_agent.py has a syntax error, not deploying"; return 1; }
  [[ -s "$CO_TOKEN" ]] || { log "cloud-ops: $CO_TOKEN missing (one-time setup step, see cloud-ops/README.md)"; return 1; }
  for p in nftables unattended-upgrades gnupg unzip rsync; do dpkg -s "$p" >/dev/null 2>&1 || missing+=("$p"); done
  if (( ${#missing[@]} )); then
    log "cloud-ops: installing ${missing[*]}"
    DEBIAN_FRONTEND=noninteractive apt-get install -y -q "${missing[@]}" >/dev/null || return 1
  fi
  nft -c -f "$src/nftables.conf" || { log "cloud-ops: nftables.conf invalid, not deploying"; return 1; }
  python3 -m py_compile "$REPO_DIR/cloud-ops/honeypot/hp-pull" 2>/dev/null \
    || { log "cloud-ops: hp-pull has a syntax error, not deploying"; return 1; }
  for f in hp-rebuild hp-secrets; do
    bash -n "$REPO_DIR/cloud-ops/honeypot/$f" || { log "cloud-ops: $f has a syntax error, not deploying"; return 1; }
  done

  # 2. Snapshot what's live now (for rollback), then install.
  mkdir -p "$prev" || return 1
  for f in "${CO_SRC_FILES[@]}"; do
    dst="${CO_DEST[$f]}"
    [[ -f "$dst" ]] && { cp -p "$dst" "$prev/$f" || return 1; }
  done
  [[ -f "$CO_AGENT" ]] && { cp -p "$CO_AGENT" "$prev/metrics_agent.py" || return 1; }
  for f in "${!HP_DEST[@]}"; do
    dst="${HP_DEST[$f]% *}"
    [[ -f "$dst" ]] && { cp -p "$dst" "$prev/hp.$f" || return 1; }
  done
  touch "$prev/.snapshot-complete"

  install -d -m 0755 /opt/rack-agent || return 1
  install -m 0644 "$REPO_DIR/dashboard/agents/metrics_agent.py" "$CO_AGENT" || return 1
  for f in "${CO_SRC_FILES[@]}"; do
    install -m 0644 "$src/$f" "${CO_DEST[$f]}" || return 1
  done

  # 3. Apply: sshd only if its config is valid (never lock ourselves out), firewall, agent.
  sshd -t || { log "cloud-ops: sshd config invalid"; return 1; }
  systemctl reload ssh || return 1
  systemctl enable --quiet nftables.service || return 1
  systemctl restart nftables.service || return 1
  systemctl enable --quiet unattended-upgrades.service || return 1
  systemctl daemon-reload || return 1
  systemctl enable --quiet rack-metrics-agent.service || return 1
  systemctl restart rack-metrics-agent.service || return 1
  sleep 3
  systemctl is-active --quiet rack-metrics-agent.service || { log "cloud-ops: metrics agent not running"; return 1; }

  # 4. Honeypot operations: verified HashiCorp tools, the lab key, puller and rebuild timers.
  deploy_cloudops_honeypot "$1" || return 1
  log "cloud-ops: healthy"
}

deploy_cloudops_honeypot() {
  local changed="$1" f dst mode
  # shellcheck source=/dev/null
  ( source "$REPO_DIR/cloud-ops/tools.env" &&
    install -m 0755 "$REPO_DIR/cloud-ops/files/hc-install" /usr/local/sbin/hc-install &&
    hc-install terraform "$TERRAFORM_VERSION" "$HASHICORP_FINGERPRINT" &&
    hc-install packer "$PACKER_VERSION" "$HASHICORP_FINGERPRINT" ) \
    || { log "cloud-ops: installing Terraform/Packer failed (verification or download)"; return 1; }
  install -d -m 0700 /etc/honeypot || return 1
  # The lab key (cloud-ops -> bastion -> honeypot) exists from the start, so the puller can start.
  [[ -f /etc/honeypot/lab_ed25519 ]] || ssh-keygen -q -t ed25519 -N "" -C cloud-ops-honeypot-lab -f /etc/honeypot/lab_ed25519 || return 1
  for f in "${!HP_DEST[@]}"; do
    dst="${HP_DEST[$f]% *}"; mode="${HP_DEST[$f]#* }"
    install -m "$mode" "$REPO_DIR/cloud-ops/honeypot/$f" "$dst" || return 1
  done
  systemctl daemon-reload || return 1
  systemctl enable --quiet --now hp-rebuild.timer hp-refresh.timer || return 1
  systemctl enable --quiet hp-pull.service || return 1
  systemctl restart hp-pull.service || return 1
  sleep 2
  systemctl is-active --quiet hp-pull.service || { log "cloud-ops: hp-pull not running"; return 1; }
  # Infrastructure changes in git reach DigitalOcean now, not at the next timer (once set up).
  if [[ -s /etc/honeypot/do_token ]]; then
    if touches "$changed" "honeypot/packer/"; then
      log "cloud-ops: honeypot image changed: starting a rebuild"; systemctl start --no-block hp-rebuild.service
    elif touches "$changed" "honeypot/terraform/"; then
      log "cloud-ops: honeypot infrastructure changed: starting terraform apply"; systemctl start --no-block hp-refresh.service
    fi
  fi
}

rollback_cloudops() {
  local prev="$STATE_DIR/cloudops.prev" f
  [[ -f "$prev/.snapshot-complete" ]] || { log "cloud-ops: failed before anything live changed, nothing to roll back"; return 0; }
  for f in "${CO_SRC_FILES[@]}"; do
    if [[ -f "$prev/$f" ]]; then install -m 0644 "$prev/$f" "${CO_DEST[$f]}" || return 1
    else rm -f "${CO_DEST[$f]}"; fi
  done
  [[ -f "$prev/metrics_agent.py" ]] && install -m 0644 "$prev/metrics_agent.py" "$CO_AGENT"
  for f in "${!HP_DEST[@]}"; do
    if [[ -f "$prev/hp.$f" ]]; then install -m "${HP_DEST[$f]#* }" "$prev/hp.$f" "${HP_DEST[$f]% *}"
    else rm -f "${HP_DEST[$f]% *}"; fi
  done
  systemctl daemon-reload
  sshd -t && systemctl reload ssh
  [[ -f /etc/nftables.conf ]] && systemctl restart nftables.service
  systemctl restart rack-metrics-agent.service 2>/dev/null || true
  if [[ -f /etc/systemd/system/hp-pull.service ]]; then systemctl restart hp-pull.service 2>/dev/null || true
  else systemctl disable --now hp-pull.service hp-rebuild.timer hp-refresh.timer 2>/dev/null || true; fi
  log "cloud-ops: rolled back"
}

# ensure_local_agent: this host's agents installed and running (cheap; run on every check).
ensure_local_agent() {
  case "$ROLE" in
    dashboard) install_local_agent ;;
    cloud-ops)
      [[ -f /etc/systemd/system/rack-metrics-agent.service ]] || return 0   # not deployed yet
      systemctl is-active --quiet rack-metrics-agent.service || {
        log "cloud-ops: metrics agent was not running, starting it"
        systemctl start rack-metrics-agent.service
      } ;;
  esac
}

# wait_healthy URL [UNIT]: URL answers 2xx (and UNIT, if given, is active) within ~15 s
wait_healthy() {
  local url="$1" unit="${2:-}"
  for _ in $(seq 1 15); do
    if curl -fs --max-time 2 -o /dev/null "$url" 2>/dev/null \
       && { [[ -z "$unit" ]] || systemctl is-active --quiet "$unit"; }; then
      return 0
    fi
    sleep 1
  done
  return 1
}

main "$@"
