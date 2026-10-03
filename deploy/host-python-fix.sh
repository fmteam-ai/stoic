#!/bin/bash
# Host python fix — restore the stock /usr/bin/python3 that cPanel/WHM needs.
#
# cPanel's package manager (/usr/local/cpanel/bin/packman*, shebang #!/usr/bin/python3)
# imports the system `dnf`/`yum` python modules, which only exist in the platform
# interpreter (RHEL/Alma 8: python3.6 · 9: python3.9). Earlier releases of
# deploy/bootstrap.sh ran `alternatives --set python3 /usr/bin/python3.11` to give the
# deploy scripts a modern interpreter — that repointed /usr/bin/python3 and WHM failed:
#   “/usr/bin/python3” reported error code “1” … ModuleNotFoundError: No module named 'dnf'
#
# This script puts /usr/bin/python3 back to the platform interpreter and links the
# modern one into a PRIVATE dir (/usr/local/lib/stoic/bin) that deploy/lib.sh and the
# standalone deploy scripts prepend to PATH. Nothing else on the host sees it.
#
#   deploy/host-python-fix.sh            # report + plan (dry run, changes nothing)
#   deploy/host-python-fix.sh --apply    # restore /usr/bin/python3, link private python3, verify packman
set -u
APPLY=0
for a in "$@"; do case "$a" in --apply) APPLY=1 ;; esac; done
[ "$(id -u)" = 0 ] || { echo "run as root"; exit 1; }
command -v dnf >/dev/null || { echo "not a dnf host — /usr/bin/python3 was never changed; nothing to do"; exit 0; }

STOIC_BIN=/usr/local/lib/stoic/bin
has_dnf() { "$1" -c 'import dnf' >/dev/null 2>&1; }
CUR=$(readlink -f /usr/bin/python3 2>/dev/null || echo none)
PLAT=""
for c in /usr/libexec/platform-python /usr/bin/python3.6 /usr/bin/python3.9 /usr/bin/python3.12; do
  [ -x "$c" ] && has_dnf "$c" && { PLAT=$c; break; }
done
MODERN=$(command -v python3.11 2>/dev/null || command -v python3.12 2>/dev/null || command -v python3.9 2>/dev/null || true)
ALTS=$(alternatives --display python3 2>/dev/null | awk '/priority/ {print $1}')

echo "== /usr/bin/python3 -> ${CUR} ($(/usr/bin/python3 --version 2>&1)) · dnf module: $(has_dnf /usr/bin/python3 && echo ok || echo MISSING)"
echo "== platform python (has dnf): ${PLAT:-not found}"
echo "== modern python for deploy scripts: ${MODERN:-none installed}"
[ -n "${ALTS}" ] && echo "== alternatives python3 group: $(printf '%s ' ${ALTS}) · mode: $(alternatives --display python3 2>/dev/null | sed -n 's/.*status is \([a-z]*\).*/\1/p')"
[ -L /usr/local/bin/python3 ] && echo "== /usr/local/bin/python3 -> $(readlink /usr/local/bin/python3) (shadows the system python on PATH)"
[ -e "${STOIC_BIN}/python3" ] && echo "== ${STOIC_BIN}/python3 -> $(readlink -f "${STOIC_BIN}/python3")"

# Audit H4: the plan is an ARRAY of typed actions (verb + fixed operands) executed by
# a case statement — no `eval`, so a hostile path or alternatives entry can't inject.
PLAN=(); plan() { PLAN+=("$*"); }
if ! has_dnf /usr/bin/python3; then
  KEEP=""; DROP=""
  for p in ${ALTS}; do if [ -x "$p" ] && has_dnf "$p"; then KEEP="${KEEP} $p"; else DROP="${DROP} $p"; fi; done
  if [ -n "${KEEP}" ]; then
    for p in ${DROP}; do plan alt-remove "${p}"; done
    plan alt-auto
  elif [ -n "${ALTS}" ] && [ -n "${PLAT}" ]; then
    plan alt-install "${PLAT}"; plan alt-auto
  elif [ -n "${PLAT}" ]; then
    plan link-system "${PLAT}"
  else
    echo "!! no interpreter with the dnf module found — reinstall it: dnf reinstall python3 python3-dnf"; exit 2
  fi
fi
if [ -L /usr/local/bin/python3 ] && [ -n "${MODERN}" ] && [ "$(readlink -f /usr/local/bin/python3)" = "$(readlink -f "${MODERN}")" ]; then
  plan rm-shadow
fi
if [ -n "${MODERN}" ] && [ "$(readlink -f "${STOIC_BIN}/python3" 2>/dev/null)" != "$(readlink -f "${MODERN}")" ]; then
  plan link-private "${MODERN}"
fi
[ -z "${MODERN}" ] && echo "!! no python >= 3.9 for the deploy scripts — install one: dnf install python3.11 (then re-run this script)"

describe() {   # action -> the exact command it will run (shown in the plan, executed by run_action)
  case "$1" in
    alt-remove)   echo "alternatives --remove python3 $2" ;;
    alt-auto)     echo "alternatives --auto python3" ;;
    alt-install)  echo "alternatives --install /usr/bin/python3 python3 $2 1000000" ;;
    link-system)  echo "ln -sfn $2 /usr/bin/python3" ;;
    rm-shadow)    echo "rm -f /usr/local/bin/python3" ;;
    link-private) echo "mkdir -p ${STOIC_BIN} && ln -sfn $2 ${STOIC_BIN}/python3" ;;
    *) echo "?? $*" ;;
  esac
}
run_action() {
  case "$1" in
    alt-remove)   alternatives --remove python3 "$2" ;;
    alt-auto)     alternatives --auto python3 ;;
    alt-install)  alternatives --install /usr/bin/python3 python3 "$2" 1000000 ;;
    link-system)  ln -sfn "$2" /usr/bin/python3 ;;
    rm-shadow)    rm -f /usr/local/bin/python3 ;;
    link-private) mkdir -p "${STOIC_BIN}" && ln -sfn "$2" "${STOIC_BIN}/python3" ;;
    *) echo "!! unknown action: $*" >&2; return 1 ;;
  esac
}
if [ "${#PLAN[@]}" -eq 0 ]; then echo "== nothing to change — system python intact, private python linked"; exit 0; fi
echo "== plan:"; for a in "${PLAN[@]}"; do echo "   $(describe ${a})"; done
if [ "${APPLY}" != 1 ]; then echo "== dry run — re-run with --apply"; exit 0; fi

for a in "${PLAN[@]}"; do set -- ${a}; echo "-- $(describe "$@")"; run_action "$@" || echo "!! failed: $(describe "$@")"; done
hash -r
echo "== /usr/bin/python3 -> $(readlink -f /usr/bin/python3) ($(/usr/bin/python3 --version 2>&1)) · dnf module: $(has_dnf /usr/bin/python3 && echo ok || echo STILL MISSING)"
[ -e "${STOIC_BIN}/python3" ] && echo "== deploy python: ${STOIC_BIN}/python3 -> $("${STOIC_BIN}/python3" --version 2>&1)"
if [ -x /usr/local/cpanel/bin/packman_get_list_json ]; then
  if /usr/local/cpanel/bin/packman_get_list_json >/dev/null 2>&1; then echo "== cPanel packman_get_list_json: OK"
  else echo "!! cPanel packman_get_list_json still fails — run it by hand to see the traceback"; exit 1; fi
fi
has_dnf /usr/bin/python3
