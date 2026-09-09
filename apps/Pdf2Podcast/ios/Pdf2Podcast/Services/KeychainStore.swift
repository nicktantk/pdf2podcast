import Foundation
import Security

enum KeychainStoreError: LocalizedError {
    case status(OSStatus)

    var errorDescription: String? {
        switch self {
        case let .status(status):
            let text = SecCopyErrorMessageString(status, nil) as String? ?? "OSStatus \(status)"
            return "The token could not be saved securely: \(text)"
        }
    }
}

struct KeychainStore {
    private let service = "io.pdf2podcast.mobile"
    private let account = "gateway-bearer-token"

    func loadToken() -> String {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
            kSecReturnData as String: true,
            kSecMatchLimit as String: kSecMatchLimitOne,
        ]
        var result: CFTypeRef?
        guard SecItemCopyMatching(query as CFDictionary, &result) == errSecSuccess,
              let data = result as? Data,
              let token = String(data: data, encoding: .utf8)
        else { return "" }
        return token
    }

    func saveToken(_ token: String) throws {
        let value = token.trimmingCharacters(in: .whitespacesAndNewlines)
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
        ]
        if value.isEmpty {
            let status = SecItemDelete(query as CFDictionary)
            if status != errSecSuccess && status != errSecItemNotFound {
                throw KeychainStoreError.status(status)
            }
            return
        }
        let data = Data(value.utf8)
        let updateStatus = SecItemUpdate(
            query as CFDictionary,
            [kSecValueData as String: data] as CFDictionary
        )
        if updateStatus == errSecItemNotFound {
            var insert = query
            insert[kSecValueData as String] = data
            insert[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
            let status = SecItemAdd(insert as CFDictionary, nil)
            guard status == errSecSuccess else { throw KeychainStoreError.status(status) }
        } else if updateStatus != errSecSuccess {
            throw KeychainStoreError.status(updateStatus)
        }
    }
}
