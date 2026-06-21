"""``ytt`` command-line entrypoint.

Subcommands (plan: Deliverables / CLI):

- ``ytt serve``     start the MCP server (uvicorn, 1 worker — the container default)
- ``ytt test``      run the test suite (``--unit`` default, ``--integration`` opt-in);
                    emits a JSON summary to stdout
- ``ytt selftest``  run the egress probe (ip/asn/org/is_residential);
                    ``--show-sub`` prints the last decoded token ``sub`` (allowlist setup)

Exit code is 0 on success, non-zero on failure.
"""

from __future__ import annotations

import argparse
import sys
from typing import Sequence


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ytt",
        description="Remote MCP server for YouTube transcripts (captions + Whisper ASR).",
    )
    parser.add_argument("--version", action="store_true", help="print version and exit")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("serve", help="start the MCP server (uvicorn, 1 worker)")

    p_test = sub.add_parser("test", help="run the test suite and emit a JSON summary")
    grp = p_test.add_mutually_exclusive_group()
    grp.add_argument("--unit", action="store_true", help="run unit tests only (default)")
    grp.add_argument(
        "--integration",
        action="store_true",
        help="run integration tests (real YouTube/Whisper; in-cluster only)",
    )

    p_self = sub.add_parser("selftest", help="run the egress probe")
    p_self.add_argument(
        "--show-sub",
        action="store_true",
        help="print the last decoded token 'sub' (for YTT_ALLOWED_SUBJECTS setup)",
    )

    return parser


def _run_tests(integration: bool) -> int:
    """Run pytest and emit a one-line JSON summary to stdout.

    Unit by default (``-m "not integration"``); ``--integration`` selects the
    in-cluster suite (real YouTube/Whisper — fails from a datacenter IP).
    """
    import json

    import pytest

    marker = "integration" if integration else "not integration"
    code = pytest.main(["-m", marker, "-q"])
    print(json.dumps({"suite": "integration" if integration else "unit", "exit_code": int(code)}))
    return int(code)


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.version:
        from ytt import __version__

        print(__version__)
        return 0

    if args.command == "serve":
        from ytt.server import serve

        return serve()

    if args.command == "test":
        return _run_tests(integration=args.integration)

    if args.command == "selftest":
        from ytt.selftest import run_selftest

        return run_selftest(show_sub=args.show_sub)

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
