#!/usr/bin/env bash
# Private deploy python (>= 3.9) lives in /usr/local/lib/stoic/bin (bootstrap.sh) — never
# /usr/bin/python3 (cPanel packman needs the stock interpreter's dnf module).
# Audit H2: prepend it to PATH ONLY when the dir is root-owned and not group/world-writable;
# a writable PATH entry in front of root's PATH is a privilege escalation.
# Source from any deploy script:  . "$(dirname "$0")/stoic-path.sh"
_stoic_bin=/usr/local/lib/stoic/bin
if [ -d "${_stoic_bin}" ]; then
  # dir itself AND its parents must be real root-owned dirs nobody else can write to
  # (a symlinked or swappable component would let a local user plant a "python3").
  _stoic_bin_ok=1; _stoic_p="${_stoic_bin}"
  while [ -n "${_stoic_p}" ] && [ "${_stoic_p}" != "/" ]; do
    if [ -h "${_stoic_p}" ]; then _stoic_bin_ok=0; _stoic_bin_why="${_stoic_p} is a symlink"; break; fi
    _stoic_bin_stat=$(stat -c '%u %a' "${_stoic_p}" 2>/dev/null || echo "? ?")
    case "${_stoic_bin_stat}" in "0 "[0-7][0-5][0-5]) ;; *) _stoic_bin_ok=0; _stoic_bin_why="${_stoic_p} uid+mode ${_stoic_bin_stat}"; break ;; esac
    _stoic_p=$(dirname "${_stoic_p}")
  done
  if [ "${_stoic_bin_ok}" = 1 ]; then
    case ":${PATH}:" in *":${_stoic_bin}:"*) ;; *) export PATH="${_stoic_bin}:${PATH}" ;; esac
  else
    echo "!! ${_stoic_bin} is not a root-owned, non-writable path (${_stoic_bin_why}) — NOT adding to PATH; fix: chown root:root <dir> && chmod 755 <dir> for every component" >&2
  fi
fi
unset _stoic_bin _stoic_bin_stat _stoic_bin_ok _stoic_bin_why _stoic_p
