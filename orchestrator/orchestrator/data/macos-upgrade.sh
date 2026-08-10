#!/bin/bash
#
# simplemdm-macos-upgrade.sh — in-place macOS upgrade for DEP-enrolled M4 minis.
#
# SOURCE OF TRUTH for **SimpleMDM script 8903**. Edit here, then paste into
# SimpleMDM — never the other way round. (Keeping the bootstrap script only in
# the SimpleMDM UI is what produced the #1260 divergence: the wrong body sat in
# script 14716 and stranded every EACS at "Waiting for /var/root/vault.yaml".)
#
# Downloads the InstallAssistant pkg from the MDC1 mirror, installs it, then
# arms a one-shot LaunchDaemon that runs `startosinstall` on next boot.
#
# ---------------------------------------------------------------------------
# Before pasting into SimpleMDM: replace INSERT_HERE with the fixed DEP admin
# password (op://RelOps/DEP Provisioned Mac Admin Account SimpleMDM SSH).
# It is NEVER committed here.
# ---------------------------------------------------------------------------
#
# Changes vs. the original, and why each one matters at 55-host scale:
#
#  1. The credential no longer survives the upgrade. The original wrote the
#     admin password into /Library/Scripts/run_upgrade.sh via a heredoc (mode
#     644 from root's umask, then chmod +x -> 0755, i.e. WORLD-READABLE) and
#     cleaned up with an `rm` at the bottom that CAN NEVER RUN: on success
#     startosinstall reboots mid-execution, and on failure the else-branch
#     exits 1 first. Both paths leave the fleet-wide admin password in cleartext
#     on a prod worker, plus a LaunchDaemon that re-fires every boot forever.
#     Now: the password lives in a 0600 root-only file, and the trigger's FIRST
#     act on next boot is to read it and delete the credential, the plist and
#     itself -- before doing anything slow or reboot-y.
#
#  2. curl can survive a saturated mirror. `releng-pxe1` has been served by
#     `python3 -m http.server`, which is single-threaded -- it handles one
#     request at a time and queues the rest. Against a ~14GB installer and any
#     real fan-out that means timeouts, and the original's `--retry 3
#     --retry-delay 5` gave up after ~15s with no resume, so a failure at 90%
#     of 14GB restarted from zero. Now: `-C -` resume, far more patient retry,
#     and a stall detector so a wedged transfer fails instead of hanging.
#
#  3. Preconditions are checked before the download, not discovered after it.
#     Free space, and admin's SecureToken -- on Apple Silicon `startosinstall`
#     needs volume-owner credentials, and a DEP host has none until an
#     interactive login (`reprovision mint <host>` grants it). Without this the
#     wave burns hours of transfer and then fails at the last step.
#
#  4. Idempotent. Re-running against a host already at the target is a no-op,
#     so a batch can be safely re-fired at stragglers.
#
#  5. Logs to a file. SimpleMDM's job-status API is unreliable here (it reports
#     `pending` after a script has actually run), so the on-box log is ground
#     truth -- same reasoning as the bootstrap sentinel.

set -u

TARGET_VERSION="15.3"
REMOTE_URL="http://releng-pxe1.test.releng.mdc1.mozilla.com/InstallAssistant-15.3.pkg.zip"
APP_PATH="/Applications/Install macOS Sequoia.app"

ZIP_PATH="/private/tmp/InstallAssistant-15.3.pkg.zip"
PKG_PATH="/private/tmp/InstallAssistant.pkg"
UPGRADE_TRIGGER="/Library/Scripts/run_upgrade.sh"
CRED_FILE="/private/var/root/.macos-upgrade-cred"
LAUNCHD_PLIST="/Library/LaunchDaemons/com.mozilla.upgrade.plist"
LOG=/var/log/macos-upgrade.log

ADMIN_USERNAME="admin"
ADMIN_PASSWORD="INSERT_HERE"

# Need ~14GB zip + ~14GB pkg + the expanded installer app.
REQUIRED_GB=45

exec >> "$LOG" 2>&1
echo "=== macos-upgrade start $(date) (target ${TARGET_VERSION}) ==="

if [[ $EUID -ne 0 ]]; then
  echo "[ERROR] must run as root."
  exit 1
fi

# The placeholder must never reach a real run. Script 8903 sat in SimpleMDM with
# ADMIN_PASSWORD="INSERT_HERE" for months: the heredoc below is unquoted, so the
# literal string expanded into run_upgrade.sh and every host would have
# downloaded ~14GB, installed the pkg, rebooted, and only THEN failed
# authenticating startosinstall -- while the unreachable cleanup left a
# LaunchDaemon re-firing that failure on every subsequent boot. Cost per host:
# 14GB and an hour, to learn something knowable in a millisecond.
if [[ "$ADMIN_PASSWORD" == "INSERT_HERE" || -z "$ADMIN_PASSWORD" ]]; then
  echo "[ERROR] ADMIN_PASSWORD is still the placeholder — refusing to run."
  echo "        Replace INSERT_HERE with the fixed DEP admin password before firing this,"
  echo "        or drive it via \`reprovision batch --action os-update\`, which substitutes"
  echo "        the credential from the vault at run time and never stores it in SimpleMDM."
  exit 1
fi

#-----------------------------------------------------------------------------
# 0. Preconditions — all cheap, all before the ~14GB download
#-----------------------------------------------------------------------------
CURRENT=$(/usr/bin/sw_vers -productVersion)
if [[ "$CURRENT" == "$TARGET_VERSION" || "$CURRENT" == "$TARGET_VERSION."* ]]; then
  echo "[INFO] already on macOS $CURRENT — nothing to do."
  exit 0
fi
echo "[INFO] macOS $CURRENT -> $TARGET_VERSION"

# Apple Silicon: startosinstall authenticates as a volume owner. A DEP host
# skips Setup Assistant, so admin holds no SecureToken until an interactive
# login. Fail here, in seconds, rather than after the transfer.
if ! /usr/sbin/sysadminctl -secureTokenStatus "$ADMIN_USERNAME" 2>&1 | /usr/bin/grep -q ENABLED; then
  echo "[ERROR] $ADMIN_USERNAME holds no SecureToken, so it is not a volume owner and"
  echo "        startosinstall will refuse. Run \`reprovision mint <host>\` first."
  exit 1
fi

FREE_GB=$(/bin/df -g / | /usr/bin/awk 'NR==2 {print $4}')
if [[ -n "$FREE_GB" && "$FREE_GB" -lt "$REQUIRED_GB" ]]; then
  echo "[ERROR] only ${FREE_GB}GB free on /, need ~${REQUIRED_GB}GB."
  exit 1
fi

/bin/mkdir -p /Library/Scripts

#-----------------------------------------------------------------------------
# 1. Download — assume the mirror is contended
#-----------------------------------------------------------------------------
# -C -                  resume a partial file instead of restarting 14GB
# --retry-all-errors    retry transient HTTP errors, not just connection ones
# --retry-connrefused   a single-threaded server refusing a connection IS the
#                       normal contended case here, and must be retried
# --speed-limit/-time   fail a transfer stalled under 10KB/s for 5 min rather
#                       than hanging on a half-open socket indefinitely
echo "[INFO] downloading $REMOTE_URL"
if ! /usr/bin/curl -L --fail -C - \
      --retry 10 --retry-delay 30 --retry-max-time 7200 \
      --retry-all-errors --retry-connrefused \
      --speed-limit 10240 --speed-time 300 \
      -o "$ZIP_PATH" "$REMOTE_URL"; then
  echo "[ERROR] download failed after retries — mirror saturated or unreachable."
  echo "        Partial file kept at $ZIP_PATH; a re-run resumes from there."
  exit 1
fi

echo "[INFO] unzipping"
if ! /usr/bin/unzip -o "$ZIP_PATH" -d /private/tmp/ || [[ ! -f "$PKG_PATH" ]]; then
  echo "[ERROR] unzip failed or $PKG_PATH missing — treating the download as corrupt."
  /bin/rm -f "$ZIP_PATH" "$PKG_PATH"   # force a clean re-fetch next run
  exit 1
fi

echo "[INFO] installing pkg"
if ! /usr/sbin/installer -pkg "$PKG_PATH" -target /; then
  echo "[ERROR] installer failed."
  exit 1
fi

if [[ ! -x "$APP_PATH/Contents/Resources/startosinstall" ]]; then
  echo "[ERROR] $APP_PATH/Contents/Resources/startosinstall missing after install."
  exit 1
fi

/bin/rm -f "$ZIP_PATH" "$PKG_PATH"

#-----------------------------------------------------------------------------
# 2. Stage the credential — root-only, and short-lived by construction
#-----------------------------------------------------------------------------
# Create with 0600 BEFORE any content lands, so the password is never briefly
# readable at the umask default. The trigger deletes this on next boot.
/usr/bin/install -m 0600 -o root -g wheel /dev/null "$CRED_FILE"
/bin/cat > "$CRED_FILE" <<CRED
$ADMIN_PASSWORD
CRED

#-----------------------------------------------------------------------------
# 3. Arm the one-shot upgrade trigger
#-----------------------------------------------------------------------------
/usr/bin/install -m 0700 -o root -g wheel /dev/null "$UPGRADE_TRIGGER"
/bin/cat > "$UPGRADE_TRIGGER" <<TRIGGER
#!/bin/bash
# One-shot: runs at next boot, upgrades, and leaves nothing behind.
set -u
exec >> $LOG 2>&1
echo "=== upgrade trigger \$(date) ==="

STARTOSINSTALL="$APP_PATH/Contents/Resources/startosinstall"

# Disarm FIRST. startosinstall reboots mid-execution on success and we exit
# early on failure, so anything cleaned up *after* it never gets cleaned up at
# all -- which is how the previous version left the fleet admin password on
# disk permanently and re-fired this daemon on every subsequent boot.
PASSWORD=\$(/bin/cat "$CRED_FILE" 2>/dev/null)
/bin/rm -f "$CRED_FILE" "$LAUNCHD_PLIST"
/bin/launchctl bootout system/com.mozilla.upgrade 2>/dev/null || true
/bin/rm -f "\$0"

if [[ -z "\$PASSWORD" ]]; then
  echo "[ERROR] no credential staged — cannot authenticate startosinstall."
  exit 1
fi
if [[ ! -x "\$STARTOSINSTALL" ]]; then
  echo "[ERROR] startosinstall not found at \$STARTOSINSTALL."
  exit 1
fi

echo "[INFO] starting macOS upgrade"
echo "\$PASSWORD" | "\$STARTOSINSTALL" \\
  --agreetolicense \\
  --nointeraction \\
  --forcequitapps \\
  --user "$ADMIN_USERNAME" \\
  --stdinpass
TRIGGER

/usr/bin/install -m 0644 -o root -g wheel /dev/null "$LAUNCHD_PLIST"
/bin/cat > "$LAUNCHD_PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
 "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.mozilla.upgrade</string>
  <key>ProgramArguments</key>
  <array>
    <string>$UPGRADE_TRIGGER</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>StandardOutPath</key>
  <string>/var/log/macos-upgrade-trigger.out</string>
  <key>StandardErrorPath</key>
  <string>/var/log/macos-upgrade-trigger.err</string>
</dict>
</plist>
PLIST

echo "[INFO] armed — rebooting in 10s; the upgrade runs on next boot."
echo "[INFO] watch: $LOG"
/bin/sleep 10
/sbin/reboot
