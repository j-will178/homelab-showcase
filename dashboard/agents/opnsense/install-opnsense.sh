#!/bin/sh
# One-time setup of the self-updating SMART agent on OPNsense. Run as root:
#   sh install-opnsense.sh http://optiplex-management.home.arpa:8080/api/smart
# Safe to re-run. Needs the agent token at /usr/local/rack-agent/token (see README).
set -e
URL=${1:?usage: sh install-opnsense.sh DASHBOARD_URL}
REPO_URL=${REPO_URL:-git@github.com:j-will178/homelab.git}
D=/usr/local/rack-agent
R=$D/repo
ACTION=/usr/local/opnsense/service/conf/actions.d/actions_rackagent.conf

[ "$(id -u)" = 0 ] || { echo "run as root"; exit 1; }
[ -s "$D/token" ] || { echo "missing $D/token: copy the agent token first (see README)"; exit 1; }
command -v git >/dev/null || pkg install -y git
mkdir -p "$D" && chmod 700 "$D"

if [ ! -f "$D/deploy_key" ]; then
    ssh-keygen -q -t ed25519 -N "" -C "rack-agent@opnsense" -f "$D/deploy_key"
fi
SSH_CMD="ssh -i $D/deploy_key -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=$D/known_hosts"

if [ ! -d "$R/.git" ]; then
    echo
    echo "Add this public key on GitHub: repo > Settings > Deploy keys > Add deploy key"
    echo "  Title: rack-agent-opnsense    Allow write access: leave UNCHECKED"
    echo
    cat "$D/deploy_key.pub"
    echo
    printf 'Press Enter once the key is added... '; read -r _
    git -c core.sshCommand="$SSH_CMD" clone -q --depth 1 --filter=blob:none --sparse "$REPO_URL" "$R"
fi
# Save the key setting BEFORE sparse-checkout: with a partial clone, sparse-checkout
# fetches the files over SSH, and without this it would use root's default (no) key.
git -C "$R" config core.sshCommand "$SSH_CMD"
git -C "$R" sparse-checkout set dashboard/agents

# Bootstrap the installed copies from the checkout, then hand over to run.sh.
cp "$R/dashboard/agents/smart_agent.py" "$D/" && cp "$R/dashboard/agents/opnsense/run.sh" "$D/"
chmod 700 "$D/smart_agent.py" "$D/run.sh"
sed "s#DASHBOARD_URL#$URL#" "$R/dashboard/agents/opnsense/actions_rackagent.conf" > "$ACTION"
service configd restart
echo "Testing (configctl rackagent smart-push):"
configctl rackagent smart-push
echo
echo "Done. Keep the GUI cron job (System > Settings > Cron, */15, 'Push SMART report to rack dashboard')."
