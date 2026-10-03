#!/usr/bin/env bash
# Private deploy python (>= 3.9) lives in /usr/local/lib/stoic/bin (bootstrap.sh) — never
# /usr/bin/python3 (cPanel packman needs the stock interpreter's dnf module).
# Audit H2: prepend it to PATH ONLY when the dir is root-owned and not group/world-writable;
# a writable PATH entry in front of root's PATH is a privilege escalation.
# Source from any deploy script:  . "$(dirname "$0")/stoic-path.sh"
_stoic_bin=/usr/local/lib/stoic/bin
if [ -d "${_stoic_bin}" ]; then
  _stoic_bin_stat=$(stat -c '%u %a' "${_stoic_bin}" 2>/dev/null || echo "? ?")
  case "${_stoic_bin_stat}" in
    "0 "7[0-5][0-5]) case ":${PATH}:" in *":${_stoic_bin}:"*) ;; *) export PATH="${_stoic_bin}:${PATH}" ;; esac ;;
    *) echo "!! ${_stoic_bin} is not root-owned / is group-or-world-writable (uid+mode: ${_stoic_bin_stat}) — NOT adding to PATH; fix: chown root:root ${_stoic_bin} && chmod 755 ${_stoic_bin}" >&2 ;;
  esac
fi
unset _stoic_bin _stoic_bin_stat
