# Contributing

Thank you for your interest in contributing to Code Intelligence Graph!

## Getting Started

1. Fork the repository
2. Set up your development environment (see [docs/setup.md](docs/setup.md))
3. Create a feature branch: `git checkout -b feature/my-feature`
4. Make your changes
5. Run the smoke tests: `python tests/smoke_test.py`
6. Submit a pull request

## Areas for Contribution

- **Parser improvements**: Better Swift/ObjC AST extraction, fewer false positives in call detection
- **Language support**: Adding Kotlin/Java support for Android projects
- **Search**: Embedding-based semantic search as an alternative to BM25
- **Documentation**: Tutorials, video walkthroughs, more examples
- **Testing**: More fixture files covering edge cases (generics, protocol extensions, async/await)

## Reporting Issues

Please include:
- macOS version and Xcode version
- Python version (`python3 --version`)
- The error message and full traceback
- A minimal Swift/ObjC snippet that reproduces the parsing issue (if applicable)

## Pull Request Guidelines

- Keep PRs focused — one feature or fix per PR
- Add or update tests in `tests/` if you change parsing or query logic
- Do not commit `.env`, `bm25_index.pkl`, or any generated files

## Code Style

- Python: follow PEP 8, use type hints for new functions
- Keep functions small and focused
- Add docstrings for public functions

## License

By contributing, you agree that your contributions will be licensed under the MIT License.
