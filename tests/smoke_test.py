#!/usr/bin/env python3
"""
smoke_test.py
Integration smoke test: parses the SampleApp.swift fixture and verifies the
parser produces the expected nodes and edges.

Run:
  python tests/smoke_test.py

Does NOT require Neo4j or BM25 — tests the parser layer only.
"""

import sys
import os
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

FIXTURE = Path(__file__).parent / "fixtures" / "SampleApp.swift"


def test_swift_parser():
    from src.parser.swift_parser import parse_file

    result = parse_file(str(FIXTURE))
    assert result is not None, "parse_file returned None"

    class_names = [c.name for c in result.classes]
    func_names = [f.name for f in result.functions]

    print(f"  Classes found:   {class_names}")
    print(f"  Functions found: {func_names}")

    # Verify key classes
    assert "APIClient" in class_names, f"Expected APIClient in classes, got: {class_names}"
    assert "UserViewController" in class_names, f"Expected UserViewController in classes"

    # Verify key functions
    assert "fetchData" in func_names, f"Expected fetchData in functions, got: {func_names}"
    assert "viewDidLoad" in func_names, f"Expected viewDidLoad in functions"
    assert "loadUserData" in func_names, f"Expected loadUserData in functions"

    # Verify at least one CALLS edge is detected
    assert len(result.calls) > 0, "Expected at least one call edge"

    print(f"  Calls detected:  {len(result.calls)}")
    print("  PASS: swift_parser")


def test_extractor():
    from src.parser.swift_parser import parse_file
    from src.parser.extractor import extract

    result = parse_file(str(FIXTURE))
    extraction = extract(result, domain_mapping={}, branch="main", workspace="")

    node_labels = [n.label for n in extraction.nodes]
    edge_rels = [e.rel for e in extraction.edges]

    func_nodes = [n for n in extraction.nodes if n.label == "Function"]
    class_nodes = [n for n in extraction.nodes if n.label == "Class"]
    file_nodes = [n for n in extraction.nodes if n.label == "File"]
    calls_edges = [e for e in extraction.edges if e.rel == "CALLS"]
    contains_edges = [e for e in extraction.edges if e.rel == "CONTAINS"]

    print(f"  File nodes:     {len(file_nodes)}")
    print(f"  Class nodes:    {len(class_nodes)}")
    print(f"  Function nodes: {len(func_nodes)}")
    print(f"  CONTAINS edges: {len(contains_edges)}")
    print(f"  CALLS edges:    {len(calls_edges)}")

    assert len(file_nodes) == 1, f"Expected 1 file node, got {len(file_nodes)}"
    assert len(class_nodes) >= 2, f"Expected >= 2 class nodes, got {len(class_nodes)}"
    assert len(func_nodes) >= 5, f"Expected >= 5 function nodes, got {len(func_nodes)}"
    assert len(contains_edges) > 0, "Expected at least one CONTAINS edge"

    # Without domain_mapping, domain should fall back to parent dir or 'unknown'
    for fn in func_nodes:
        assert "domain" in fn.props, "Function node missing domain property"

    print("  PASS: extractor")


def main():
    print(f"Running smoke tests against: {FIXTURE}")
    print()

    tests = [
        ("Swift parser", test_swift_parser),
        ("Extractor",    test_extractor),
    ]

    passed = 0
    failed = 0
    for name, fn in tests:
        print(f"[{name}]")
        try:
            fn()
            passed += 1
        except Exception as e:
            print(f"  FAIL: {e}")
            failed += 1
        print()

    print(f"Results: {passed} passed, {failed} failed")
    if failed > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
