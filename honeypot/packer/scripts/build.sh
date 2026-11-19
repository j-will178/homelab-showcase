#!/bin/bash
# Runs on Packer's short-lived build droplet (it has internet access; the honeypot never will).
# Everything the honeypot needs must be installed here: once deployed it can't download anything.
set -euxo pipefail
export DEBIAN_FRONTEND=noninteractive
F=/tmp/files

# ---- patched base OS (includes trixie-security) ----
# A fresh droplet is still booting when Packer connects: cloud-init and the apt timers hold the
# package lock for a minute or two (apt-get then fails with exit 100). Wait for both.
cloud-init status --wait >/dev/null || true
systemctl stop apt-daily.timer apt-daily-upgrade.timer 2>/dev/null || true
# Whatever is already running (an apt-daily run, an agent installer) finishes first; it's not
# killed, which could leave dpkg half-done. Up to 10 minutes.
for _ in $(seq 120); do
  # Not by name for unattended-upgrades: its always-running helper unattended-upgrade-shutdown
  # shows up with the same 15-character name, "unattended-upgr". Match the real run's command line.
  pgrep -x 'apt|apt-get|dpkg' >/dev/null || pgrep -f '^/usr/bin/python3 /usr/bin/unattended-upgrade( |$)' >/dev/null || break
  sleep 5
done
apt() { apt-get -o DPkg::Lock::Timeout=600 -o Acquire::Retries=3 "$@"; }
apt update
apt -y -o Dpkg::Options::=--force-confold full-upgrade
apt -y install --no-install-recommends python3-venv nftables sudo
# DigitalOcean's agents phone home and add a web-console login path; neither is wanted here.
apt -y purge droplet-agent do-agent || true
apt -y autoremove --purge
apt clean

# ---- accounts ----
# cowrie: runs the honeypot, no login. collector: log reading only (ForceCommand), needs a shell
# for sshd to run the forced command. hpadmin: admin access through the bastion.
useradd --system --home-dir /var/lib/cowrie --shell /usr/sbin/nologin cowrie
useradd --create-home --shell /bin/sh collector
useradd --create-home --shell /bin/bash --groups sudo hpadmin
passwd -l collector; passwd -l hpadmin
echo 'hpadmin ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/90-hpadmin
chmod 440 /etc/sudoers.d/90-hpadmin
usermod -a -G cowrie collector          # read access to the logs

# ---- Cowrie: exact versions, every file checked against the SHA-256 in requirements.txt ----
python3 -m venv /opt/cowrie
/opt/cowrie/bin/pip install --no-cache-dir --require-hashes --only-binary=:all: -r $F/requirements.txt
# Twisted caches its plugin list inside the venv; the sandboxed service can't write there, so build it now.
/opt/cowrie/bin/python -c "from twisted.plugin import IPlugin, getPlugins; list(getPlugins(IPlugin))"
install -d -o root -g cowrie -m 750 /etc/cowrie
install -o root -g cowrie -m 640 $F/cowrie.cfg $F/userdb.txt /etc/cowrie/
install -d -o cowrie -g cowrie -m 750 /var/lib/cowrie /var/lib/cowrie/log /var/lib/cowrie/state /var/lib/cowrie/downloads
chmod g+s /var/lib/cowrie/log            # new log files stay group cowrie, readable by collector
install -m 644 $F/cowrie.service /etc/systemd/system/cowrie.service
systemctl enable cowrie.service

# ---- log reader, health snapshot, sshd, firewall ----
install -m 755 $F/hp-log-read /usr/local/bin/hp-log-read
install -m 755 $F/hp-health /usr/local/sbin/hp-health
install -m 644 $F/hp-health.service $F/hp-health.timer /etc/systemd/system/
install -d -o root -g collector -m 750 /var/lib/hp-health
systemctl enable hp-health.timer
install -m 644 $F/50-honeypot-sshd.conf /etc/ssh/sshd_config.d/50-honeypot.conf
install -m 644 $F/nftables.conf /etc/nftables.conf
install -d -m 755 /etc/nftables.d
echo '# filled in at first boot by cloud-init with the bastion address' > /etc/nftables.d/bastion.nft
systemctl enable nftables.service
nft -c -f /etc/nftables.conf              # syntax check now, not at first boot
sshd -t -f /etc/ssh/sshd_config -o HostKey=/etc/ssh/ssh_host_ed25519_key 2>&1 || { echo "sshd config invalid"; exit 1; }

# ---- cloud-init: host key and authorized keys come from Terraform at first boot ----
cat > /etc/cloud/cloud.cfg.d/99-honeypot.cfg <<'CFG'
disable_root: true
ssh_pwauth: false
ssh_deletekeys: true
ssh_genkeytypes: []
CFG

# ---- leave nothing of the build behind ----
rm -f /etc/ssh/ssh_host_*
rm -f /root/.ssh/authorized_keys
cloud-init clean --logs --seed
truncate -s 0 /etc/machine-id
rm -rf /tmp/files /var/lib/apt/lists/* /root/.bash_history
journalctl --rotate && journalctl --vacuum-time=1s || true
sync
