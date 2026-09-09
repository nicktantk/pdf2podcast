import Foundation

enum GatewayClientError: LocalizedError, Equatable {
    case invalidBaseURL
    case missingToken
    case transport(String)
    case server(status: Int, code: String, message: String)
    case invalidResponse(String)

    var errorDescription: String? {
        switch self {
        case .invalidBaseURL:
            return "The gateway URL is not valid."
        case .missingToken:
            return "Add the mobile gateway token in Settings."
        case let .transport(message):
            return "Couldn’t reach Pdf2Podcast backend: \(message)"
        case let .server(_, _, message):
            return message
        case let .invalidResponse(message):
            return "Pdf2Podcast returned an unreadable response: \(message)"
        }
    }
}

protocol GatewayServing {
    func health() async throws -> HealthResponse
    func connection() async throws -> ConnectionResponse
    func history(limit: Int, before: Int?) async throws -> HistoryResponse
    func chat(message: String, turnID: String) async throws -> ChatResponse
    func tasks(includeFinished: Bool) async throws -> TasksResponse
    func upsertTask(id: String, summary: String, detail: String, priority: String) async throws -> TaskItem
    func setTaskStatus(id: String, status: String) async throws -> TaskItem
    func fleet(alertLimit: Int) async throws -> FleetResponse
    func submitPDFForAudio(
        filename: String,
        fileB64: String,
        title: String?,
        source: String?,
        transcriptPrompt: String?,
        ttsProvider: String?,
        ttsVoice: String?
    ) async throws -> PDFAudioJob
    func pdfJobs(limit: Int) async throws -> PDFJobsResponse
    func pdfJob(id: String) async throws -> PDFAudioJob
    func downloadPDFAudio(jobID: String) async throws -> URL
}

final class GatewayClient: GatewayServing {
    private let baseURL: URL
    private let token: String
    private let session: URLSession
    private let decoder: JSONDecoder
    private let encoder: JSONEncoder

    init(baseURL: URL, token: String, session: URLSession = .shared) {
        self.baseURL = baseURL
        self.token = token.trimmingCharacters(in: .whitespacesAndNewlines)
        self.session = session
        decoder = JSONDecoder()
        encoder = JSONEncoder()
    }

    func health() async throws -> HealthResponse {
        try await send(path: "/api/health")
    }

    func connection() async throws -> ConnectionResponse {
        try await send(path: "/api/connection")
    }

    func history(limit: Int = 100, before: Int? = nil) async throws -> HistoryResponse {
        var query = [URLQueryItem(name: "limit", value: String(limit))]
        if let before { query.append(URLQueryItem(name: "before", value: String(before))) }
        return try await send(path: "/api/history", query: query)
    }

    func chat(message: String, turnID: String) async throws -> ChatResponse {
        let body = ChatRequest(message: message, turnID: turnID, source: "mobile-ios")
        return try await send(path: "/api/chat", method: "POST", body: body, timeout: 905)
    }

    func tasks(includeFinished: Bool = false) async throws -> TasksResponse {
        try await send(
            path: "/api/tasks",
            query: [URLQueryItem(name: "include_finished", value: String(includeFinished))]
        )
    }

    func upsertTask(id: String, summary: String, detail: String, priority: String) async throws -> TaskItem {
        try await send(
            path: "/api/tasks",
            method: "POST",
            body: TaskRequest(id: id, summary: summary, detail: detail, priority: priority)
        )
    }

    func setTaskStatus(id: String, status: String) async throws -> TaskItem {
        try await send(
            path: "/api/tasks/status",
            method: "POST",
            body: StatusRequest(id: id, status: status)
        )
    }

    func fleet(alertLimit: Int = 100) async throws -> FleetResponse {
        try await send(
            path: "/api/fleet",
            query: [URLQueryItem(name: "alert_limit", value: String(alertLimit))]
        )
    }

    func submitPDFForAudio(
        filename: String,
        fileB64: String,
        title: String?,
        source: String?,
        transcriptPrompt: String?,
        ttsProvider: String?,
        ttsVoice: String?
    ) async throws -> PDFAudioJob {
        try await send(
            path: "/api/audio/jobs",
            method: "POST",
            body: PDFJobRequest(
                filename: filename,
                fileB64: fileB64,
                title: title?.trimmingCharacters(in: .whitespacesAndNewlines),
                source: source?.trimmingCharacters(in: .whitespacesAndNewlines),
                transcriptPrompt: transcriptPrompt?.trimmingCharacters(in: .whitespacesAndNewlines),
                ttsProvider: ttsProvider?.trimmingCharacters(in: .whitespacesAndNewlines),
                ttsVoice: ttsVoice?.trimmingCharacters(in: .whitespacesAndNewlines)
            ),
            timeout: 120
        )
    }

    func pdfJobs(limit: Int = 100) async throws -> PDFJobsResponse {
        try await send(
            path: "/api/audio/jobs",
            query: [URLQueryItem(name: "limit", value: String(limit))]
        )
    }

    func pdfJob(id: String) async throws -> PDFAudioJob {
        try await send(path: "/api/audio/jobs/\(id)")
    }

    func downloadPDFAudio(jobID: String) async throws -> URL {
        guard !token.isEmpty else { throw GatewayClientError.missingToken }
        guard var components = URLComponents(url: baseURL, resolvingAgainstBaseURL: false) else {
            throw GatewayClientError.invalidBaseURL
        }
        let basePath = components.path.hasSuffix("/") ? String(components.path.dropLast()) : components.path
        components.path = basePath + "/api/audio/jobs/\(jobID)/audio"
        guard let url = components.url else { throw GatewayClientError.invalidBaseURL }

        var request = URLRequest(url: url, timeoutInterval: 600)
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        request.setValue("audio/mpeg", forHTTPHeaderField: "Accept")
        let temporary: URL
        let response: URLResponse
        do {
            (temporary, response) = try await session.download(for: request)
        } catch {
            throw GatewayClientError.transport(error.localizedDescription)
        }
        guard let http = response as? HTTPURLResponse else {
            throw GatewayClientError.invalidResponse("missing HTTP status")
        }
        guard 200..<300 ~= http.statusCode else {
            let data = (try? Data(contentsOf: temporary)) ?? Data()
            if let envelope = try? decoder.decode(ErrorEnvelope.self, from: data) {
                throw GatewayClientError.server(
                    status: http.statusCode,
                    code: envelope.error.code,
                    message: envelope.error.message
                )
            }
            throw GatewayClientError.server(
                status: http.statusCode,
                code: "http_\(http.statusCode)",
                message: HTTPURLResponse.localizedString(forStatusCode: http.statusCode)
            )
        }

        let destination = FileManager.default.temporaryDirectory
            .appendingPathComponent("\(jobID).mp3")
        try? FileManager.default.removeItem(at: destination)
        do {
            try FileManager.default.moveItem(at: temporary, to: destination)
        } catch {
            throw GatewayClientError.transport("Could not save downloaded audio: \(error.localizedDescription)")
        }
        return destination
    }

    private func send<Response: Decodable>(
        path: String,
        query: [URLQueryItem] = [],
        method: String = "GET",
        timeout: TimeInterval = 20
    ) async throws -> Response {
        try await send(path: path, query: query, method: method, bodyData: nil, timeout: timeout)
    }

    private func send<Response: Decodable, Body: Encodable>(
        path: String,
        query: [URLQueryItem] = [],
        method: String,
        body: Body,
        timeout: TimeInterval = 20
    ) async throws -> Response {
        let bodyData: Data
        do {
            bodyData = try encoder.encode(body)
        } catch {
            throw GatewayClientError.invalidResponse(error.localizedDescription)
        }
        return try await send(path: path, query: query, method: method, bodyData: bodyData, timeout: timeout)
    }

    private func send<Response: Decodable>(
        path: String,
        query: [URLQueryItem],
        method: String,
        bodyData: Data?,
        timeout: TimeInterval
    ) async throws -> Response {
        guard !token.isEmpty else { throw GatewayClientError.missingToken }
        guard var components = URLComponents(url: baseURL, resolvingAgainstBaseURL: false) else {
            throw GatewayClientError.invalidBaseURL
        }
        let basePath = components.path.hasSuffix("/") ? String(components.path.dropLast()) : components.path
        components.path = basePath + path
        components.queryItems = query.isEmpty ? nil : query
        guard let url = components.url else { throw GatewayClientError.invalidBaseURL }

        var request = URLRequest(url: url, timeoutInterval: timeout)
        request.httpMethod = method
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        request.cachePolicy = .reloadIgnoringLocalAndRemoteCacheData
        if let bodyData {
            request.httpBody = bodyData
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        }

        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await session.data(for: request)
        } catch {
            throw GatewayClientError.transport(error.localizedDescription)
        }
        guard let http = response as? HTTPURLResponse else {
            throw GatewayClientError.invalidResponse("missing HTTP status")
        }
        guard 200..<300 ~= http.statusCode else {
            if let envelope = try? decoder.decode(ErrorEnvelope.self, from: data) {
                throw GatewayClientError.server(
                    status: http.statusCode,
                    code: envelope.error.code,
                    message: envelope.error.message
                )
            }
            throw GatewayClientError.server(
                status: http.statusCode,
                code: "http_\(http.statusCode)",
                message: HTTPURLResponse.localizedString(forStatusCode: http.statusCode)
            )
        }
        do {
            return try decoder.decode(Response.self, from: data)
        } catch {
            throw GatewayClientError.invalidResponse(error.localizedDescription)
        }
    }
}

private struct ChatRequest: Encodable {
    let message: String
    let turnID: String
    let source: String

    enum CodingKeys: String, CodingKey {
        case message, source
        case turnID = "turn_id"
    }
}

private struct TaskRequest: Encodable {
    let id: String
    let summary: String
    let detail: String
    let priority: String
}

private struct StatusRequest: Encodable {
    let id: String
    let status: String
}

private struct PDFJobRequest: Encodable {
    let filename: String
    let fileB64: String
    let title: String?
    let source: String?
    let transcriptPrompt: String?
    let ttsProvider: String?
    let ttsVoice: String?

    enum CodingKeys: String, CodingKey {
        case filename
        case fileB64 = "file_b64"
        case title
        case source
        case transcriptPrompt = "transcript_prompt"
        case ttsProvider = "tts_provider"
        case ttsVoice = "tts_voice"
    }
}
