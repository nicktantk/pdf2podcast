import Foundation

struct HealthResponse: Decodable {
    let ok: Bool
    let service: String
    let node: String
}

struct ConnectionResponse: Decodable {
    let connected: Bool
    let node: NodeInfo
}

struct NodeInfo: Codable, Identifiable, Hashable {
    var id: String { nodeID ?? name }
    let nodeID: String?
    let name: String
    let version: String?
    let capabilities: [String]
    let methods: [String]

    enum CodingKeys: String, CodingKey {
        case nodeID = "node_id"
        case name, version, capabilities, methods
    }

    init(
        nodeID: String? = nil,
        name: String,
        version: String? = nil,
        capabilities: [String] = [],
        methods: [String] = []
    ) {
        self.nodeID = nodeID
        self.name = name
        self.version = version
        self.capabilities = capabilities
        self.methods = methods
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        nodeID = try container.decodeIfPresent(String.self, forKey: .nodeID)
        name = try container.decodeIfPresent(String.self, forKey: .name) ?? "Unknown node"
        version = try container.decodeIfPresent(String.self, forKey: .version)
        capabilities = try container.decodeIfPresent([String].self, forKey: .capabilities) ?? []
        methods = try container.decodeIfPresent([String].self, forKey: .methods) ?? []
    }
}

struct ChatMessage: Codable, Identifiable, Hashable {
    let id: String
    let role: String
    let text: String
    let origin: String
    let node: String
    let timestamp: Double
    let engine: String
    let turnID: String
    let source: String
    let status: String

    var isUser: Bool { role == "user" }
    var date: Date { Date(timeIntervalSince1970: timestamp) }

    enum CodingKeys: String, CodingKey {
        case id, role, text, origin, node, engine, source, status
        case timestamp = "ts"
        case turnID = "turn_id"
    }

    init(
        id: String = UUID().uuidString,
        role: String,
        text: String,
        origin: String = "mobile",
        node: String = "",
        timestamp: Double = Date().timeIntervalSince1970,
        engine: String = "",
        turnID: String = "",
        source: String = "mobile",
        status: String = "completed"
    ) {
        self.id = id
        self.role = role
        self.text = text
        self.origin = origin
        self.node = node
        self.timestamp = timestamp
        self.engine = engine
        self.turnID = turnID
        self.source = source
        self.status = status
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        id = try container.decodeIfPresent(String.self, forKey: .id) ?? UUID().uuidString
        role = try container.decodeIfPresent(String.self, forKey: .role) ?? "assistant"
        text = try container.decodeIfPresent(String.self, forKey: .text) ?? ""
        origin = try container.decodeIfPresent(String.self, forKey: .origin) ?? ""
        node = try container.decodeIfPresent(String.self, forKey: .node) ?? ""
        timestamp = try container.decodeIfPresent(Double.self, forKey: .timestamp) ?? 0
        engine = try container.decodeIfPresent(String.self, forKey: .engine) ?? ""
        turnID = try container.decodeIfPresent(String.self, forKey: .turnID) ?? ""
        source = try container.decodeIfPresent(String.self, forKey: .source) ?? ""
        status = try container.decodeIfPresent(String.self, forKey: .status) ?? "completed"
    }
}

struct HistoryResponse: Decodable {
    let conversation: String
    let messages: [ChatMessage]
    let total: Int
    let start: Int
    let hasMore: Bool
    let nextBefore: Int?

    enum CodingKeys: String, CodingKey {
        case conversation, messages, total, start
        case hasMore = "has_more"
        case nextBefore = "next_before"
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        conversation = try container.decodeIfPresent(String.self, forKey: .conversation) ?? "shared"
        messages = try container.decodeIfPresent([ChatMessage].self, forKey: .messages) ?? []
        total = try container.decodeIfPresent(Int.self, forKey: .total) ?? messages.count
        start = try container.decodeIfPresent(Int.self, forKey: .start) ?? 0
        hasMore = try container.decodeIfPresent(Bool.self, forKey: .hasMore) ?? false
        nextBefore = try container.decodeIfPresent(Int.self, forKey: .nextBefore)
    }
}

struct ChatResponse: Decodable {
    let answer: String
    let engine: String
    let handoff: String?
    let forward: String?
    let turn: Int?
    let replayed: Bool

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        answer = try container.decode(String.self, forKey: .answer)
        engine = try container.decodeIfPresent(String.self, forKey: .engine) ?? ""
        handoff = try container.decodeIfPresent(String.self, forKey: .handoff)
        forward = try container.decodeIfPresent(String.self, forKey: .forward)
        turn = try container.decodeIfPresent(Int.self, forKey: .turn)
        replayed = try container.decodeIfPresent(Bool.self, forKey: .replayed) ?? false
    }

    enum CodingKeys: String, CodingKey {
        case answer, engine, handoff, forward, turn, replayed
    }
}

struct TaskItem: Codable, Identifiable, Hashable {
    let id: String
    let summary: String
    let status: String
    let priority: String
    let detail: String
    let createdAt: Double
    let updatedAt: Double
    let node: String?
    let address: String?

    enum CodingKeys: String, CodingKey {
        case id, summary, status, priority, detail, node, address
        case createdAt = "created_at"
        case updatedAt = "updated_at"
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        id = try container.decode(String.self, forKey: .id)
        summary = try container.decode(String.self, forKey: .summary)
        status = try container.decodeIfPresent(String.self, forKey: .status) ?? "queued"
        priority = try container.decodeIfPresent(String.self, forKey: .priority) ?? "normal"
        detail = try container.decodeIfPresent(String.self, forKey: .detail) ?? ""
        createdAt = try container.decodeIfPresent(Double.self, forKey: .createdAt) ?? 0
        updatedAt = try container.decodeIfPresent(Double.self, forKey: .updatedAt) ?? 0
        node = try container.decodeIfPresent(String.self, forKey: .node)
        address = try container.decodeIfPresent(String.self, forKey: .address)
    }
}

struct AgentItem: Decodable, Identifiable, Hashable {
    var id: String { address ?? "\(session ?? ""):\(name)" }
    let name: String
    let session: String?
    let state: String
    let headline: String?
    let question: String?
    let node: String?
    let address: String?
    let priority: String?
    let summary: String?

    enum CodingKeys: String, CodingKey {
        case name, agent, session, state, headline, question, node, address, priority, summary
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        name = try container.decodeIfPresent(String.self, forKey: .name)
            ?? container.decodeIfPresent(String.self, forKey: .agent)
            ?? "Agent"
        session = try container.decodeIfPresent(String.self, forKey: .session)
        state = try container.decodeIfPresent(String.self, forKey: .state) ?? "idle"
        headline = try container.decodeIfPresent(String.self, forKey: .headline)
        question = try container.decodeIfPresent(String.self, forKey: .question)
        node = try container.decodeIfPresent(String.self, forKey: .node)
        address = try container.decodeIfPresent(String.self, forKey: .address)
        priority = try container.decodeIfPresent(String.self, forKey: .priority)
        summary = try container.decodeIfPresent(String.self, forKey: .summary)
    }
}

struct TasksResponse: Decodable {
    let tasks: [TaskItem]
    let agents: [AgentItem]
    let summary: String
    let unavailableNodes: [String: String]

    enum CodingKeys: String, CodingKey {
        case tasks, agents, summary
        case unavailableNodes = "unavailable_nodes"
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        tasks = try container.decodeIfPresent([TaskItem].self, forKey: .tasks) ?? []
        agents = try container.decodeIfPresent([AgentItem].self, forKey: .agents) ?? []
        summary = try container.decodeIfPresent(String.self, forKey: .summary) ?? ""
        unavailableNodes = try container.decodeIfPresent([String: String].self, forKey: .unavailableNodes) ?? [:]
    }
}

struct FleetAlert: Codable, Identifiable, Hashable {
    let id: String
    let kind: String
    let agent: String
    let headline: String
    let detail: String
    let at: Double
    var sourceNode: String?

    var date: Date { Date(timeIntervalSince1970: at) }

    init(
        id: String,
        kind: String,
        agent: String,
        headline: String,
        detail: String,
        at: Double,
        sourceNode: String? = nil
    ) {
        self.id = id
        self.kind = kind
        self.agent = agent
        self.headline = headline
        self.detail = detail
        self.at = at
        self.sourceNode = sourceNode
    }
}

struct FleetResponse: Decodable {
    let nodes: [NodeResult]
    let alerts: [AlertResult]
    let errors: [String: GatewayProblem]
}

struct NodeResult: Decodable, Identifiable {
    var id: String { node ?? name ?? UUID().uuidString }
    let node: String?
    let name: String?
    let ok: NodeInfo?
    let error: GatewayProblem?
}

struct AlertResult: Decodable, Identifiable {
    var id: String { node ?? name ?? UUID().uuidString }
    let node: String?
    let name: String?
    let ok: AlertsBody?
    let error: GatewayProblem?
}

struct AlertsBody: Decodable {
    let alerts: [FleetAlert]
}

struct GatewayProblem: Codable, Error, Hashable {
    let code: String
    let message: String
}

struct ErrorEnvelope: Decodable {
    let error: GatewayProblem
}

struct PendingTurn: Codable, Identifiable, Equatable {
    let id: String
    let message: String
    let createdAt: Double
}

struct PDFJobsResponse: Decodable {
    let jobs: [PDFAudioJob]
}

struct PDFAudioJob: Codable, Identifiable, Equatable {
    var id: String { jobID }
    let jobID: String
    let status: String
    let stage: String?
    let filename: String
    let source: String
    let title: String?
    let ttsProvider: String?
    let transcriptPrompt: String?
    let createdAt: Double?
    let updatedAt: Double?
    let completedAt: Double?
    let progressPercent: Int?
    let estimatedSecondsRemaining: Int?
    let chunksDone: Int?
    let chunksTotal: Int?
    let chapterCount: Int?
    let pageCount: Int?
    let transcriptChars: Int?
    let audioBytes: Int?
    let audioPath: String?
    let transcriptPath: String?
    let error: String?
    let detail: String?

    var isActive: Bool { status == "queued" || status == "processing" }
    var isComplete: Bool { status == "complete" }
    var progressFraction: Double { Double(progressPercent ?? 0) / 100 }

    enum CodingKeys: String, CodingKey {
        case jobID = "job_id"
        case status
        case stage
        case filename
        case source
        case title
        case ttsProvider = "tts_provider"
        case transcriptPrompt = "transcript_prompt"
        case createdAt = "created_at"
        case updatedAt = "updated_at"
        case completedAt = "completed_at"
        case progressPercent = "progress_percent"
        case estimatedSecondsRemaining = "estimated_seconds_remaining"
        case chunksDone = "chunks_done"
        case chunksTotal = "chunks_total"
        case chapterCount = "chapter_count"
        case pageCount = "page_count"
        case transcriptChars = "transcript_chars"
        case audioBytes = "audio_bytes"
        case audioPath = "audio_path"
        case transcriptPath = "transcript_path"
        case error
        case detail
    }

    init(
        jobID: String,
        status: String,
        stage: String? = nil,
        filename: String = "",
        source: String = "",
        title: String? = nil,
        ttsProvider: String? = nil,
        transcriptPrompt: String? = nil,
        createdAt: Double? = nil,
        updatedAt: Double? = nil,
        completedAt: Double? = nil,
        progressPercent: Int? = nil,
        estimatedSecondsRemaining: Int? = nil,
        chunksDone: Int? = nil,
        chunksTotal: Int? = nil,
        chapterCount: Int? = nil,
        pageCount: Int? = nil,
        transcriptChars: Int? = nil,
        audioBytes: Int? = nil,
        audioPath: String? = nil,
        transcriptPath: String? = nil,
        error: String? = nil,
        detail: String? = nil
    ) {
        self.jobID = jobID
        self.status = status
        self.stage = stage
        self.filename = filename
        self.source = source
        self.title = title
        self.ttsProvider = ttsProvider
        self.transcriptPrompt = transcriptPrompt
        self.createdAt = createdAt
        self.updatedAt = updatedAt
        self.completedAt = completedAt
        self.progressPercent = progressPercent
        self.estimatedSecondsRemaining = estimatedSecondsRemaining
        self.chunksDone = chunksDone
        self.chunksTotal = chunksTotal
        self.chapterCount = chapterCount
        self.pageCount = pageCount
        self.transcriptChars = transcriptChars
        self.audioBytes = audioBytes
        self.audioPath = audioPath
        self.transcriptPath = transcriptPath
        self.error = error
        self.detail = detail
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        jobID = try container.decodeIfPresent(String.self, forKey: .jobID) ?? UUID().uuidString
        status = try container.decodeIfPresent(String.self, forKey: .status) ?? "queued"
        stage = try container.decodeIfPresent(String.self, forKey: .stage)
        filename = try container.decodeIfPresent(String.self, forKey: .filename) ?? "upload.pdf"
        source = try container.decodeIfPresent(String.self, forKey: .source) ?? "mobile-ios"
        title = try container.decodeIfPresent(String.self, forKey: .title)
        ttsProvider = try container.decodeIfPresent(String.self, forKey: .ttsProvider)
        transcriptPrompt = try container.decodeIfPresent(String.self, forKey: .transcriptPrompt)
        createdAt = try container.decodeIfPresent(Double.self, forKey: .createdAt)
        updatedAt = try container.decodeIfPresent(Double.self, forKey: .updatedAt)
        completedAt = try container.decodeIfPresent(Double.self, forKey: .completedAt)
        progressPercent = try container.decodeIfPresent(Int.self, forKey: .progressPercent)
        estimatedSecondsRemaining = try container.decodeIfPresent(Int.self, forKey: .estimatedSecondsRemaining)
        chunksDone = try container.decodeIfPresent(Int.self, forKey: .chunksDone)
        chunksTotal = try container.decodeIfPresent(Int.self, forKey: .chunksTotal)
        chapterCount = try container.decodeIfPresent(Int.self, forKey: .chapterCount)
        pageCount = try container.decodeIfPresent(Int.self, forKey: .pageCount)
        transcriptChars = try container.decodeIfPresent(Int.self, forKey: .transcriptChars)
        audioBytes = try container.decodeIfPresent(Int.self, forKey: .audioBytes)
        audioPath = try container.decodeIfPresent(String.self, forKey: .audioPath)
        transcriptPath = try container.decodeIfPresent(String.self, forKey: .transcriptPath)
        error = try container.decodeIfPresent(String.self, forKey: .error)
        detail = try container.decodeIfPresent(String.self, forKey: .detail)
    }
}
