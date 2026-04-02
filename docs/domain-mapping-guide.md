# Domain Mapping Guide

## What is a domain?

A **domain** is a logical feature area assigned to each function and file in your graph.
Examples: `networking`, `auth`, `ui`, `payments`, `navigation`.

Domains are used for:
- **Community detection**: grouping related functions together
- **Risk analysis**: the dashboard shows which domains have the most cross-module coupling
- **MCP context**: every `cig_context` and `cig_impact` result includes the domain of each function

## How does it work?

The system uses **longest-prefix matching** against `domain_mapping.yaml`.

Given a file at `/MyApp/Networking/APIClient.swift`:
- The key `MyApp/Networking/` matches → domain: `networking`
- The key `MyApp/` also matches, but it's shorter → ignored (longest wins)

If no prefix matches, the tool falls back to the file's **top-level directory name** under the workspace, so the tool works out-of-the-box even without a configured mapping.

## Quick start

1. **Generate a starter mapping** from your project's directory structure:

```bash
python scripts/generate_domain_mapping.py --workspace /path/to/your/ios/project
```

This scans directories containing `.swift` or `.m` files and applies heuristics
(e.g. directories containing "network" → `networking`, "auth" → `auth`).

2. **Review and customize** `domain_mapping.yaml`:

```yaml
mappings:
  MyApp/Network/:      networking
  MyApp/Auth/:         auth
  MyApp/UI/Views/:     ui
  MyApp/UI/:           ui          # fallback for other UI files
  MyApp/Payment/:      payments
  MyApp/Utils/:        utilities
```

3. **Rebuild** after changing the mapping:

```bash
python cli.py build
```

## Best practices

- **Be specific at the top level**: map `MyApp/Feature/SubFeature/` before `MyApp/Feature/`
- **Use simple names**: lowercase, no spaces, use `/` for sub-domains if needed (e.g. `navigation/routing`)
- **Group utilities together**: `extensions/`, `helpers/`, `common/` → all `utilities`
- **Don't over-granularize**: 10–20 domains is ideal for a large app; too many domains hurt community detection

## Example for a typical iOS app

```yaml
mappings:
  # Core feature domains
  MyApp/Features/Authentication/:  auth
  MyApp/Features/Payments/:        payments
  MyApp/Features/Profile/:         profile
  MyApp/Features/Feed/:            feed

  # Infrastructure
  MyApp/Network/:                  networking
  MyApp/Database/:                 data
  MyApp/Analytics/:                analytics

  # Presentation
  MyApp/UI/Components/:            ui/components
  MyApp/UI/:                       ui

  # Utilities
  MyApp/Extensions/:               utilities
  MyApp/Helpers/:                  utilities

  # Third-party integrations (inside your repo)
  MyApp/Integrations/Stripe/:      payments
  MyApp/Integrations/Firebase/:    analytics
```
