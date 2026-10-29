# Remote sync

Declarative reconciliation for HetrixTools HTTP monitors, Healthchecks.io checks,
and Tailscale Services. Each run reads the API, uses DiffSync to compare it with
configuration, and previews changes. `--doit` applies the plan. There is no
persistent state file or background reconciliation.

## Usage

Run configured apps from the consuming flake (this repository's root):

```sh
nix run .#hetrixtools
nix run .#healthchecks
nix run .#tailscale-svc
nix run .#tailscale-svc -- --doit
```

All apps accept:

| Option | Behavior |
| --- | --- |
| `--doit` | Discover, preview, and apply the full plan, including removals. |
| `-v`, `--verbose` | Show detailed API reads and ownership operations. |
| `-l`, `--list` | Print provider inventory as JSON keyed by remote ID. |
| `--adopt KEY=ID` | Select an existing resource within the full plan; repeatable. |
| `--help` | Show help without decrypting credentials. |

Plans and inventory go to stdout; progress, retries, and errors go to stderr.
`--list` skips ownership resolution, HetrixTools private notes, and Tailscale host
approvals. It cannot be combined with `--doit` or `--adopt`.

## Flake configuration

The standalone flake exports `packages.remote-sync`, `packages.default`,
`devShells.default`, and `flakeModules.default`. It uses `systems`, `flake-parts`,
and `nixpkgs` unstable; the consuming flake can override those inputs:

```nix
inputs.remote-sync.url = "path:./remote-sync";
inputs.remote-sync.inputs.systems.follows = "systems";
inputs.remote-sync.inputs.flake-parts.follows = "flake-parts";
inputs.remote-sync.inputs.nixpkgs.follows = "nixpkgs";
```

Import `inputs.remote-sync.flakeModules.default` in your flake-parts modules.
Each provider takes an `owner` namespace, a `sopsSecretsFile`, and declarations:

```nix
{ lib, ... }: {
  hetrixtools = {
    owner = "flake";
    sopsSecretsFile = ./secrets.yaml;
    httpDefaults.locations = lib.mkDefault [ "new_york" "singapore" ];
    httpMonitors.web = {
      name = "Website";
      target = "https://example.com/health";
    };
  };
  healthchecks = {
    owner = "flake";
    sopsSecretsFile = ./secrets.yaml;
    checks.server.timeout = 120;
  };
  tailscale = {
    owner = "flake";
    sopsSecretsFile = ./secrets.yaml;
    services.web = {
      ports = [ "tcp:443" ];
      devices = [ "server" ];
    };
  };
}
```

`httpDefaults`, `checkDefaults`, and `serviceDefaults` are modules applied to each
resource. Use `lib.mkDefault` for overridable defaults; normal module merging,
list concatenation, and priorities apply. See [flake-module.nix](flake-module.nix)
for all fields and defaults. Provider field rules are left to the APIs.
The Tailscale configuration is optional (`null` disables `tailscale-svc`).

## Ownership and adoption

Healthchecks and Tailscale use `nix-sync-<owner>`, such as `nix-sync-flake`.
Checks are identified by slug and services by API name; plans use those identities
as labels. HetrixTools keeps `nix-sync-<owner>--<key>` and identifies owned monitors
by their declaration key.

| Provider | Marker storage | Automatic adoption matches | Declaration removed |
| --- | --- | --- | --- |
| HetrixTools | Standalone private-note line | Exact target URL and HTTP method | Delete owned monitor |
| Healthchecks | Tag | Exact slug | Pause owned check |
| Tailscale | Standalone comment line | Exact service name (`svc:…`) | Delete owned service |

Adoption attaches ownership and reconciles settings in the same apply, preserving
other notes, comments, or tags used alongside the marker. Unrelated unowned
resources and other owners' resources are left alone. Conflicting ownership and
ambiguous matches abort before writes.

`--adopt KEY=ID` uses the declaration key, even when plans show a slug or service
name. The resource must match and be unowned or belong to the same owner.
HetrixTools also requires the same ownership key:

```sh
nix run .#hetrixtools -- --list
nix run .#hetrixtools -- --adopt web=MONITOR_ID
nix run .#hetrixtools -- --adopt web=MONITOR_ID --doit
```

This still reconciles every declaration and removal. Explicit adoption can
disambiguate HetrixTools monitors; Healthchecks still requires unique slugs.

Healthchecks and Tailscale declaration keys can change while the effective slug
or service name stays the same. Changing a slug/service name declares a different
identity: the old check is paused or the old service deleted. To retain the
resource through a rename, rename it remotely and change the declaration before
the next sync. Defaults derive identities from keys, so set them explicitly when
renaming only a key.

Existing keyed Healthchecks/Tailscale markers for this owner are shortened on
apply as an Update, preserving other tags/comment text, check history, and service
addresses. Matching already uses the slug/service name during this migration.

Keep HetrixTools keys stable; renaming one requires editing its existing marker
or removing the marker and adopting under the new key.
Changing `owner` alone does not transfer resources. To stop managing a resource
without deleting it, remove both its declaration and marker before applying.

## Provider behavior

This repository's [deployment module](../deployments/remote-sync/default.nix)
supplies shared defaults and collects host configuration. Collected fields use
`mkDefault`, so explicit declarations can override them.

### HetrixTools

`name` defaults to the declaration key and controls the public display name.
Once owned, changes to name, target, or method update the monitor while retaining
its ID.
Automatic adoption ignores the display name; URLs must match exactly, including
case and trailing slashes. Ownership stays in private notes.

Locations use v3 names, translated to v2 write codes by the provider. This
repository defaults to New York, San Francisco, Singapore, and Tokyo, alerts
after three minutes, and enables public reports. Categories are empty unless
declared; d0nuts declarations set `d0nuts.im`.

### Healthchecks

`slug` defaults to the key. `name` defaults to the slug and is set only on
creation; existing names, UUIDs, history, and integrations are preserved. New
checks attach all existing project integrations. The module's default `grace` is
60 seconds.

Host collection includes enabled `services.healthchecks-ping` configurations
using `https://hc-ping.com` with `pingKey` or `pingKeyRuntimeFile` (slug-based
pings). The host alias is the key, `pingUUIDOrSlug` is the slug, `intervalSec`
is the timeout, and **`intervalSec + 60` is the grace period**. Ping credentials
are not included in the generated configuration.

Missing declarations pause owned checks; returning declarations resume the same
checks. Both are Update operations through the `paused` attribute. Managed checks
use `manual_resume = true` so lingering pings cannot resume a paused check.
Healthchecks sync never deletes checks.

### Tailscale Services

`name` defaults to `svc:<key>`; `displayName` defaults to an empty string.
Updates preserve comments and assigned IP addresses. `devices` selects hosts by
hostname, short or full MagicDNS name, device ID, or node ID. Missing devices are
skipped with a warning, so services can be created before hosts join the
tailnet. Run sync again after they join to approve them. Ambiguous device matches
still abort discovery. Pending approvals appear as updates; apply approves them.
Removing a device from the list does not revoke its approval.

Host collection requires both Tailscale and Serve enabled:

- `services.tailscale.serve.custom` collects `svc:` entries, excluding
  `target = "off"`. HTTP, HTTPS, TCP, and TLS-terminated TCP flags supply TCP ports;
  the default is `tcp:443`. For `tun = true`, declare remote ports explicitly.
- `services.tailscale.serve.services` uses endpoint keys as ports. An explicit
  `serve.configFile` replaces these declarations and is not read by the collector.

The key is the service name without `svc:`. Devices come from
`services.tailscale.extraFlags.hostname`, falling back to `networking.hostName`.
Repeated service names merge ports and devices across hosts.

## Credentials

Configured apps decrypt `sopsSecretsFile` at runtime with `sops exec-env`.
In this repository, edit `deployments/remote-sync/secrets.yaml` with SOPS:

```sh
sops deployments/remote-sync/secrets.yaml
```

Supply the keys needed by your configured providers in that encrypted file:

```yaml
HETRIXTOOLS_API_TOKEN: "your-api-token"
HETRIXTOOLS_CONTACT_LIST_ID: "your-contact-list-id"
HEALTHCHECKS_API_KEY: "your-management-api-key"
TAILSCALE_OAUTH_CLIENT_SECRET: "your-client-secret"
```

- **HetrixTools:** v3 GET monitors, GET private notes, and POST private notes;
  v2 add/edit/delete monitor permissions. `contact_list_id = null` uses the file's
  contact list ID; `""` disables notifications. `--list` needs only the API token.
- **Healthchecks:** a read-write management API key for the hosts' project,
  separate from their ping key.
- **Tailscale:** an OAuth client with write access to `services` and `devices:core`.
  Only the client secret is configured. Access tokens are obtained and renewed
  in memory; the client determines the tailnet.

Keep credentials out of Nix values. The launcher preserves the inherited
environment, with SOPS values overriding matching variables.

## Execution and development

Discovery happens once per invocation; apply uses that snapshot without
rereading resources. Use one writer per account during a run. There is no
transaction or rollback: earlier successful writes remain after a later failure.
Preview again after failures; newly created HetrixTools monitors can be adopted
if ownership tagging failed, and Tailscale can retry unfinished host approvals.

HTTP 429 retries indefinitely using the advertised cooldown, or 60 seconds when
none is usable. GET requests receiving 502/503/504 retry at most twice, provided
the delay is at most 30 seconds. Other failures stop the run. Ctrl-C prints
`Interrupted.` and exits with status 130.

[Providers](remote_sync/providers/) handle API discovery and writes;
[RemoteResource](remote_sync/resources.py) supplies ownership and normalization.
[Core adapters](remote_sync/core.py) match resources and prepare settings for
DiffSync, which compares models and invokes callbacks to create, update, or delete
resources.

From this directory:

```sh
nix run -- tailscale --help
nix build
nix flake check
```

Enter the development shell to run tests and the CLI against the working tree:

```sh
nix develop
python -m pytest
python -m remote_sync.cli tailscale --help
```

The standalone CLI takes a provider name and `--config-file`; configured apps
supply both and load SOPS credentials. Its provider name is `tailscale`, while
the configured app is `tailscale-svc`. Builds and flake checks run Python tests
with fake APIs and real DiffSync; they do not test live API access or alert delivery.
