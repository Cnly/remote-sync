import argparse
import difflib
import json
import logging
from pathlib import Path
import sys

import structlog

from . import utils
from .core import APIAdapter, ConfigAdapter, Resource
from .providers import Collection
from .providers.grafana import Grafana
from .providers.healthchecks import Healthchecks
from .providers.hetrixtools import HetrixTools
from .providers.tailscale import Tailscale
from .utils import progress


PROVIDERS = {
    "grafana": Grafana,
    "hetrixtools": HetrixTools,
    "healthchecks": Healthchecks,
    "tailscale": Tailscale,
}


def reconcile(collections: dict[str, Collection], *, owner, list_only=False, doit=False):
    """List collections or prepare and display every plan before applying any.

    Each collection retains its own identity and ownership rules. All discovery,
    validation and diff calculation must succeed before writes begin. Apply is
    sequential; a failure leaves earlier successful writes in place.
    """
    remotes = {
        name: APIAdapter(provider=collection.provider, owner=owner,
                         declarations=None if list_only else collection.declarations,
                         adoptions=collection.adoptions)
        for name, collection in collections.items()
    }

    if list_only:
        inventories = {name: remote.inventory for name, remote in remotes.items()}
        print(json.dumps(inventories.get("", inventories), indent=2))
        progress(f"Listed {sum(len(inventory) for inventory in inventories.values())} remote resources.")
        return

    plans = []
    for name, remote in remotes.items():
        desired = ConfigAdapter(desired=remote.desired)
        plans.append((name, remote, desired, remote.diff_from(desired)))

    count = sum(len(collection.declarations) for collection in collections.values())
    progress(f"Comparing {count} declarations with remote resources...\n")
    summaries = {}
    for name, remote, _, diff in plans:
        if summaries:
            print()
        if name:
            print(f"=== {name} ===\n")
        summaries[name] = print_plan(diff, remote, collection=name)
    if len(summaries) > 1:
        print_overall_summary(summaries)

    if not doit or not any(diff.has_diffs() for _, _, _, diff in plans):
        return

    progress("Applying changes...")
    for _, remote, desired, diff in plans:
        if diff.has_diffs():
            remote.sync_from(desired, diff=diff)

    print("\nApplied.")


def print_plan(diff, remote=None, *, collection=""):
    """Print one detailed plan and return its resource names grouped by action."""
    summary = {"adopt": [], "create": [], "update": [], "delete": []}
    if not diff.has_diffs():
        print("No changes.")
        return summary
    prefix = f"({collection}) " if collection else ""
    action_order = {"create": 1, "update": 2, "delete": 3}
    changes = (change for change in diff.get_children() if change.action)
    for change in sorted(changes, key=lambda change: (
        0 if remote and change.name in remote.pending_adoptions else action_order[change.action]
    )):
        if remote and change.name in remote.pending_adoptions:
            summary["adopt"].append(change.name)
            print(f"ADOPT {prefix}{change.name} ({remote.get(Resource, change.name).remote_id})")
        else:
            summary[change.action].append(change.name)
            print(f"{change.action.upper()} {prefix}{change.name}")
        desired = change.source_attrs.get("settings", {}) if change.source_attrs else {}
        live = change.dest_attrs.get("settings", {}) if change.dest_attrs else {}
        for line in difflib.unified_diff(
            json.dumps(live, indent=2, sort_keys=True).splitlines(),
            json.dumps(desired, indent=2, sort_keys=True).splitlines(),
            fromfile="remote", tofile="config", lineterm="",
        ):
            print(f" |  {line}")
        print()
    print("Summary:")
    for category, names in summary.items():
        print(f"  {category.capitalize()} ({len(names)}):{'' if names else ' none'}")
        for name in names:
            print(f"    {name}")
    return summary


def print_overall_summary(summaries):
    rows = [["Collection", "Adopt", "Create", "Update", "Delete"]]
    for name, summary in summaries.items():
        rows.append([name, *(str(len(names)) for names in summary.values())])
    widths = [max(map(len, column)) for column in zip(*rows)]
    print("\nOverall summary:")
    for row in rows:
        print("  " + row[0].ljust(widths[0]) + "  " + "  ".join(
            value.rjust(width) for value, width in zip(row[1:], widths[1:])
        ))


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

    provider = PROVIDERS[args.provider](**config.get("settings", {}))
    collections = provider.collections(owner=owner, declarations=declarations, adoptions=adoptions)
    progress(f"Reading remote resources from {args.provider}...")
    reconcile(collections, owner=owner, list_only=args.list, doit=args.doit)


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
