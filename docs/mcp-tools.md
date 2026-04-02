# MCP Tool Reference

All tools are available via the stdio MCP server (`src/mcp/mcp_stdio.py`) and the HTTP API (`/api/mcp/*`).

---

## cig_search

Search for functions using natural language. Uses BM25 + vector hybrid search (RRF fusion) when a DashScope API key is configured; falls back to BM25-only otherwise.

**Input**
```json
{
  "query": "handle payment failure",
  "top_k": 10
}
```

**Output** (text)
```
[1] score=12.4 | PaymentManager.handleFailure | payments | PaymentManager.swift:84
[2] score=9.1  | CheckoutVC.showPaymentError  | ui       | CheckoutViewController.swift:201
```

**Use when**: you don't know the exact function name and want to find entry points by semantics.

---

## cig_context

Get 360° context for a known function.

**Input**
```json
{
  "symbol": "PaymentManager.handleFailure",
  "branch": "master"
}
```

**Output** (text)
```
Function: PaymentManager.handleFailure
File: PaymentManager.swift:84
Domain: payments
Signature: func handleFailure(error: Error)

Called by (3):
  <- CheckoutViewController.submitPayment [CheckoutViewController.swift:145]
  <- RetryHandler.retryPayment [RetryHandler.swift:67]
  ...

Calls (2):
  -> Logger.log [Logger.swift:12] (confidence=95)
  -> AlertPresenter.showAlert [AlertPresenter.swift:44] (confidence=88)

Community: payments/core (42 members)
```

**Use when**: you know the function name and want to understand its responsibilities.

---

## cig_impact

Analyze the blast radius of modifying a function.

**Input**
```json
{
  "symbol": "NetworkClient.request",
  "max_depth": 5,
  "branch": "master"
}
```

**Output** (text)
```
Impact analysis: NetworkClient.request
18 unique callers across 7 files

Depth 1:
  APIService.fetchUser  [APIService.swift:34]
  APIService.fetchOrders [APIService.swift:78]
  ...

Depth 2:
  UserViewController.loadData [UserViewController.swift:67]
  OrdersViewController.refresh [OrdersViewController.swift:122]
  ...
```

**Use when**: assessing change risk before modifying a function, or during code review.

---

## cig_call_chain

Get the downstream call tree of a function.

**Input**
```json
{
  "symbol": "AppDelegate.applicationDidFinishLaunching",
  "max_depth": 4,
  "branch": "master"
}
```

**Output** (text)
```
Call chain: AppDelegate.applicationDidFinishLaunching

Chain 1: applicationDidFinishLaunching -> setupNetworking -> configureSession -> ...
Chain 2: applicationDidFinishLaunching -> setupAnalytics -> Analytics.initialize
...
```

**Use when**: understanding the execution flow of a function.

---

## cig_graph_stats

Show code graph health summary.

**Input**
```json
{}
```

**Output** (text)
```
=== Code Intelligence Graph Status ===
Functions: 12,453
Classes / Structs: 1,847
Files: 523
Edges: 89,204
CALLS Coverage: 71%
Last Updated: 2025-01-15T10:30:00Z

High-Risk Nodes Top 10 (most called):
  Logger.log | utilities | fanIn=847
  AlertPresenter.show | ui | fanIn=312
  ...
```

---

## HTTP API Endpoints

For direct HTTP calls (e.g. from scripts or other tools):

```bash
# Search
curl -X POST http://localhost:8080/api/mcp/search \
  -H "Content-Type: application/json" \
  -d '{"query": "handle payment", "top_k": 5}'

# Context
curl -X POST http://localhost:8080/api/mcp/context \
  -H "Content-Type: application/json" \
  -d '{"symbol_name": "PaymentManager.handleFailure"}'

# Impact scope
curl -X POST http://localhost:8080/api/mcp/impact-scope \
  -H "Content-Type: application/json" \
  -d '{"function_name": "NetworkClient.request", "max_depth": 3}'

# Call chain
curl -X POST http://localhost:8080/api/mcp/call-chain \
  -H "Content-Type: application/json" \
  -d '{"function_name": "AppDelegate.applicationDidFinishLaunching"}'

# Raw Cypher query
curl -X POST http://localhost:8080/api/mcp/cypher \
  -H "Content-Type: application/json" \
  -d '{"query": "MATCH (f:Function) RETURN count(f)"}'
```
