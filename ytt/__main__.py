"""Enable ``python -m ytt`` to dispatch to the CLI."""

from ytt.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
