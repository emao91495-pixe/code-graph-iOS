// SampleApp.swift
// A minimal Swift fixture used by smoke_test.py to verify the parser and graph pipeline.

import Foundation

// MARK: - Protocol

protocol Networking {
    func fetchData(url: String) -> String
}

// MARK: - Service layer

class APIClient: Networking {
    func fetchData(url: String) -> String {
        let result = parseResponse(url)
        return result
    }

    private func parseResponse(_ raw: String) -> String {
        return formatOutput(raw)
    }

    private func formatOutput(_ text: String) -> String {
        return text.trimmingCharacters(in: .whitespaces)
    }
}

// MARK: - View layer

class UserViewController {
    private let client: APIClient

    init(client: APIClient = APIClient()) {
        self.client = client
    }

    func viewDidLoad() {
        loadUserData()
    }

    private func loadUserData() {
        let data = client.fetchData(url: "https://api.example.com/user")
        displayUser(data)
    }

    private func displayUser(_ data: String) {
        print(data)
    }
}

// MARK: - Utilities

struct DataFormatter {
    static func format(_ value: String) -> String {
        return value.uppercased()
    }
}
