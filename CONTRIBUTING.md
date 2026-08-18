# Contributing

Contributions are welcome! This is a small, single-maintainer project, so the
process is deliberately light for now:

- **Found a bug, or have an idea?** Open an issue on GitHub.
- **Want to make a change?** Open a pull request. For anything non-trivial,
  opening an issue first to talk it over saves us both time.

More detailed guidelines will appear here if I get any contributions :D

## A few things worth knowing

- **License.** By contributing, you agree that your contribution is licensed
  under the project's [MIT License](LICENSE).
- **Tests.** Please run `uv run pytest` before opening a pull request, and add
  a test for any new behaviour — it is how the project stays working.
- **Plugins can live anywhere.** A new tab (say, for another memory system) can
  be its own package outside this repo and needs no changes here — see
  [docs/extending/writing-a-plugin.md](docs/extending/writing-a-plugin.md). You
  may not need to modify hobserver at all.
