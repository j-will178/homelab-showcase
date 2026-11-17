#!/bin/sh
# OPNsense: called by the configd action from the GUI cron, every minute.
# Every run: make sure the metrics loop (a sample every 10 s) is running.
# Every 15 min: update from GitHub, then the SMART report. Arguments are passed
# through to both agents (the metrics agent rewrites /api/smart to /api/metrics).
#
# The metrics loop runs under daemon(8) -r, which restarts it whenever it exits;
# the agent exits by itself when metrics_agent.py is updated, so updates take
# effect within one interval. This script only (re)starts the supervisor if it's gone.
#
#   1. pull   latest main into $D/repo (sparse: dashboard/agents, read-only deploy key)
#   2. check  changed Python files parse; changed shell files pass `sh -n`
#   3. install  atomically (mv), previous copy kept as .prev; failures are NOT installed
#   4. run    the agent, whatever happened above (a failed fetch never stops reporting)

D=${RACK_AGENT_DIR:-/usr/local/rack-agent}
R=$D/repo
LOG=${RACK_AGENT_LOG:-/var/log/rack-agent.log}
STAMP=$D/last-full-run
FULL_EVERY_MIN=${RACK_AGENT_FULL_EVERY:-15}
PY=${RACK_AGENT_PYTHON:-/usr/local/bin/python3}
LOOP_SECONDS=${RACK_AGENT_METRICS_EVERY:-10}
PIDFILE=$D/metrics-supervisor.pid
ARGSFILE=$D/metrics-args
STATUS=$D/deploy-status.json

log() { echo "$(date -u '+%Y-%m-%d %H:%M:%SZ') [update] $*" | tee -a "$LOG" >&2; }

check() {   # check FILE: does it parse?
    case "$1" in
        *.py) "$PY" -c 'import ast,sys; ast.parse(open(sys.argv[1]).read())' "$1" 2>/dev/null ;;
        *.sh) sh -n "$1" 2>/dev/null ;;
        *) true ;;
    esac
}

write_status() {   # write_status RESULT MESSAGE [changed]: shown on the Deployments screen
    c=$(git -C "$R" rev-parse HEAD 2>/dev/null)
    "$PY" - "$STATUS" "$1" "$2" "${3:-}" "$c" "$(git --version 2>/dev/null | awk '{print $3}')" \
        "$(git -C "$R" log -1 --format='%ct%x09%s' 2>/dev/null)" "$FULL_EVERY_MIN" <<'PY' || true
import json, os, sys, time
path, result, msg, changed, commit, git, log, every = sys.argv[1:9]
try:
    old = json.load(open(path))
except (OSError, ValueError):
    old = {}
now = int(time.time())
ct, _, subject = log.partition("\t")
st = {"checked_at": now, "changed_at": now if changed else old.get("changed_at"), "every_s": int(every) * 60,
      "result": result, "message": msg[:200], "branch": "main", "commit": commit or None,
      "subject": subject[:120] or None, "committed_at": int(ct) if ct.isdigit() else None, "git": git or None}
with open(path + ".tmp", "w") as f:
    json.dump(st, f)
os.replace(path + ".tmp", path)
PY
}

mark_running() {   # mark_running PHASE: "running since" + step for the Deployments screen; write_status clears it
    "$PY" - "$STATUS" "$1" <<'PY' || true
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

install_file() {   # install_file NAME: repo copy -> $D, if changed and valid
    src=$R/dashboard/agents/$1; dst=$D/$(basename "$1")
    [ -f "$src" ] || return 0
    cmp -s "$src" "$dst" 2>/dev/null && return 0
    if ! check "$src"; then
        log "$1 at $short has errors: NOT installed, keeping current copy"
        rejected="$rejected $1"
        return 0
    fi
    [ -f "$dst" ] && cp -p "$dst" "$dst.prev"
    cp "$src" "$dst.new" && chmod 700 "$dst.new" && mv -f "$dst.new" "$dst" && log "installed $1 from $short" && installed=1
}

update() {
    [ -d "$R/.git" ] || { log "no repo checkout: auto-update not set up, skipping"; write_status failed "auto-update not set up"; return 0; }
    before=$(git -C "$R" rev-parse HEAD) || return 1
    mark_running "pulling from GitHub"
    git -C "$R" fetch -q --depth 1 origin main || { write_status failed "git fetch failed"; return 1; }
    after=$(git -C "$R" rev-parse FETCH_HEAD) || return 1
    short=$(echo "$after" | cut -c1-7)
    if [ "$after" != "$before" ]; then
        git -C "$R" reset -q --hard FETCH_HEAD || return 1
        log "repo $(echo "$before" | cut -c1-7) -> $short"
    fi
    rejected=""; installed=0
    install_file smart_agent.py
    install_file metrics_agent.py
    install_file opnsense/run.sh       # replaces this script by rename; the running copy is unaffected
    if [ -n "$rejected" ]; then
        write_status rejected "not installed (failed checks):$rejected"
    elif [ "$installed" = 1 ] || [ "$after" != "$before" ]; then
        write_status ok "updated to $short" changed
    else
        write_status ok "up to date"
    fi
    return 0
}

ensure_metrics() {   # start the supervised metrics loop unless it's already running
    [ -f "$D/metrics_agent.py" ] || return 0
    # "Deploy now" from the dashboard clears $STAMP, so the next run (within a minute) is a full one.
    set -- --loop "$LOOP_SECONDS" --deploy-status "$STATUS" --deploy-stamp "$STAMP" "$@"
    if [ -f "$PIDFILE" ] && pgrep -F "$PIDFILE" >/dev/null 2>&1; then
        [ "$*" = "$(cat "$ARGSFILE" 2>/dev/null)" ] && return 0
        log "metrics loop arguments changed: restarting it"     # e.g. this script was updated
        pkill -F "$PIDFILE"; sleep 1
    fi
    echo "$*" > "$ARGSFILE"
    daemon -f -r -R 5 -P "$PIDFILE" -o "$LOG" "$PY" "$D/metrics_agent.py" "$@" \
        || { log "could not start metrics loop"; return 1; }
    log "started metrics loop (every ${LOOP_SECONDS}s)"
}

# Full run (update + SMART) due? Missing stamp, or older than FULL_EVERY_MIN minutes.
due=1
if [ -f "$STAMP" ] && [ -n "$(find "$STAMP" -mmin -"$((FULL_EVERY_MIN - 1))" 2>/dev/null)" ]; then due=0; fi

[ "$due" = 1 ] && { update || log "update FAILED (running the current copy)"; }
rc=0
ensure_metrics "$@" || rc=1
if [ "$due" = 1 ]; then
    "$PY" "$D/smart_agent.py" "$@" || rc=$?
    touch "$STAMP"
fi
exit "$rc"
