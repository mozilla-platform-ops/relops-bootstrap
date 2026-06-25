// extract-identity.swift — export a single keychain identity as PKCS12.
//
// `security export -t identities` exports ALL identities; we want just the
// SCEP-issued one to keep /var/root/.relops-bootstrap/ clean. This helper
// uses SecItemExport with a specific SecIdentityRef.
//
// Usage:
//   sudo swift extract-identity.swift <identity-label> <output-p12> <passphrase>
//
// e.g.  sudo swift extract-identity.swift "Mac mini" /tmp/relops.p12 supersecret
//
// Exits non-zero on failure with a single-line error on stderr.

import Foundation
import Security

func die(_ code: Int32, _ msg: String) -> Never {
    FileHandle.standardError.write(Data((msg + "\n").utf8))
    exit(code)
}

guard CommandLine.arguments.count == 4 else {
    die(2, "usage: extract-identity.swift <label> <output.p12> <passphrase>")
}
let label = CommandLine.arguments[1]
let outputPath = CommandLine.arguments[2]
let passphrase = CommandLine.arguments[3]

let q: [String: Any] = [
    kSecClass as String:        kSecClassIdentity,
    kSecAttrLabel as String:    label,
    kSecReturnRef as String:    true,
    kSecMatchLimit as String:   kSecMatchLimitOne,
]
var r: CFTypeRef?
let lookupStatus = SecItemCopyMatching(q as CFDictionary, &r)
guard lookupStatus == errSecSuccess, let identity = r else {
    die(3, "identity \(label) not found (status \(lookupStatus))")
}

var keyParams = SecItemImportExportKeyParameters(
    version: UInt32(SEC_KEY_IMPORT_EXPORT_PARAMS_VERSION),
    flags: SecKeyImportExportFlags(rawValue: 0),
    passphrase: Unmanaged.passUnretained(passphrase as CFTypeRef),
    alertTitle: Unmanaged.passUnretained("" as CFString),
    alertPrompt: Unmanaged.passUnretained("" as CFString),
    accessRef: nil,
    keyUsage: nil,
    keyAttributes: nil
)

var exportedData: CFData?
let exportStatus = SecItemExport(
    identity,
    .formatPKCS12,
    SecItemImportExportFlags(rawValue: 0),
    &keyParams,
    &exportedData
)

guard exportStatus == errSecSuccess, let data = exportedData as Data? else {
    die(4, "SecItemExport failed (status \(exportStatus))")
}

let outURL = URL(fileURLWithPath: outputPath)
do {
    try data.write(to: outURL, options: .atomic)
    try FileManager.default.setAttributes(
        [.posixPermissions: NSNumber(value: 0o600)],
        ofItemAtPath: outputPath
    )
} catch {
    die(5, "write to \(outputPath) failed: \(error)")
}

print("wrote \(data.count) bytes to \(outputPath)")
