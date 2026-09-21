#!/bin/bash
#
# screencapture-approve.sh — grant Screen Recording to the Taskcluster worker
# binaries on a SIP-enabled macOS host. Bug 2073303.
#
# Staged to the host by `reprovision screencapture-grant` (and by the provision /
# reprovision flows) with ADMIN_PASSWORD substituted from the vault at fire time,
# then run as root. Not a SimpleMDM script: the credential must not sit in the MDM
# UI, and we want a real exit code.
#
# WHY THIS EXISTS
#
# kTCCServiceScreenCapture is a system-scoped TCC service, read only from
# /Library/Application Support/com.apple.TCC/TCC.db, which SIP protects. ronin's
# macos_tcc_perms writes that database directly, which works only while SIP is off.
# On a SIP-on host the write silently fails and the fallback it takes -- writing
# the grant into cltbld's USER database -- is inert, because TCC never reads this
# service from a user database. The host then fails every getDisplayMedia() call
# with SCStreamErrorUserDeclined (-3801) for its entire life, showing up only as an
# intermittent orange (bug 1937556: 499 failures in 30 days).
#
# MDM cannot supply the grant. Apple permits only
# AllowStandardUserToSetSystemService for this service in a PPPC payload, which
# authorises a standard user to approve it rather than approving it -- and an
# approval made while such a profile is installed is recorded as MDM-managed
# (flags 12) and is then ignored by TCC. There is no consent dialog to automate
# either; tccd logs "Service kTCCServiceScreenCapture does not allow prompting;
# returning denied". The Screen Recording pane of System Settings is the only route.
#
# Three preconditions, all enforced below, all of which were learned the hard way:
#   1. SIP on               -- SIP-off hosts are already handled by macos_tcc_perms
#   2. Developer-ID-signed worker binaries -- an ad-hoc binary is Identifier=a.out
#                              with no TeamIdentifier and can never satisfy a code
#                              requirement, so the grant lands and does nothing
#   3. No ScreenCapture PPPC override -- see above; it poisons the row to flags 12
#   4. Host idle            -- a running test owns the GUI session: the click never
#                              lands AND opening System Settings can corrupt that
#                              test. 16 of 18 failures on the first fleet pass were
#                              mid-mochitest.
#
# Result on success: auth_value 2, auth_reason 4, flags 0 -- an ordinary
# user-approved row, honoured by TCC and durable across reboots.
#
# EACS re-enables SIP and wipes TCC, so this has to run on every reprovision.

set -u

ADMIN_USER="INSERT_USER_HERE"
ADMIN_PASSWORD="INSERT_HERE"

TCC_DB="/Library/Application Support/com.apple.TCC/TCC.db"
OVERRIDES="/Library/Application Support/com.apple.TCC/MDMOverrides.plist"
SESSION_USER="cltbld"
CLIENTS=(/usr/local/bin/generic-worker-multiuser /usr/local/bin/start-worker)

log()  { echo "[screencapture] $*"; }
fail() { echo "[ERROR] $*" >&2; exit 1; }
skip() { echo "[SKIP] $*"; exit 3; }

[ "$ADMIN_PASSWORD" = "INSERT_HERE" ] && fail "credential placeholder was not substituted"

row() {
    /usr/bin/sqlite3 -cmd ".timeout 5000" "$TCC_DB" \
        "SELECT auth_value || '/' || flags FROM access
          WHERE service = 'kTCCServiceScreenCapture' AND client = '$1';" 2>/dev/null
}

granted() {
    for c in "${CLIENTS[@]}"; do
        case "$(row "$c")" in
            2/0|2/4) ;;
            *) return 1 ;;
        esac
    done
    return 0
}

# --- preconditions -----------------------------------------------------------

/usr/bin/csrutil status 2>/dev/null | grep -qi disabled && \
    skip "SIP is off — macos_tcc_perms already grants this host"

if granted; then
    log "already granted ($(row "${CLIENTS[0]}"))"
    exit 0
fi

ident=$(/usr/bin/codesign -dvvv "${CLIENTS[0]}" 2>&1 | /usr/bin/awk -F= '/^Identifier=/{print $2; exit}')
[ "$ident" = "generic-worker-multiuser-darwin-arm64" ] || \
    fail "worker binary is not Developer-ID signed (Identifier=${ident:-unknown}); the grant would be stored and ignored"

overrides=$(/usr/bin/plutil -p "$OVERRIDES" 2>/dev/null | /usr/bin/grep -c kTCCServiceScreenCapture)
[ "$overrides" = "0" ] || \
    fail "a ScreenCapture PPPC override is installed ($overrides entries); approving now yields a flags=12 row that TCC ignores"

/usr/bin/pgrep -f '/opt/worker/tasks/' >/dev/null 2>&1 && \
    skip "host is running a task — retry when idle (driving System Settings mid-test can corrupt it)"

uid=$(/usr/bin/id -u "$SESSION_USER" 2>/dev/null) || fail "no $SESSION_USER user"
[ "$(/usr/bin/stat -f%Su /dev/console)" = "$SESSION_USER" ] || \
    skip "$SESSION_USER does not own the console session yet"

# --- approve -----------------------------------------------------------------

asuser() { /bin/launchctl asuser "$uid" /usr/bin/sudo -u "$SESSION_USER" "$@"; }

# Credential handoff: 0600, owned by the session user, read once and removed by the
# AppleScript itself. Never an argv (invisible to ps) and never in the environment.
creds=$(/usr/bin/sudo -u "$SESSION_USER" /usr/bin/mktemp "/Users/${SESSION_USER}/.sc-creds.XXXXXX") \
    || fail "could not create credential file"
trap '/bin/rm -f "$creds"' EXIT
/usr/bin/printf '%s\n%s\n' "$ADMIN_USER" "$ADMIN_PASSWORD" > "$creds"
/usr/sbin/chown "$SESSION_USER" "$creds"; /bin/chmod 600 "$creds"

asuser /usr/bin/osascript -e 'tell application "System Settings" to quit' >/dev/null 2>&1
sleep 3; /usr/bin/pkill -x "System Settings" >/dev/null 2>&1; sleep 2
asuser /usr/bin/open "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture"
sleep 12

asuser /usr/bin/osascript - "$creds" 2>&1 <<'OSA'
on run argv
  set credFile to item 1 of argv
  set adminUser to do shell script "head -1 " & quoted form of credFile
  set adminPass to do shell script "sed -n 2p " & quoted form of credFile
  do shell script "rm -f " & quoted form of credFile

  tell application "System Events" to tell process "System Settings"
    set frontmost to true
    delay 2
    repeat with nm in {"generic-worker-multiuser", "start-worker"}
      set cb to my findCB(window 1, nm as string, 0)
      if cb is missing value then error "checkbox not found: " & (nm as string)
      if value of cb is 0 then
        click cb
        delay 3
        -- system.preferences.security requires an admin, so a sheet appears.
        try
          if (count of sheets of window 1) > 0 then
            tell sheet 1 of window 1
              try
                set value of (first text field whose subrole is not "AXSecureTextField") to adminUser
              end try
              set pw to (first text field whose subrole is "AXSecureTextField")
              set focused of pw to true
              set value of pw to adminPass
              delay 1
              keystroke return
            end tell
            delay 6
          end if
        end try
        delay 2
      end if
    end repeat
  end tell
  return "done"
end run

-- Deliberately NOT `entire contents of window 1`: on macOS 15.3 that returns an
-- empty list against this pane even when it is loaded. And the left-hand category
-- sidebar is ALSO an outline, reached first by a depth-first search, so we search
-- for the checkbox by name rather than locating "the outline".
on findCB(el, nm, depth)
  if depth > 14 then return missing value
  tell application "System Events"
    set kids to {}
    try
      set kids to UI elements of el
    on error
      return missing value
    end try
    repeat with k in kids
      try
        if class of k is checkbox and name of k is nm then return k
      end try
      set f to my findCB(k, nm, depth + 1)
      if f is not missing value then return f
    end repeat
  end tell
  return missing value
end findCB
OSA

sleep 4
asuser /usr/bin/osascript -e 'tell application "System Settings" to quit' >/dev/null 2>&1
sleep 3; /usr/bin/pkill -x "System Settings" >/dev/null 2>&1

# --- verify ------------------------------------------------------------------

for c in "${CLIENTS[@]}"; do
    r=$(row "$c")
    case "$r" in
        2/0|2/4) log "granted $c ($r)" ;;
        2/12)    fail "$c landed flags=12 (MDM-managed, TCC ignores it) — was the override really gone?" ;;
        *)       fail "$c not granted (got ${r:-none})" ;;
    esac
done
log "Screen Recording granted"
exit 0
