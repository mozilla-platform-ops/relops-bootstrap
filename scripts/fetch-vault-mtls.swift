// fetch-vault-mtls.swift — fetch role-scoped vault.yaml from vault-broker via mTLS.
//
// Replaces the older sign-jwt-with-keychain.swift approach. The macOS keychain
// ACL gates SecKeyCreateSignature to a hard-coded list of network-stack helpers
// (configd, nehelper, NEIKEv2Provider, racoon) — non-Apple binaries can't
// invoke it, even running as root from a LaunchDaemon. URLSession sidesteps
// this entirely: during the TLS handshake, the OS network stack pulls the
// SCEP-issued client cert from the System keychain and uses it via those same
// helpers. Our code never touches the private key.
//
// Usage:
//   swift fetch-vault-mtls.swift <role>
// e.g. swift fetch-vault-mtls.swift gecko_t_osx_1500_m4_no_sip
//
// Env:
//   BROKER_URL  default https://forge.relops.mozilla.com  (override for testing)
//
// Exit codes:
//   0 = vault.yaml content printed to stdout
//   1 = transport error (DNS, TLS handshake, network)
//   2 = HTTP non-2xx (broker rejected the cert or role mismatch)
//   3 = wrong argv

import Foundation

func die(_ code: Int32, _ msg: String) -> Never {
    FileHandle.standardError.write(Data((msg + "\n").utf8))
    exit(code)
}

guard CommandLine.arguments.count == 2 else {
    die(3, "usage: fetch-vault-mtls.swift <role>")
}
let role = CommandLine.arguments[1]

let brokerURL = ProcessInfo.processInfo.environment["BROKER_URL"] ?? "https://forge.relops.mozilla.com"
guard let url = URL(string: "\(brokerURL)/secret/\(role)") else {
    die(3, "bad URL: \(brokerURL)/secret/\(role)")
}

// URLSession receives an NSURLAuthenticationMethodClientCertificate challenge
// when the LB's Server TLS Policy requests one. The GCP HTTPS LB sends an
// EMPTY `distinguishedNames` list, which makes Apple's .performDefaultHandling
// conservatively decide NOT to send any cert. So we must look up the identity
// ourselves and provide it explicitly via URLCredential.
//
// We can read kSecClassIdentity from the legacy System keychain even without
// the com.apple.identities access-group entitlement (that gate only applies
// to the modern data-protection keychain, not the legacy CSSM file backend).
// The actual TLS signing, when URLSession uses our provided identity, runs
// inside Network framework / Secure Transport processes that DO have sign
// access via the System.keychain ACL's "Allow All" entry — so the keychain
// ACL wall that blocks SecKeyCreateSignature in our own code is sidestepped.
final class CertChallengeDelegate: NSObject, URLSessionDelegate {
    let identityLabel: String

    init(identityLabel: String) {
        self.identityLabel = identityLabel
    }

    private func lookupIdentity() -> SecIdentity? {
        let q: [String: Any] = [
            kSecClass as String:        kSecClassIdentity,
            kSecAttrLabel as String:    identityLabel,
            kSecReturnRef as String:    true,
            kSecMatchLimit as String:   kSecMatchLimitOne,
        ]
        var r: CFTypeRef?
        let status = SecItemCopyMatching(q as CFDictionary, &r)
        if status != errSecSuccess {
            FileHandle.standardError.write(Data("identity lookup status: \(status)\n".utf8))
            return nil
        }
        return (r as! SecIdentity?)
    }

    /// Enumerate all certs in the legacy keychain. Dumps labels to stderr so
    /// we can see what's there; returns refs.
    private func enumerateCerts() -> [(String, SecCertificate)] {
        let q: [String: Any] = [
            kSecClass as String:        kSecClassCertificate,
            kSecMatchLimit as String:   kSecMatchLimitAll,
            kSecReturnRef as String:    true,
            kSecReturnAttributes as String: true,
        ]
        var r: CFTypeRef?
        let status = SecItemCopyMatching(q as CFDictionary, &r)
        guard status == errSecSuccess, let arr = r as? [[String: Any]] else {
            FileHandle.standardError.write(Data("enumerateCerts status: \(status)\n".utf8))
            return []
        }
        var out: [(String, SecCertificate)] = []
        for item in arr {
            let labl = (item["labl"] as? String) ?? "?"
            if let ref = item[kSecValueRef as String] {
                out.append((labl, ref as! SecCertificate))
            }
        }
        return out
    }

    /// Find the intermediate cert that issued the leaf, by matching the leaf's
    /// issuer DN against each candidate cert's subject DN.
    private func findIssuer(of leaf: SecCertificate, from candidates: [(String, SecCertificate)]) -> SecCertificate? {
        guard let leafIssuer = SecCertificateCopyNormalizedIssuerSequence(leaf) as Data? else {
            return nil
        }
        for (labl, candidate) in candidates {
            guard let candidateSubject = SecCertificateCopyNormalizedSubjectSequence(candidate) as Data? else { continue }
            if candidateSubject == leafIssuer {
                FileHandle.standardError.write(Data("matched intermediate: \(labl)\n".utf8))
                return candidate
            }
        }
        return nil
    }

    func urlSession(_ session: URLSession,
                    didReceive challenge: URLAuthenticationChallenge,
                    completionHandler: @escaping (URLSession.AuthChallengeDisposition, URLCredential?) -> Void) {
        let method = challenge.protectionSpace.authenticationMethod
        FileHandle.standardError.write(Data("challenge: \(method)\n".utf8))

        if method != NSURLAuthenticationMethodClientCertificate {
            completionHandler(.performDefaultHandling, nil)
            return
        }

        guard let identity = lookupIdentity() else {
            FileHandle.standardError.write(Data("no identity found for label \(identityLabel)\n".utf8))
            completionHandler(.cancelAuthenticationChallenge, nil)
            return
        }

        // Dump everything we see, then try to find the intermediate by
        // matching its subject DN against the leaf's issuer DN.
        let allCerts = enumerateCerts()
        FileHandle.standardError.write(Data("certs in keychain (\(allCerts.count)):\n".utf8))
        for (labl, _) in allCerts {
            FileHandle.standardError.write(Data("  - \(labl)\n".utf8))
        }

        var leaf: SecCertificate?
        SecIdentityCopyCertificate(identity, &leaf)

        var extras: [SecCertificate]? = nil
        if let leaf = leaf, let intermediate = findIssuer(of: leaf, from: allCerts) {
            extras = [intermediate]
        }

        let count = extras?.count ?? 0
        FileHandle.standardError.write(Data("offering identity \(identityLabel) + \(count) intermediate(s)\n".utf8))

        // Apple's NSURLCredential init CRASHES on an empty (non-nil) certificates
        // array. Pass nil when we don't have intermediates.
        let credential: URLCredential
        if let extras = extras, !extras.isEmpty {
            credential = URLCredential(identity: identity, certificates: extras, persistence: .forSession)
        } else {
            credential = URLCredential(identity: identity, certificates: nil, persistence: .forSession)
        }
        completionHandler(.useCredential, credential)
    }
}

let sem = DispatchSemaphore(value: 0)
var httpStatus = -1
var body: Data = Data()
var transportError: Error?

// Identity label is the keychain `labl` attribute. For SCEP-issued certs, that
// equals the cert's Subject CN. The CN is currently set to the device serial
// (via SimpleMDM ComputerName -> step-ca template), e.g. "Mac mini" on m4-81.
// Override per-host via IDENTITY_LABEL env var when the CN is different.
let identityLabel = ProcessInfo.processInfo.environment["IDENTITY_LABEL"] ?? "Mac mini"
let session = URLSession(
    configuration: .ephemeral,
    delegate: CertChallengeDelegate(identityLabel: identityLabel),
    delegateQueue: nil)
let task = session.dataTask(with: url) { data, response, error in
    transportError = error
    if let r = response as? HTTPURLResponse { httpStatus = r.statusCode }
    if let d = data { body = d }
    sem.signal()
}
task.resume()
_ = sem.wait(timeout: .now() + .seconds(30))

if let e = transportError {
    die(1, "transport error: \(e)")
}

if (200...299).contains(httpStatus) {
    FileHandle.standardOutput.write(body)
    exit(0)
}

FileHandle.standardError.write(Data("HTTP \(httpStatus)\n".utf8))
FileHandle.standardError.write(body)
exit(2)
