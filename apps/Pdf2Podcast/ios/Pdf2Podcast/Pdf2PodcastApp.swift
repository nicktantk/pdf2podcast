import SwiftUI

@main
struct Pdf2PodcastApp: App {
    @StateObject private var settings: SettingsStore
    @StateObject private var model: AppModel

    init() {
        let sharedSettings = SettingsStore()
        _settings = StateObject(wrappedValue: sharedSettings)
        _model = StateObject(wrappedValue: AppModel(settings: sharedSettings))
    }

    var body: some Scene {
        WindowGroup {
            ContentView(model: model, settings: settings)
        }
    }
}
