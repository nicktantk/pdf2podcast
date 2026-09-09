import Foundation

@MainActor
final class SettingsStore: ObservableObject {
    static let defaultGatewayURL = "http://127.0.0.1:8766"
    private static let gatewayURLKey = "gateway-url"

    @Published var gatewayURL: String
    @Published var token: String
    @Published var saveError: String?

    private let defaults: UserDefaults
    private let keychain: KeychainStore

    init(defaults: UserDefaults = .standard, keychain: KeychainStore = KeychainStore()) {
        self.defaults = defaults
        self.keychain = keychain
        gatewayURL = defaults.string(forKey: Self.gatewayURLKey) ?? Self.defaultGatewayURL
        token = keychain.loadToken()
    }

    var baseURL: URL? {
        guard let url = URL(string: gatewayURL.trimmingCharacters(in: .whitespacesAndNewlines)),
              let scheme = url.scheme?.lowercased(),
              scheme == "http" || scheme == "https",
              url.host != nil
        else { return nil }
        return url
    }

    var isConfigured: Bool {
        baseURL != nil && !token.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

    @discardableResult
    func save() -> Bool {
        guard baseURL != nil else {
            saveError = "Enter a valid HTTP or HTTPS gateway URL."
            return false
        }
        do {
            let cleanedToken = token.trimmingCharacters(in: .whitespacesAndNewlines)
            try keychain.saveToken(cleanedToken)
            token = cleanedToken
            gatewayURL = gatewayURL.trimmingCharacters(in: .whitespacesAndNewlines)
            defaults.set(gatewayURL, forKey: Self.gatewayURLKey)
            saveError = nil
            return true
        } catch {
            saveError = error.localizedDescription
            return false
        }
    }
}

