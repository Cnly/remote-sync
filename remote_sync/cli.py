import argparse
import json
import logging
from pathlib import Path
import sys

import structlog

from . import utils
from .core import APIAdapter, ConfigAdapter, print_plan
from .providers.healthchecks import Healthchecks
from .providers.hetrixtools import HetrixTools
from .providers.tailscale import Tailscale
from .utils import progress


PROVIDERS = {"hetrixtools": HetrixTools, "healthchecks": Healthchecks, "tailscale": Tailscale}


def run(argv=None):

    parser = argparse.ArgumentParser(description="Preview remote resource changes and adoptions; apply only with --doit.")
    parser.add_argument("provider", choices=PROVIDERS, help="remote service provider")
    parser.add_argument("--config-file", required=True)
    parser.add_argument("--doit", action="store_true", help="apply the displayed plan")
    parser.add_argument("-v", "--verbose", action="store_true", help="show detailed API reads and ownership checks")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("-l", "--list", action="store_true", help="print the API inventory as JSON, without changing anything")
    action.add_argument("--adopt", action="append", metavar="KEY=ID", help="select a resource for adoption within the full sync plan; repeatable, preview by default")
    args = parser.parse_args(argv)

    assert not (args.list and args.doit), "--list cannot be combined with --doit."

    utils.verbose = args.verbose
    config = json.loads(Path(args.config_file).read_text())
    owner, declarations = config["owner"], config["resources"]

    adoptions = {}
    for argument in args.adopt or []:
        key, separator, remote_id = argument.partition("=")
        assert separator and remote_id and key not in adoptions, "Use distinct --adopt KEY=ID arguments."
        adoptions[key] = remote_id

    progress(f"Reading remote resources from {args.provider}...")
    remote = APIAdapter(
        provider=PROVIDERS[args.provider](), owner=owner,
        declarations=None if args.list else declarations, adoptions=adoptions,
    )

    if args.list:
        print(json.dumps(remote.inventory, indent=2))
        progress(f"Listed {len(remote.inventory)} remote resources.")
        return

    desired = ConfigAdapter(desired=remote.desired)

    progress(f"Comparing {len(declarations)} declarations with remote resources...\n")
    diff = remote.diff_from(desired)
    print_plan(diff, remote)

    if not args.doit or not diff.has_diffs():
        return

    progress("Applying changes...")
    remote.sync_from(desired, diff=diff)

    print("\nApplied.")


def main():
    # The plan is the user-facing output. Avoid verbose SDK logs and exception
    # tracebacks containing HTTP request details or write-only credentials.
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.CRITICAL))
    try:
        run()
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    except (AssertionError, RuntimeError, ValueError, KeyError, TypeError, AttributeError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
