#!/bin/bash
# mint-securetoken.sh — automate the first-SecureToken "mint" on a freshly-EACS'd
# Apple-Silicon worker, replacing the manual password-ssh step in the EACS golden path.
#
# WHY THIS EXISTS
#   On this fleet DEP skips Setup Assistant, so no account is a volume owner at
#   enrollment and admin has no SecureToken. The first SecureToken is only minted
#   by an *interactive* (PAM) password login — key-based ssh does NOT trigger it.
#   Proven required by an A/B on m4-81 (2026-07-02): WITH a password-ssh mint the
#   bootstrap finished (script.ran job_status=0); WITHOUT it the box wedged at the
#   BST wait-loop and timed out (job_status=1). This script performs that login
#   unattended so the whole EACS->prod flow becomes hands-off.
#
# WHERE TO RUN
#   From an operator-side host that has corp-network + DNS to the workers (the
#   laptop-side orchestrator). NOT from the Cloud Run provisioner — it can't reach
#   the worker network. Call it right after the device re-enrolls (enrolled_at bumps)
#   and within the bootstrap's 30-min BST wait window. Order vs. the provisioner fire
#   doesn't matter: mint early and the bootstrap's step-1 sees the token and skips
#   the wait entirely.
#
# REQUIREMENTS
#   expect (brew install expect). The operator key is installed on the worker by the
#   relops_key_admin prestage pkg, so the verify step uses key-based ssh + passwordless
#   sudo (Passwordless Sudo pkg).
#
# PASSWORD SOURCE
#   Defaults to "admin" (the current SimpleMDM auto-admin password on this fleet).
#   SimpleMDM does NOT expose the auto-admin password via API, so when you enable
#   unique-per-device passwords you must source ADMIN_PW another way (secret store,
#   or the rotation step that sets a known password). Override with the ADMIN_PW env.
#
# USAGE
#   ./mint-securetoken.sh <worker-fqdn> [admin-user]
#   ADMIN_PW=... SSHD_WAIT=900 ./mint-securetoken.sh macmini-m4-81.test.releng.mdc1.mozilla.com

set -euo pipefail

HOST="${1:?usage: mint-securetoken.sh <worker-fqdn> [admin-user]}"
ADMIN_USER="${2:-admin}"
ADMIN_PW="${ADMIN_PW:-admin}"
SSHD_WAIT="${SSHD_WAIT:-900}"   # seconds to wait for sshd to come up post-enroll

log() { echo "[mint $(date -u +%H:%M:%SZ)] $*"; }

secure_token_status() {
  # key-based, non-interactive; empty output if we can't get in yet
  ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
      -o ConnectTimeout=15 "${ADMIN_USER}@${HOST}" \
      "sudo sysadminctl -secureTokenStatus ${ADMIN_USER} 2>&1 | sed 's/.*Secure token is //'" 2>/dev/null || true
}

# 1. Wait for sshd (the relops-ssh prestage pkg enables it during DEP convergence).
log "waiting for sshd on ${HOST} (up to ${SSHD_WAIT}s)..."
deadline=$(( $(date +%s) + SSHD_WAIT ))
until nc -z -G 5 "${HOST}" 22 2>/dev/null; do
  [ "$(date +%s)" -ge "${deadline}" ] && { log "ERROR: sshd never came up on ${HOST}"; exit 1; }
  sleep 15
done
log "sshd is up."

# 2. Mint the SecureToken if admin doesn't already hold one. The mint is a PAM
#    password login — the authentication itself grants the first SecureToken (the
#    ~2-3s pause). We run a trivial remote command; success of the auth is what
#    matters. NumberOfPasswordPrompts=1 avoids retry loops on a bad password.
if secure_token_status | grep -q "ENABLED"; then
  log "${ADMIN_USER} already SecureToken-ENABLED — skipping mint."
else
  log "minting SecureToken via interactive password login as ${ADMIN_USER}..."
  set +e
  expect <<EXPECT
set timeout 45
log_user 0
spawn ssh -F /dev/null \
  -o PubkeyAuthentication=no \
  -o PreferredAuthentications=keyboard-interactive,password \
  -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
  -o NumberOfPasswordPrompts=1 -o ConnectTimeout=20 \
  ${ADMIN_USER}@${HOST} true
expect {
  -re "(P|p)assword:" { send "${ADMIN_PW}\r"; exp_continue }
  -re "(denied|failed|Authentication)" { exit 2 }
  timeout { exit 3 }
  eof
}
EXPECT
  rc=$?
  set -e
  case "${rc}" in
    0) log "password login completed." ;;
    2) log "ERROR: authentication denied (wrong ADMIN_PW for ${ADMIN_USER}?)"; exit 2 ;;
    3) log "ERROR: password login timed out"; exit 3 ;;
    *) log "WARN: expect exited ${rc}; verifying anyway..." ;;
  esac
fi

# 3. Verify the token (poll briefly; the grant is near-instant but allow slack).
log "verifying ${ADMIN_USER} SecureToken..."
ok=false
for i in $(seq 1 6); do
  st="$(secure_token_status)"
  log "  attempt ${i}: ${st:-<no answer>}"
  case "${st}" in *ENABLED*) ok=true; break ;; esac
  sleep 5
done
[ "${ok}" = true ] || { log "ERROR: ${ADMIN_USER} SecureToken not ENABLED"; exit 1; }
log "SecureToken OK — ${ADMIN_USER} ENABLED"

# 4. Escrow the Bootstrap Token so this box is MDM-EACS-able next cycle.
#    The bootstrap script SKIPS its own BST-escrow when the token already exists
#    (which it now does, because we pre-minted), so we must escrow here or the box
#    can't be MDM-erased next time (proven prerequisite on Apple Silicon).
#    profiles install with -user/-password is non-interactive.
log "escrowing Bootstrap Token (keeps the box re-EACS-able)..."
ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=20 \
    "${ADMIN_USER}@${HOST}" \
    "sudo profiles install -type bootstraptoken -user ${ADMIN_USER} -password '${ADMIN_PW}'" 2>&1 | sed 's/^/    /' || true
esc="$(ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=15 \
       "${ADMIN_USER}@${HOST}" 'sudo profiles status -type bootstraptoken 2>&1 | grep -i escrowed' 2>/dev/null)" || true
log "  ${esc:-<no answer>}"
case "${esc}" in
  *YES*) log "DONE — ${ADMIN_USER} SecureToken ENABLED + Bootstrap Token escrowed (bootstrappable AND re-EACS-able)."; exit 0 ;;
  *)     log "WARN: SecureToken is ENABLED but BST escrow not confirmed — box may not be MDM-EACS-able until escrowed."; exit 0 ;;
esac
