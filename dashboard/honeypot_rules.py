"""
What did the attacker try to do? Rule-based session analysis for the honeypot screens.

  normalize(cmd)        replaces the parts bots vary between runs (URLs, IPs, keys, base64,
                        random file names) with placeholders, so repeat runs look the same
  fingerprint(cmds)     short hash of the normalized command sequence = the session's "pattern"
  analyze(session)      tags (recon, persistence, ...), a few-sentence summary, the commands no
                        rule recognised, and the payload hosts it tried to download from

Deterministic and offline: no AI, nothing an attacker types can change how it behaves, and every
sentence comes from a rule below. Tune RULES once real data shows what's being missed (the
patterns screen lists unrecognized commands).
"""

import hashlib
import re
from urllib.parse import urlsplit

ARITH_RE = re.compile(r"\$\(\(([^()]*)\)\)")   # $((403*680+1)): honeypot detectors randomize the numbers


def _arith(m):
    return "$((" + re.sub(r"\d+", "<n>", m.group(1)) + "))"


PLACEHOLDERS = [
    (ARITH_RE, _arith),
    (re.compile(r"\b(?:https?|ftp|tftp)://[^\s;|&\"'`)]+", re.I), "<url>"),
    (re.compile(r"\bssh-(?:rsa|ed25519|dss)\s+[A-Za-z0-9+/=]{16,}(?:\s+[^\s\"'>]+)?"), "<ssh-key>"),
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?\b"), "<ip>"),
    (re.compile(r"(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{32,}={0,2}(?![A-Za-z0-9+/])"), "<b64>"),
    (re.compile(r"\b[0-9a-f]{16,}\b", re.I), "<hex>"),
    # random-looking names: 8+ chars mixing at least 2 letters and 2 digits (e.g. ./a8f3k2qz)
    (re.compile(r"(?<![\w<])(?=(?:[a-z]*\d){2})(?=(?:\d*[a-z]){2})[a-z0-9]{8,}(?![\w>])", re.I), "<rand>"),
]
URL_RE = re.compile(r"\b(?:https?|ftp|tftp)://([^\s/;|&\"'`):]+)", re.I)
TFTP_HOST_RE = re.compile(r"\btftp\b[^;|&]*?\s((?:\d{1,3}\.){3}\d{1,3})\b")


URL_FIND = PLACEHOLDERS[1][0]
# Mirai-family bots run `/bin/busybox WORD`: busybox answers "WORD: applet not found", which proves a
# real BusyBox shell echoes output back. The word is chosen per botnet variant, so it's worth showing.
MARKER_RE = re.compile(r"\bbusybox\s+([A-Z][A-Z0-9_]{3,})\b")


def payload_names(commands):
    """File names a session downloads (last part of each URL path): bots rename payloads per run
    (x86_64, arm7, sshd...), so within a session these become <file> too."""
    names = set()
    for c in commands:
        for url in URL_FIND.findall(str(c)):
            name = urlsplit(url).path.rsplit("/", 1)[-1]
            base = name.rsplit(".", 1)[0]
            for n in (name, base):
                if len(n) >= 2 and re.fullmatch(r"[\w.-]+", n):
                    names.add(n)
    return sorted(names, key=len, reverse=True)


def normalize(cmd, names=()):
    s = " ".join(str(cmd).split())
    for rx, rep in PLACEHOLDERS:
        s = rx.sub(rep, s)
    for n in names:
        s = re.sub(r"(?<![\w<])" + re.escape(n) + r"(?![\w>])", "<file>", s)
    return s


def normalize_all(commands):
    names = payload_names(commands)
    return [normalize(c, names) for c in commands]


def fingerprint(commands):
    norm = "\n".join(normalize_all(commands))
    return hashlib.sha256(norm.encode()).hexdigest()[:12]


# tag, regex on the (lower-cased) command, phrase for the summary
RULES = [
    ("recon", r"\buname\b", "kernel"),
    ("recon", r"/proc/cpuinfo|\bnproc\b|\blscpu\b", "CPU"),
    ("recon", r"\bfree\b|/proc/meminfo", "memory"),
    ("recon", r"(^|[;&|]\s*)(w|who|last|whoami|id)(\s|$)", "logged-in users"),
    ("recon", r"\bcrontab -l\b", "scheduled jobs"),
    ("recon", r"/etc/(issue|os-release)|\bhostname\b|\bhostnamectl\b", "OS"),
    ("recon", r"\bdf\b|\bmount\b|\blsblk\b", "disks"),
    ("recon", r"\buptime\b", "uptime"),
    ("recon", r"\bifconfig\b|\bip (a|addr|route)\b", "network"),
    ("recon", r"ls -lh \$\(which|\bfile /bin/|/proc/self/exe", "binary architecture"),
    ("recon", r"\bps (aux|-ef)\b(?!.*(miner|xmr))|\btop\b", "running processes"),
    ("recon", r"\bls -\w*\s+/(home|root)\b", "home directories"),
    ("persistence", r"authorized_keys", "replaced the SSH authorized keys to keep access"),
    ("persistence", r"\bcrontab (?!-l)|/etc/cron", "added a cron job"),
    ("persistence", r"\buseradd\b|\badduser\b", "created a user account"),
    ("persistence", r"(^|[;&|]\s*)(passwd|chpasswd)\b", "changed a password"),
    ("persistence", r"/etc/rc\.local|systemctl enable|\.bashrc", "set something to run at startup"),
    ("cover-tracks", r"history -c|histfile|\.bash_history|unset hist", "cleared the shell history"),
    ("cover-tracks", r"\bchattr\b|\blockr\b", "changed file attributes so they can't be removed"),
    ("cover-tracks", r"rm -rf /var/log|/var/log/\S+\s*>|>\s*/var/log", "wiped logs"),
    ("malware-drop", r"\b(wget|curl|tftp|ftpget|busybox wget)\b", "download"),
    ("malware-drop", r"\|\s*(ba)?sh\b", "pipe-to-shell"),
    ("execution", r"chmod \+x|\bchmod 7\d\d\b", "made a file executable"),
    ("execution", r"(^|[;&|]\s*)\./|\bnohup\b|\bsh <file>|\bperl -e\b|\bpython3? -c\b", "ran a program"),
    ("miner-hunt", r"\bxmrig\b|\bminer\b|\bkinsing\b|\bkdevtmpfsi\b", "looked for and killed competing crypto-miners"),
    ("credential-access", r"/etc/(passwd|shadow)|\.ssh/id_|\.aws/|\.docker/config", "read account and key files"),
    ("iot-probe", r"^(enable|system|shell|sh|linuxshell)$|^ping\s*;\s*sh$",
     "tried to break out of a restricted router console (enable, system, shell)"),
    ("iot-probe", r"/bin/busybox|cat /bin/echo", "probed for a BusyBox IoT shell"),
    ("drop-site", r">\s*/[\w./-]*/\.\w+\s*&&\s*chmod 7\d\d\b",
     "searched for a writable, executable directory to drop malware in"),
    # Honeypot detectors: shell-fidelity tests (random arithmetic, $$, positional parameters, functions,
    # subshells), then /proc/self/stat vs /proc/self/status (Pid/NSpid) for a real process and a container.
    ("honeypot-check", r"\$\(\(\s*\d+\s*[-*+]\s*\d+|\bset -- |\(\w+=\w+;\s*printf|\b\w+\(\) \{ printf|"
     r"\"\$\$\"|\| while IFS= read|>&2; exit \d+",
     "tested whether the shell is real (arithmetic, functions, subshells)"),
    ("honeypot-check", r"/proc/self/stat\b|/proc/self/status|\bnspid\b",
     "checked /proc/self for a real process and a container (Cowrie fails this step)"),
    ("liveness", r"echo -e [\"']\\\\?x|^echo \S+$|^(sh -c ')?printf\b|^echo; echo\b", "checked the shell answers"),
    # shell plumbing: recognized (so it isn't "unrecognized"), but not worth a sentence
    ("housekeeping", r"^(cd|ls|pwd|clear|exit|export \w+=)\b(?!.*\.ssh)|^rm -rf? [\w./<>-]+$|^$", ""),
]
_RULES = [(tag, re.compile(rx, re.I), phrase) for tag, rx, phrase in RULES]
TAG_ORDER = ["honeypot-check", "recon", "credential-access", "iot-probe", "drop-site", "miner-hunt", "malware-drop",
             "execution", "persistence", "cover-tracks", "liveness"]
TAG_LABEL = {"honeypot-check": "Honeypot check", "drop-site": "Drop-site search",
             "recon": "Recon", "credential-access": "Credential grab", "iot-probe": "IoT shell probe",
             "miner-hunt": "Miner hunting", "malware-drop": "Malware drop", "execution": "Execution",
             "persistence": "Persistence", "cover-tracks": "Track covering", "liveness": "Liveness check"}


def classify(cmd):
    """[(tag, phrase)] matched by one command (raw or normalized)."""
    s = " ".join(str(cmd).split())
    return [(tag, phrase) for tag, rx, phrase in _RULES if rx.search(s)]


def payload_hosts(commands, downloads=()):
    hosts = []
    for c in list(commands) + list(downloads):
        for h in URL_RE.findall(str(c)) + TFTP_HOST_RE.findall(str(c)):
            if h not in hosts:
                hosts.append(h)
    return hosts


def label(tags):
    tags = [t for t in TAG_ORDER if t in tags and t not in ("execution", "liveness")] or \
           [t for t in TAG_ORDER if t in tags]
    return " + ".join(TAG_LABEL[t] for t in tags[:3]) if tags else "Unrecognized"


def _join(parts):
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]


def analyze(commands, downloads=(), username=None, password=None):
    """-> {fingerprint, tags, label, summary, unrecognized, hosts, markers}"""
    found = {}
    unrecognized = []
    for c, n in zip(commands, normalize_all(commands)):
        m = classify(c)
        if not m:
            unrecognized.append(n)
        for tag, phrase in m:
            found.setdefault(tag, [])
            if phrase not in found[tag]:
                found[tag].append(phrase)
    tags = [t for t in TAG_ORDER if t in found]
    hosts = payload_hosts(commands, downloads)
    markers = []
    for c in commands:
        for m in MARKER_RE.findall(str(c)):
            if m not in markers:
                markers.append(m)

    steps = []
    if "honeypot-check" in found:
        steps.append(_join(found["honeypot-check"][:2]))
    if "recon" in found:
        steps.append(f"fingerprinted the system ({', '.join(found['recon'][:4])})")
    if "credential-access" in found:
        steps.append(found["credential-access"][0])
    if "iot-probe" in found:
        mark = f" with the botnet marker \u201c{_join(markers[:3])}\u201d" if markers else ""
        probe = [p + mark if p.startswith("probed") else p for p in found["iot-probe"][:2]]
        steps.append(_join(probe))
    if "drop-site" in found:
        steps.append(found["drop-site"][0])
    if "miner-hunt" in found:
        steps.append(found["miner-hunt"][0])
    if "malware-drop" in found:
        where = f" from {_join(hosts[:2])}" if hosts else ""
        run = " and run it" if "execution" in found else ""
        steps.append(f"tried to download a payload{where}{run} (blocked: no outbound access)")
    elif "execution" in found:
        steps.append(found["execution"][0])
    if "persistence" in found:
        steps.append(_join(found["persistence"][:2]))
    if "cover-tracks" in found:
        steps.append(_join(found["cover-tracks"][:2]))

    who = f"Got in as {username} with password \u201c{password}\u201d. " if username is not None else ""
    if steps:
        first, rest = steps[0], steps[1:]
        text = first[0].upper() + first[1:]
        if rest:
            text += ", then " + _join(rest)
        text += "."
    elif commands:
        text = "Ran commands the rules don't recognize yet."
    else:
        text = "Got a shell but typed nothing."
    if steps and unrecognized:
        text += f" Also ran {len(unrecognized)} unrecognized command{'s' if len(unrecognized) != 1 else ''}."
    return {"fingerprint": fingerprint(commands) if commands else None, "tags": tags, "label": label(tags),
            "summary": who + text, "unrecognized": unrecognized, "hosts": hosts,
            "markers": markers}
