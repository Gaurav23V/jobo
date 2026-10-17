#!/bin/bash
# Is a REAL `jobo apply` process running?
#
# `pgrep -f "jobo apply"` is wrong here and gave false positives three separate
# times on 2026-09-11: the agent's own probe shell contains the string "jobo
# apply" in its command line, so the check matches ITSELF and reports a
# finished run as still running. The same class of mistake with `pkill -f` is
# worse - it killed the probing shell instead of the target.
#
# Two consequences worth avoiding:
#   - monitor_apply.sh would never reach its "run finished" exit
#   - apply_status.sh reports running=yes forever, hiding a crashed run
#
# So walk /proc and accept only a command line that STARTS with the real
# invocation, and never this script's own process tree.
_apply_alive_list() {
  # One "pid<TAB>cmdline" per line. /proc on Linux (unchanged behaviour);
  # macOS has no /proc, so fall back to ps there.
  local pid
  if [ -d /proc/self ]; then
    for pid in /proc/[0-9]*; do
      pid=${pid#/proc/}
      printf '%s\t%s\n' "$pid" "$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null)"
    done
  else
    ps -axo pid=,args= | sed -E 's/^ *([0-9]+) +/\1\t/'
  fi
}

apply_alive() {
  local pid cmd self_tree
  self_tree=" $$ $PPID "
  while IFS=$'\t' read -r pid cmd; do
    case "$self_tree" in *" $pid "*) continue ;; esac
    [ -z "$cmd" ] && continue
    # A genuine invocation ends with `jobo apply [args]` - it is the COMMAND,
    # not a string sitting inside some other shell's -c payload. So require the
    # command line to START with a launcher and contain no shell -c marker and
    # no probe tooling.
    case "$cmd" in
      *pgrep*|*" grep "*|*apply_status.sh*|*monitor_apply.sh*) continue ;;
      *" -c "*|*" -lic "*|*" -lc "*) continue ;;
    esac
    case "$cmd" in
      *"jobo apply"*) return 0 ;;
    esac
  done < <(_apply_alive_list)
  return 1
}
