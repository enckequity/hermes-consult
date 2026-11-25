"""Website consultation plugin: answers visitor questions from the business's published facts
and files contact requests on the kanban board.

It registers one CLI command, ``hermes consult`` (``serve`` and ``status``). It registers no
agent tools, hooks, middleware, dashboard routes or toolset.
"""

import json
import sys
from pathlib import Path

# Set by register(): the plugin context whose ``ctx.llm`` answers consultations and whose
# settings drive the service. Probe/validation contexts may not provide ``llm``.
_hermes_consult_ctx = None


def _root():
    root = str(Path(__file__).resolve().parent)
    if root not in sys.path:
        sys.path.insert(0, root)
    return root


def configure(parser):
    actions = parser.add_subparsers(dest="consult_action")
    serve = actions.add_parser(
        "serve", help="Run the consultation service; it follows the plugin settings and sites.json"
    )
    serve.add_argument("--interval", type=float, default=5.0, help="Seconds between config checks")
    actions.add_parser("status", help="Print the service status file")


def main(args):
    _root()
    from hermes_constants import get_hermes_home

    home = get_hermes_home()
    action = getattr(args, "consult_action", None)
    if action == "serve":
        from connector.runtime import HermesRuntime
        from connector.service import SETTINGS, serve

        ctx = _hermes_consult_ctx
        if ctx is None:
            raise SystemExit("Enable the plugin, then run this as `hermes consult serve`.")
        serve(
            lambda: {key: ctx.get_config(key) for key in SETTINGS},
            home,
            HermesRuntime,
            interval=max(1.0, float(args.interval)),
        )
        return 0
    if action == "status":
        try:
            print((Path(home) / "consult" / "status.json").read_text(encoding="utf-8"))
        except FileNotFoundError:
            print(json.dumps({"running": False, "errors": ["the service has not run yet"]}))
        return 0
    print("usage: hermes consult {serve,status}")
    return 2


def register(ctx):
    global _hermes_consult_ctx
    _hermes_consult_ctx = ctx
    _root()
    ctx.register_cli_command(
        name="consult",
        help="Website consultation service",
        setup_fn=configure,
        handler_fn=main,
    )

    def shutdown():
        global _hermes_consult_ctx
        _hermes_consult_ctx = None

    ctx.on_unload(shutdown)
