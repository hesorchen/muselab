import AppKit
import Foundation

private struct ActivitySummary: Decodable {
    let running: Int
    let unread: Int
    let attention: Int
}

private struct MuseLabConfig {
    let token: String
    let baseURL: URL

    private static func normalizedBaseURL(_ raw: String) -> URL? {
        guard var components = URLComponents(string: raw),
              let scheme = components.scheme?.lowercased(),
              scheme == "http" || scheme == "https",
              components.host != nil,
              components.user == nil,
              components.password == nil,
              components.query == nil,
              components.fragment == nil,
              components.path.isEmpty || components.path == "/" else {
            return nil
        }
        components.scheme = scheme
        components.path = ""
        return components.url
    }

    static func load(from path: String) -> MuseLabConfig? {
        guard let contents = try? String(contentsOfFile: path, encoding: .utf8) else {
            return nil
        }
        var values: [String: String] = [:]
        for rawLine in contents.split(whereSeparator: { $0.isNewline }) {
            var line = rawLine.trimmingCharacters(in: .whitespaces)
            guard !line.isEmpty, !line.hasPrefix("#") else { continue }
            if line.hasPrefix("export ") {
                line = line.dropFirst(7).trimmingCharacters(in: .whitespaces)
            }
            guard let separator = line.firstIndex(of: "=") else { continue }
            let key = String(line[..<separator]).trimmingCharacters(in: .whitespaces)
            var value = String(line[line.index(after: separator)...])
                .trimmingCharacters(in: .whitespaces)
            if value.count >= 2,
               (value.hasPrefix("\"") && value.hasSuffix("\""))
                || (value.hasPrefix("'") && value.hasSuffix("'")) {
                value.removeFirst()
                value.removeLast()
            } else if let comment = value.firstIndex(of: "#"),
                      comment > value.startIndex,
                      value[value.index(before: comment)].isWhitespace {
                value = String(value[..<comment]).trimmingCharacters(in: .whitespaces)
            }
            values[key] = value
        }
        guard let token = values["MUSELAB_TOKEN"] ?? values["PORTAL_TOKEN"],
              !token.isEmpty else { return nil }
        if let remoteURL = values["MUSELAB_URL"], !remoteURL.isEmpty {
            guard let baseURL = normalizedBaseURL(remoteURL) else { return nil }
            return MuseLabConfig(token: token, baseURL: baseURL)
        }
        let port = Int(values["MUSELAB_PORT"] ?? values["PORTAL_PORT"] ?? "8765") ?? 8765
        guard (1...65535).contains(port),
              let baseURL = URL(string: "http://127.0.0.1:\(port)") else { return nil }
        return MuseLabConfig(token: token, baseURL: baseURL)
    }
}

private enum IndicatorState {
    case attention(Int)
    case unread(Int)
    case running(Int)
    case idle
    case offline

    var symbolName: String {
        switch self {
        case .attention: return "exclamationmark.circle.fill"
        case .unread: return "checkmark.circle.fill"
        case .running: return "arrow.triangle.2.circlepath"
        case .idle: return "circle.grid.2x2"
        case .offline: return "wifi.slash"
        }
    }

    var count: Int? {
        switch self {
        case .attention(let count), .unread(let count), .running(let count): return count
        case .idle, .offline: return nil
        }
    }

    var tooltip: String {
        switch self {
        case .attention(let count): return "MuseLab：\(count) 个任务需要处理"
        case .unread(let count): return "MuseLab：\(count) 个任务完成，待查看"
        case .running(let count): return "MuseLab：\(count) 个任务进行中"
        case .idle: return "MuseLab：暂无待查看任务"
        case .offline: return "MuseLab：服务未连接"
        }
    }

    static func from(_ summary: ActivitySummary) -> IndicatorState {
        if summary.attention > 0 { return .attention(summary.attention) }
        if summary.unread > 0 { return .unread(summary.unread) }
        if summary.running > 0 { return .running(summary.running) }
        return .idle
    }
}

private final class StatusBarController: NSObject {
    private let envPath: String
    private let statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
    private let session: URLSession
    private var timer: Timer?
    private var isFetching = false
    private var etag: String?
    private var requestIdentity: String?

    init(envPath: String) {
        self.envPath = envPath
        let configuration = URLSessionConfiguration.ephemeral
        configuration.timeoutIntervalForRequest = 3
        configuration.timeoutIntervalForResource = 5
        self.session = URLSession(configuration: configuration)
        super.init()

        if let button = statusItem.button {
            button.target = self
            button.action = #selector(openTaskCenter)
            button.sendAction(on: [.leftMouseUp])
        }
        render(.offline)
        poll()
        timer = Timer.scheduledTimer(withTimeInterval: 10, repeats: true) { [weak self] _ in
            self?.poll()
        }
    }

    private func render(_ state: IndicatorState) {
        guard let button = statusItem.button else { return }
        let image = NSImage(systemSymbolName: state.symbolName, accessibilityDescription: state.tooltip)
        image?.isTemplate = true
        button.image = image
        button.title = state.count.map { " \($0)" } ?? ""
        button.imagePosition = state.count == nil ? .imageOnly : .imageLeading
        button.toolTip = state.tooltip
        button.setAccessibilityLabel(state.tooltip)
    }

    private func poll() {
        guard !isFetching else { return }
        guard let config = MuseLabConfig.load(from: envPath) else {
            requestIdentity = nil
            etag = nil
            render(.offline)
            return
        }
        let identity = "\(config.baseURL.absoluteString)|\(config.token)"
        if requestIdentity != identity {
            requestIdentity = identity
            etag = nil
        }
        guard let url = URL(string: "/api/activity/summary", relativeTo: config.baseURL) else {
            render(.offline)
            return
        }
        var request = URLRequest(url: url)
        request.setValue(config.token, forHTTPHeaderField: "X-Auth-Token")
        if let etag { request.setValue(etag, forHTTPHeaderField: "If-None-Match") }
        isFetching = true
        session.dataTask(with: request) { [weak self] data, response, _ in
            DispatchQueue.main.async {
                guard let self else { return }
                self.isFetching = false
                guard let response = response as? HTTPURLResponse else {
                    self.etag = nil
                    self.render(.offline)
                    return
                }
                if response.statusCode == 304 { return }
                guard response.statusCode == 200, let data,
                      let summary = try? JSONDecoder().decode(ActivitySummary.self, from: data) else {
                    self.etag = nil
                    self.render(.offline)
                    return
                }
                self.etag = response.value(forHTTPHeaderField: "ETag")
                self.render(.from(summary))
            }
        }.resume()
    }

    @objc private func openTaskCenter() {
        guard let config = MuseLabConfig.load(from: envPath),
              let url = URL(string: "/?activity=1", relativeTo: config.baseURL) else { return }
        NSWorkspace.shared.open(url)
    }
}

private final class AppDelegate: NSObject, NSApplicationDelegate {
    private var controller: StatusBarController?

    func applicationDidFinishLaunching(_ notification: Notification) {
        let arguments = CommandLine.arguments
        guard let flag = arguments.firstIndex(of: "--env"), arguments.indices.contains(flag + 1) else {
            fputs("usage: MuseLabStatusBar --env /path/to/.env\n", stderr)
            NSApplication.shared.terminate(nil)
            return
        }
        controller = StatusBarController(envPath: arguments[flag + 1])
    }
}

let app = NSApplication.shared
private let delegate = AppDelegate()
app.setActivationPolicy(.accessory)
app.delegate = delegate
app.run()
