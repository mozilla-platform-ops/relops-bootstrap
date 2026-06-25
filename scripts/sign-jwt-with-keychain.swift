// sign-jwt-with-keychain.swift
//
// Builds + signs a JWT using a SCEP-issued client cert sitting in the macOS
// System keychain (with the private key in the Secure Enclave / non-extractable).
//
// The bootstrap script on each m4 host calls this to authenticate to the
// vault-broker. Same tool also works for ad-hoc operator-side integration tests.
//
// Build:
//   swiftc sign-jwt-with-keychain.swift -o sign-jwt
// Run:
//   sudo ./sign-jwt \
//     --issuer-contains "Mozilla RelOps Bootstrap CA Intermediate CA" \
//     --audience       "https://forge.relops.mozilla.com" \
//     --lifetime-secs  60
// Output (stdout): a JWT string.
//
// Why sudo: the SCEP cert was installed by mdmclient into the System keychain
// (PayloadScope=System), which root can read but a normal user can't.

import Foundation
import Security

// ---------- arg parsing ----------

func argValue(_ name: String) -> String? {
    let args = CommandLine.arguments
    guard let i = args.firstIndex(of: name), i + 1 < args.count else { return nil }
    return args[i + 1]
}

let issuerSubstring = argValue("--issuer-contains") ?? "Mozilla RelOps Bootstrap CA Intermediate CA"
let audience        = argValue("--audience")        ?? "https://forge.relops.mozilla.com"
let lifetimeSecs    = Int(argValue("--lifetime-secs") ?? "60") ?? 60
let subject         = argValue("--subject")         ?? Host.current().localizedName ?? "unknown"

func bail(_ msg: String, _ code: Int32 = 1) -> Never {
    FileHandle.standardError.write(("ERROR: " + msg + "\n").data(using: .utf8)!)
    exit(code)
}

// ---------- find the SCEP-issued identity in the System keychain ----------

let query: [String: Any] = [
    kSecClass as String:      kSecClassIdentity,
    kSecReturnRef as String:  true,
    kSecMatchLimit as String: kSecMatchLimitAll,
]

var result: CFTypeRef?
let status = SecItemCopyMatching(query as CFDictionary, &result)
guard status == errSecSuccess, let identities = result as? [SecIdentity] else {
    bail("SecItemCopyMatching status=\(status) — no SecIdentities found in keychain")
}

var match: (SecIdentity, SecCertificate)?
for ident in identities {
    var cert: SecCertificate?
    SecIdentityCopyCertificate(ident, &cert)
    guard let cert = cert else { continue }

    let issuerCFData = SecCertificateCopyNormalizedIssuerSequence(cert)
    // Easier path: SecCertificateCopyValues to pull a printable issuer
    let summary = SecCertificateCopyShortDescription(nil, cert, nil) as String? ?? ""
    let subjectCN = (SecCertificateCopySubjectSummary(cert) as String?) ?? ""

    // Long-form issuer by decoding via the cert's properties dictionary
    var error: Unmanaged<CFError>?
    let issuerProps = SecCertificateCopyValues(cert, [kSecOIDX509V1IssuerName] as CFArray, &error)
    let issuerStr = (issuerProps as? [String: Any])
        .flatMap { $0[kSecOIDX509V1IssuerName as String] as? [String: Any] }
        .flatMap { $0["value"] as? [[String: Any]] }
        .map { entries -> String in
            entries.compactMap { entry -> String? in
                guard let val = entry["value"] as? String else { return nil }
                return val
            }.joined(separator: " / ")
        } ?? summary

    if issuerStr.contains(issuerSubstring) || summary.contains(issuerSubstring) {
        match = (ident, cert)
        break
    }
    _ = subjectCN  // referenced for diagnostics if needed
    _ = issuerCFData
}

guard let (identity, leafCert) = match else {
    bail("no SecIdentity whose issuer contains '\(issuerSubstring)'")
}

// ---------- extract the cert's DER, build x5c entry ----------

let leafDER = SecCertificateCopyData(leafCert) as Data
let leafB64 = leafDER.base64EncodedString()

// ---------- get the private key ref (handle to Secure Enclave) ----------

var privateKey: SecKey?
let pkStatus = SecIdentityCopyPrivateKey(identity, &privateKey)
guard pkStatus == errSecSuccess, let privateKey = privateKey else {
    bail("SecIdentityCopyPrivateKey status=\(pkStatus)")
}

// ---------- determine alg by key attributes ----------

let keyAttrs = SecKeyCopyAttributes(privateKey) as? [String: Any] ?? [:]
let keyType = keyAttrs[kSecAttrKeyType as String] as? String ?? ""
let alg: String
let secAlg: SecKeyAlgorithm
if keyType == (kSecAttrKeyTypeRSA as String) {
    alg = "RS256"
    secAlg = .rsaSignatureMessagePKCS1v15SHA256
} else if keyType == (kSecAttrKeyTypeECSECPrimeRandom as String) {
    alg = "ES256"
    secAlg = .ecdsaSignatureMessageX962SHA256
} else {
    bail("unsupported key type: \(keyType)")
}

// ---------- build JWT header + payload ----------

func b64url(_ data: Data) -> String {
    data.base64EncodedString()
        .replacingOccurrences(of: "+", with: "-")
        .replacingOccurrences(of: "/", with: "_")
        .replacingOccurrences(of: "=", with: "")
}
func b64url(_ string: String) -> String {
    b64url(string.data(using: .utf8)!)
}

let jti = UUID().uuidString
let iat = Int(Date().timeIntervalSince1970)
let exp = iat + lifetimeSecs

let header: [String: Any] = ["alg": alg, "typ": "JWT", "x5c": [leafB64]]
let payload: [String: Any] = ["iat": iat, "exp": exp, "aud": audience, "sub": subject, "jti": jti]

let headerJSON  = try! JSONSerialization.data(withJSONObject: header,  options: [.sortedKeys])
let payloadJSON = try! JSONSerialization.data(withJSONObject: payload, options: [.sortedKeys])

let signingInput = b64url(headerJSON) + "." + b64url(payloadJSON)

// ---------- sign with the Secure Enclave key ----------

var signError: Unmanaged<CFError>?
guard let signature = SecKeyCreateSignature(
    privateKey,
    secAlg,
    signingInput.data(using: .utf8)! as CFData,
    &signError
) else {
    bail("SecKeyCreateSignature failed: \(signError?.takeRetainedValue().localizedDescription ?? "unknown")")
}

// For ES256, SecKeyCreateSignature returns DER-encoded ECDSA-Sig-Value; JWT
// requires raw (R||S) concatenation. Convert if needed.
var sigData = signature as Data
if secAlg == .ecdsaSignatureMessageX962SHA256 {
    // Parse DER SEQUENCE { INTEGER r, INTEGER s } and convert to raw 64-byte concat.
    sigData = derEcdsaToRaw(sigData) ?? sigData
}

print(signingInput + "." + b64url(sigData))

// ---------- helpers ----------

func derEcdsaToRaw(_ der: Data) -> Data? {
    // Minimal DER parser for SEQUENCE of two INTEGERs (32-byte each, left-padded).
    let bytes = [UInt8](der)
    guard bytes.count > 8, bytes[0] == 0x30 else { return nil }
    var i = 1
    // length (short or long form)
    if bytes[i] & 0x80 != 0 {
        let n = Int(bytes[i] & 0x7f); i += 1
        i += n
    } else {
        i += 1
    }
    guard bytes[i] == 0x02 else { return nil }
    i += 1
    let rLen = Int(bytes[i]); i += 1
    var r = Array(bytes[i ..< i + rLen]); i += rLen
    guard bytes[i] == 0x02 else { return nil }
    i += 1
    let sLen = Int(bytes[i]); i += 1
    var s = Array(bytes[i ..< i + sLen])
    // strip leading 0x00 (DER positive integer padding) and left-pad to 32 bytes
    if r.first == 0x00 { r.removeFirst() }
    if s.first == 0x00 { s.removeFirst() }
    while r.count < 32 { r.insert(0, at: 0) }
    while s.count < 32 { s.insert(0, at: 0) }
    return Data(r + s)
}
