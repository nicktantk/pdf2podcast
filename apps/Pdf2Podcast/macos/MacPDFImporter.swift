// Import copies rather than opening external documents in place through Catalyst's file broker.
import OSLog
import SwiftUI
import UIKit
import UniformTypeIdentifiers

struct MacImportedPDF: Sendable {
    let url: URL
    private let directory: URL

    static func stage(_ source: URL) throws -> MacImportedPDF {
        let files = FileManager.default
        let directory = files.temporaryDirectory
            .appendingPathComponent("Pdf2PodcastUploads", isDirectory: true)
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try files.createDirectory(at: directory, withIntermediateDirectories: true)
        let destination = directory.appendingPathComponent(source.lastPathComponent)
        do {
            var coordinationError: NSError?
            var copyError: Error?
            var copied = false
            NSFileCoordinator(filePresenter: nil).coordinate(
                readingItemAt: source, options: .forUploading, error: &coordinationError
            ) { readableURL in
                do {
                    try files.copyItem(at: readableURL, to: destination)
                    copied = true
                } catch { copyError = error }
            }
            if let coordinationError { throw coordinationError }
            if let copyError { throw copyError }
            guard copied else { throw CocoaError(.fileReadUnknown) }
            return MacImportedPDF(url: destination, directory: directory)
        } catch {
            try? files.removeItem(at: directory)
            throw error
        }
    }

    func removeTemporaryCopy() {
        // Only delete the UUID directory we created, never the selected original or provider URL.
        try? FileManager.default.removeItem(at: directory)
    }
}

@MainActor
struct MacPDFImporter: UIViewControllerRepresentable {
    @Binding var isPresented: Bool
    let onImport: (Result<MacImportedPDF, Error>) -> Void

    func makeCoordinator() -> Coordinator { Coordinator(parent: self) }

    func makeUIViewController(context: Context) -> Presenter {
        let presenter = Presenter()
        presenter.didAppear = { [weak presenter, weak coordinator = context.coordinator] in
            guard let presenter else { return }
            coordinator?.updatePresentation(from: presenter)
        }
        return presenter
    }

    func updateUIViewController(_ controller: Presenter, context: Context) {
        context.coordinator.parent = self
        context.coordinator.updatePresentation(from: controller)
    }

    // Catalyst bridges an explicit presentation to its native file panel. Embedding the
    // picker as the content of a SwiftUI sheet does not invoke that presentation path.
    final class Presenter: UIViewController {
        var didAppear: (() -> Void)?

        override func loadView() {
            view = UIView()
            view.backgroundColor = .clear
        }

        override func viewDidAppear(_ animated: Bool) {
            super.viewDidAppear(animated)
            didAppear?()
        }
    }

    final class Coordinator: NSObject, UIDocumentPickerDelegate {
        var parent: MacPDFImporter
        let logger = Logger(subsystem: "io.pdf2podcast.mac", category: "PDFUpload")
        private var handledSelection = false
        private var picker: UIDocumentPickerViewController?

        init(parent: MacPDFImporter) { self.parent = parent }

        func updatePresentation(from presenter: Presenter) {
            guard parent.isPresented else {
                picker?.dismiss(animated: true)
                picker = nil
                return
            }
            guard picker == nil, presenter.viewIfLoaded?.window != nil,
                  presenter.presentedViewController == nil else { return }
            let picker = UIDocumentPickerViewController(forOpeningContentTypes: [.pdf], asCopy: true)
            picker.allowsMultipleSelection = false
            picker.shouldShowFileExtensions = true
            picker.delegate = self
            self.picker = picker
            handledSelection = false
            logger.notice("native-copy-picker-presenting")
            presenter.present(picker, animated: true) { [weak self] in
                self?.logger.notice("native-copy-picker-presentation-completed")
            }
        }

        func documentPickerWasCancelled(_ controller: UIDocumentPickerViewController) {
            guard !handledSelection else { return }
            handledSelection = true
            logger.notice("copy-picker-cancelled")
            parent.isPresented = false
        }

        func documentPicker(_ controller: UIDocumentPickerViewController, didPickDocumentsAt urls: [URL]) {
            guard !handledSelection else { return }
            handledSelection = true
            guard let source = urls.first else {
                parent.isPresented = false
                parent.onImport(.failure(CocoaError(.fileReadUnknown)))
                return
            }
            let accessed = source.startAccessingSecurityScopedResource()
            logger.notice("copy-picker-selected; preparing-private-copy")
            Task { @MainActor in
                defer { if accessed { source.stopAccessingSecurityScopedResource() } }
                do {
                    let imported = try await Task.detached(priority: .userInitiated) {
                        try MacImportedPDF.stage(source)
                    }.value
                    logger.notice("private-copy-ready")
                    parent.isPresented = false
                    parent.onImport(.success(imported))
                } catch {
                    let failure = error as NSError
                    logger.error("import-failed domain=\(failure.domain, privacy: .public) code=\(failure.code)")
                    parent.isPresented = false
                    parent.onImport(.failure(error))
                }
            }
        }
    }
}
