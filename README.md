# Remote sync

Declarative reconciliation for Cloudflare zone rules and Worker routing, Grafana alert folders and inhibition rules,
HetrixTools HTTP monitors, Healthchecks.io checks, and Tailscale Services. Each run reads the API, uses
DiffSync to compare it with configuration, and previews changes. `--doit` applies
the plan. There is no persistent state file or background reconciliation.
The flake module also provides Wrangler apps for Cloudflare Workers.
For token setup, see [Cloudflare API token permissions](#cloudflare-api-token-permissions).

## Usage

Run the configured reconciliation apps from the consuming flake.

```sh
nix run .#sync-cloudflare
nix run .#sync-hetrixtools
nix run .#sync-healthchecks
nix run .#sync-tailscale-svc
nix run .#sync-tailscale-svc -- --doit
nix run .#sync-grafana
nix run .#sync-grafana -- --doit
```

The reconciliation apps accept the following options.

| Option | Behavior |
| --- | --- |
| `--doit` | Discover, preview, and apply the full plan, including removals. |
| `-v`, `--verbose` | Show detailed API reads and ownership operations. |
| `-l`, `--list` | Print inventory keyed by remote ID. Cloudflare groups rulesets by zone and routing by Worker. Grafana groups resources under `folders` and `inhibitionRules`. |
| `--adopt KEY=ID` | Select an existing resource within the full plan; repeatable. Grafana adoption targets folders. |
| `--help` | Show help without decrypting credentials. |

Plans and inventory go to stdout; progress, retries, and errors go to stderr.
`--list` skips ownership resolution, HetrixTools private notes, and Tailscale host
approvals. It cannot be combined with `--doit` or `--adopt`.

Each collection keeps its detailed diff and resource-name summary. Previews with
multiple collections end with an overview of each collection's operation counts,
including zero-change collections. Several rule edits in one Cloudflare phase
count as one phase change. Several alert edits in one Grafana folder count as
one folder change.

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
Providers take a `sopsSecretsFile` and declarations. Providers that store ownership markers also take an `owner` namespace.

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
The Tailscale configuration is optional (`null` disables `sync-tailscale-svc`).

## Ownership and adoption

Healthchecks and Tailscale use `nix-sync-<owner>`, such as `nix-sync-flake`.
Checks are identified by slug and services by API name; plans use those identities
as labels. HetrixTools keeps `nix-sync-<owner>--<key>` and identifies owned monitors
by their declaration key.

| Provider | Marker storage | Automatic adoption matches | Declaration removed |
| --- | --- | --- | --- |
| Cloudflare rules | Rule `ref` | Explicit rule ID within zone and phase | Delete owned rule in configured phase |
| Cloudflare Worker routes and Custom Domains | Target Worker in its account | Existing bindings to the declared Worker | Delete the binding from the declared Worker |
| HetrixTools | Standalone private-note line | Exact target URL and HTTP method | Delete owned monitor |
| Healthchecks | Tag | Exact slug | Pause owned check |
| Tailscale | Standalone comment line | Exact service name (`svc:…`) | Delete owned service |
| Grafana folders | Folder `remote-sync/owner` annotation | Exact UID of a folder with no alerts | Clear alerts; retain folder and dashboards |
| Grafana inhibition rules | Immutable `nix-sync-<owner>--<key>` name | Adoption unsupported | Delete owned rule |

Adoption attaches ownership and reconciles settings in the same apply, preserving
other notes, comments, or tags used alongside the marker. Unrelated unowned
resources and other owners' resources are left alone. Conflicting ownership and
ambiguous matches abort before writes.

`--adopt KEY=ID` uses the declaration key, even when plans show a slug or service
name. The resource must match and be unowned or belong to the same owner.
HetrixTools also requires the same ownership key:

```sh
nix run .#sync-hetrixtools -- --list
nix run .#sync-hetrixtools -- --adopt web=MONITOR_ID
nix run .#sync-hetrixtools -- --adopt web=MONITOR_ID --doit
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

### Cloudflare API token permissions

Set `CLOUDFLARE_API_TOKEN` in `cloudflare.sopsSecretsFile`. In the Cloudflare
dashboard, select the permissions for the operations you use. For a token used
only for previews or `--list`, select **Read** for each applicable permission.
**Edit** includes read access. [Cloudflare permission reference](https://developers.cloudflare.com/fundamentals/api/reference/permissions/)

| Dashboard permission                  | Operation                                                                    |
| ------------------------------------- | ---------------------------------------------------------------------------- |
| Zone → Zone → Read                    | Look up zones for rules, Worker routes, or Custom Domains                    |
| Account → Workers Scripts → Read      | Read a declared Worker's live routes and Custom Domains                      |
| Account → Workers Scripts → Edit      | Deploy Worker code with Wrangler or attach, update, or remove Custom Domains |
| Zone → Workers Routes → Edit          | Create or remove ordinary Worker routes (`zoneRoutes`)                       |
| Zone → Single Redirect → Edit         | Manage Single Redirects (`http_request_dynamic_redirect`)                    |
| Zone → Cache Rules → Edit             | Manage Cache Rules (`http_request_cache_settings`)                           |
| Account → Account Rulesets → Edit     | Additional grant in Cloudflare's [Cache Rules API guide]                     |
| Account → Account Filter Lists → Edit | Additional grant in Cloudflare's [Cache Rules API guide]                     |
| Zone → Zone WAF → Edit                | Manage custom WAF rules (`http_request_firewall_custom`)                     |
| Zone → Zone WAF → Edit                | Manage rate limiting (`http_ratelimit`)                                      |
| Zone → Transform Rules → Edit         | Rewrite URLs (`http_request_transform`)                                      |
| Zone → Transform Rules → Edit         | Transform request headers (`http_request_late_transform`)                    |
| Zone → Transform Rules → Edit         | Transform response headers (`http_response_headers_transform`)               |
| Account → Account Rulesets → Read     | Read rulesets for URL and header transforms ([zone transform examples])      |
| Zone → Origin Rules → Edit            | Manage Origin Rules (`http_request_origin`)                                  |
| Zone → Config Rules → Edit            | Manage Configuration Rules (`http_config_settings`)                          |

[Cache Rules API guide]: https://developers.cloudflare.com/cache/how-to/cache-rules/create-api/#required-api-token-permissions
[zone transform examples]: https://developers.cloudflare.com/terraform/additional-configurations/transform-rules/

The zone Rulesets API used by `sync-cloudflare` lists **Cache Settings Write**
and **Zone Transform Rules Write** among its accepted grants. Cloudflare's URL
Rewrite API guide specifies **Account → Transform Rules → Edit** for URL rewrites.
[URL Rewrite API permissions](https://developers.cloudflare.com/rules/transform/url-rewrite/create-api/#required-api-token-permissions) · [Zone ruleset writes](https://developers.cloudflare.com/ruleset-engine/rulesets-api/update/)

Grant each zone-level rule permission on the zones where you declare that phase. For Worker
routing, grant **Zone → Zone → Read** on zones containing a declared or live
binding. Previewing an ordinary route also needs **Zone → Workers Routes → Read**
in its zone. Removing a stale route needs **Workers Routes → Edit** there, even
if the zone is absent from Nix. [Worker Routes API](https://developers.cloudflare.com/api/resources/workers/subresources/routes/) · [Custom Domains API](https://developers.cloudflare.com/api/resources/workers/subresources/domains/)

To **discover the account ID** automatically, scope the token to one Workers
account. If listing accounts returns HTTP 403 or an empty list, `sync-cloudflare`
can infer the account from visible zones. Wrangler needs a successful
`GET /accounts` when it has no configured or cached ID. Cloudflare's Workers token template includes
**Account → Account Settings → Read** for account access. [Token templates](https://developers.cloudflare.com/fundamentals/api/reference/template/) · [Wrangler account lookup](https://github.com/cloudflare/workers-sdk/blob/wrangler%404.156.0/packages/workers-auth/src/core/factory.ts#L413-L432)

### Cloudflare Rules

`cloudflare` defaults to `{}`. Importing the flake module exposes
`sync-cloudflare` and `wrangler`. Both require `cloudflare.sopsSecretsFile`
when Nix evaluates their programs, including during `nix flake check`.
Declare at least one zone or Worker before running `sync-cloudflare`.

Declare zones under `cloudflare.zones` to manage their entry points through
the Rulesets API. Use the zone's DNS name and the Cloudflare phase name.
The app resolves the zone ID once per run and requires one exact match
visible to the token.

```nix
cloudflare = {
  owner = "flake";
  sopsSecretsFile = ./secrets.yaml;
  zones."example.com".rules.http_request_cache_settings.media = {
    expression = ''http.host eq "example.com" and starts_with(http.request.uri.path, "/media/")'';
    action = "set_cache_settings";
    action_parameters.cache = true;
  };
};
```

`sync-cloudflare` accepts the nine zone-level phases in the
[permissions table](#cloudflare-api-token-permissions). It rejects other phase
names before discovery. Within those phases, use the API's action names and
native `action_parameters`, `ratelimit`, `logging`, and `exposed_credential_check`
objects where applicable. [Cloudflare phase reference](https://developers.cloudflare.com/ruleset-engine/reference/phases-list/)

Rules run in ascending `order` within their phase. The default is `100`, and
ties are allowed. The Nix module breaks ties by declaration key. It emits an
ordered JSON array with a `key` field on each rule and drops `order` after
sorting. Give rules distinct values when their relative order
matters. A dashboard reorder of tied managed rules can appear in the next diff.
Changing an order value only produces a diff if the resulting sequence changes.

An owned rule has a stable `nix-sync-<owner>--<key>` ref. Use `--list` to see
each zone's rulesets and their rules in execution order, including IDs and refs.
To adopt a dashboard rule explicitly, use
`--adopt example.com/http_request_cache_settings/media=RULE_ID`.
Removing a rule deletes it from its configured phase. To remove all owned rules
from a phase, set its entry to an empty set (for example,
`zones."example.com".rules.http_request_cache_settings = {};`) and apply before
dropping the phase or zone configuration.

Preview uses GET requests and shows one diff of the managed rules per phase.
On `--doit` the app validates every changed phase with `dry_run=true` before
the first live write. Each changed phase then takes one request. A new entry
point is created with its initial rules. An existing entry point receives the
complete rule list. The app fills surviving managed positions in declared
order, appends overflow, and retains dashboard and other owners' rules by ID
in their existing relative order. A rule added between discovery and apply can
be lost because the update replaces the list. Use one writer per phase during a
run. [Cloudflare ruleset updates](https://developers.cloudflare.com/ruleset-engine/rulesets-api/update/) · [Server-side validation](https://developers.cloudflare.com/ruleset-engine/rulesets-api/dry-run/)

### Cloudflare Workers

`cloudflare.workers.<name>` generates a `sync-worker-<name>` Wrangler app and
gives `sync-cloudflare` ownership of that Worker's ordinary routes and Custom
Domains. The declaration key defaults the Wrangler script
name. Use `settings.name` to target a different script. Put other Wrangler
fields such as `vars` in `settings`. The `cloudflare.workerDefaults` module
applies to every Worker.

```nix
cloudflare = {
  sopsSecretsFile = ./secrets.yaml;
  workerDefaults.settings.main = lib.mkDefault "source/index.js";
  workers.site = {
    source = ./workers/site;
    settings.compatibility_date = "2026-09-10";
    customDomains."app.example.com" = {};
    zoneRoutes."example.net" = [ "example.net/files/*" ];
  };
};
```

Both routing options default to `{}`. Leaving either empty removes the
declared Worker's live bindings of that kind on apply, including
bindings in zones absent from `cloudflare.zones`. Other Workers and scriptless
bypass routes keep their bindings. To retire a Worker's routing, apply with both
maps empty before removing its declaration. For a Custom Domain, `enabled`
defaults to true and `previews_enabled` defaults to false. Set `zone_name` or
`zone_id` to select its zone explicitly.

When multiple accounts are visible, set the Worker's `settings.account_id` or
set `CLOUDFLARE_ACCOUNT_ID` for a shared account. The per-Worker setting takes
precedence. A Workers-only configuration can omit `cloudflare.owner`.
[Cloudflare API token permissions](#cloudflare-api-token-permissions)

The provider reads the service's default environment, then lists that Worker's
ordinary routes and Custom Domains. Failure to read either inventory stops
the run before any writes. Zone route checks cover the zones in the Worker's
live route list and its `zoneRoutes` declaration. For a new Custom Domain,
an explicit zone ID selects that zone directly. Otherwise the provider uses
the declared zone name or searches the hostname and its parents for a match.
If discovery cannot verify a managed binding or its zone, it fails before
any writes. A denied ownership check or write during apply stops the run
with an error. Earlier successful writes remain in effect. [Wrangler's route lookup](https://github.com/cloudflare/workers-sdk/blob/wrangler@4.156.0/packages/deploy-helpers/src/deploy/helpers/download-worker-config.ts) · [Zone Details API](https://developers.cloudflare.com/api/resources/zones/methods/get/) · [Zone Routes API](https://developers.cloudflare.com/api/resources/workers/subresources/routes/) · [Custom Domains API](https://developers.cloudflare.com/api/resources/workers/subresources/domains/)

Deploy a new script before applying its routing. Run the routing app to preview
and remove stale routes even when the Worker code already has its latest deployment.

```sh
nix run .#wrangler -- whoami
nix run .#sync-worker-site -- dev
nix run .#sync-worker-site -- deploy
nix run .#sync-cloudflare
nix run .#sync-cloudflare -- --doit
```

The standalone `wrangler` app works with just `cloudflare.sopsSecretsFile`
and passes arguments straight to Wrangler. The per-Worker apps also pass
the generated `wrangler.json`. Run `dev` from the consuming flake root
with a source directory inside that flake to pick up working-tree edits.
`dev` receives the routing declarations for local host inference. `deploy`
uses the Nix store snapshot and leaves publishing routes and domains to
`sync-cloudflare`. Both commands share Wrangler's local state under
`.wrangler/<name>/runtime`.

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

### Grafana alerts

The optional `grafana` configuration manages dedicated alert folders and their
individual `AlertRule` resources. Set `url` to the Grafana UI URL, not a metrics
ingestion endpoint. `namespace` defaults to `default` for the first OSS organization;
Grafana Cloud uses `stacks-<STACK_ID>` (shown in the instance's `/swagger` page).
The provider discovers versions from `/apis/rules.alerting.grafana.app`, preferring
`v1beta1` and using `v0alpha1` when that is the supported version. Both use the new
AlertRule API; there is no fallback to the deprecated provisioning API. Availability
can differ between Cloud stacks running the same Grafana version.

```nix
grafana = {
  owner = "flake";
  url = "https://your-stack.grafana.net";
  namespace = "stacks-123";
  sopsSecretsFile = ./secrets.yaml;
  folders.infrastructure = {
    title = "Infrastructure";
    ruleDefaults = {
      trigger.interval = lib.mkDefault "1m";
      for = lib.mkDefault "5m";
    };
    rules.example-host-missing = {
      title = "Example host stopped reporting";
      expressions.A = {
        datasourceUID = "your-prometheus-data-source-uid";
        relativeTimeRange = { from = "10m"; to = "0s"; };
        model = {
          expr = ''absent_over_time(node_time_seconds{host="example"}[5m]) or on() vector(0)'';
          instant = true;
          range = false;
        };
      };
      expressions.B = {
        source = true;
        model = { type = "math"; expression = "$A > 0"; };
      };
    };
  };
};
```

Folder and rule `uid` values default to their attribute keys. Rule UIDs must be
globally unique and stay in the same folder; move an existing rule explicitly
before changing its declared location. The UID is the API's `metadata.name`,
distinct from its internal `metadata.uid`. `folderDefaults` and each folder's
`ruleDefaults` are modules, with normal Nix merging and priorities. Expressions
merge by their attribute keys; exactly one sets `source = true` as the condition.
Each rule's `trigger.interval` defaults to `"1m"`; no evaluation group is created.

Rule fields follow the v1beta1 spec: `keepFiringFor`, `paused`, and the state
values `Ok` and `KeepLast`. Durations, including query time ranges, are strings.
Optional fields such as `panelRef`, `missingSeriesEvalsToResolve`, and
`notificationSettings` can be declared directly. Notification settings require
`type = "SimplifiedRouting"` with `receiver`, or `type = "NamedRoutingTree"`
with `routingTree`. Omit them to use the default notification policy tree.

Ownership is `nix-sync-<owner>` on the folder. **All AlertRule resources in an
owned folder are managed**, including manually added rules. Use dedicated folders:
omitted rules are removed on apply. A matching folder with no alert rules is
adopted automatically; adopting a folder that already contains alerts requires
`--adopt KEY=FOLDER_UID`, and the preview shows its complete reconciliation.
Removing a folder declaration clears its alert rules and retains the folder,
its ownership marker, dashboards, and other metadata. An empty `rules = {}` has
the same alert-clearing behavior. RecordingRule resources and rule sequences
are not managed by this provider.

Edit managed rules in their declarations; subsequent applies reconcile UI edits.
Contact points, notification policies, data sources, and Alloy configuration are
managed separately. Uses the [v1 folder API](https://grafana.com/docs/grafana/latest/developer-resources/api-reference/http-api/folder/)
and [AlertRule API](https://grafana.com/whats-new/2026-10-22-new-alertrule-and-recordingrule-apis-for-grafana-managed-alerts/).

### Grafana notification inhibition

`grafana.inhibitionRules` declares native `InhibitionRule` resources for Grafana's
built-in Alertmanager. `nix run .#sync-grafana` previews both folders/alert rules and
inhibition rules; `nix run .#sync-grafana -- --doit` applies both plans. Discovery and
declaration validation for both resource sets finish before any writes, so an
inhibition API or permission error during discovery prevents folder changes too.
`--adopt KEY=FOLDER_UID` still selects folders within the complete plan.

The inhibition handler discovers `notifications.alerting.grafana.app`, preferring
`v1beta1` and using `v0alpha1` when available. Unsupported APIs fail with the requested endpoint;
there is no fallback to replacing the full Alertmanager configuration.

```nix
grafana.inhibitionRuleDefaults = {
  source_matchers = [{ label = "managed_by"; value = "nix"; }];
  target_matchers = [{ label = "managed_by"; value = "nix"; }];
};
grafana.inhibitionRules.unit-failed = {
  source_matchers = [{ label = "alertname"; value = "Systemd unit failed"; }];
  target_matchers = [{
    label = "alertname";
    type = "=~";
    value = "Required systemd unit is not active|Systemd service keeps restarting";
  }];
  equal = [ "host" "container" "name" ];
};
```

Matchers use the API's `label`, `type`, and `value` fields; `type` defaults to `=`.
Matchers within each list are ANDed. `inhibitionRuleDefaults` is a module, so
matcher lists concatenate and normal priorities/conditions apply. `equal` lists
the labels whose values must agree between source and target; missing and empty
values are equivalent. Omitting it allows inhibition across all label values.
Only a firing source suppresses notifications; target alert states and evaluation
are unchanged. Both alerts must reach the same Alertmanager.

The API does not retain custom ownership annotations. Resource names therefore
reserve `nix-sync-<owner>--<key>` for this app. The full name must be a lowercase
DNS name of at most 40 characters, and the owner must not contain `--`. Keep keys
stable: renaming one creates a new rule and deletes the old one. Removing a
declaration deletes its owned rule, including when `inhibitionRules = {}`. Rules
outside this owner's prefix, and rules with external provisioning provenance,
are preserved. Names are immutable, so adopting existing rules is unsupported.
Updates and deletions use resource-version preconditions and stop on conflicts.
The inhibition API is read even when declarations are empty, so removing the last
rule still reconciles its deletion.

The service account needs `alert.notifications.inhibition-rules:read`, `:write`,
and `:delete` permissions for these rules (or the broader notification read/write
permissions). Folder and alert-rule permissions alone are insufficient. See
[Grafana's inhibition documentation](https://grafana.com/docs/grafana/latest/alerting/configure-notifications/inhibition-rules/)
for API availability and notification semantics.

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
Edit the encrypted file in the consuming flake with SOPS.

```sh
sops edit ./secrets.yaml
```

Supply the keys needed by your configured providers in that encrypted file:

```yaml
CLOUDFLARE_API_TOKEN: "your-cloudflare-api-token"
HETRIXTOOLS_API_TOKEN: "your-api-token"
HETRIXTOOLS_CONTACT_LIST_ID: "your-contact-list-id"
HEALTHCHECKS_API_KEY: "your-management-api-key"
TAILSCALE_OAUTH_CLIENT_SECRET: "your-client-secret"
GRAFANA_SERVICE_ACCOUNT_TOKEN: "your-service-account-token"
```

- **Cloudflare** and Wrangler share `CLOUDFLARE_API_TOKEN`. If you also use
  octoDNS, grant DNS record access to the token configured in its Cloudflare
  provider. [Cloudflare API token permissions](#cloudflare-api-token-permissions)
- **HetrixTools:** v3 GET monitors, GET private notes, and POST private notes;
  v2 add/edit/delete monitor permissions. `contact_list_id = null` uses the file's
  contact list ID; `""` disables notifications. `--list` needs only the API token.
- **Healthchecks:** a read-write management API key for the hosts' project,
  separate from their ping key.
- **Tailscale:** an OAuth client with write access to `services` and `devices:core`.
  Only the client secret is configured. Access tokens are obtained and renewed
  in memory; the client determines the tailnet.
- **Grafana:** a Grafana instance service account with folder read/create/write
  permissions and alert-rule read/create/update/delete permissions on the managed
  folders, plus inhibition-rule read/write/delete permissions described above.
  The new API uses alert-rule permissions, rather than the legacy
  provisioning role. This is separate from Alloy's Cloud access-policy tokens
  for sending metrics and logs.

Keep credentials out of Nix values. The launcher preserves the inherited
environment, with SOPS values overriding matching variables.

## Execution and development

Discovery happens once per invocation. Worker routing also rechecks binding
ownership before updating or deleting it. Use one writer per account during a run. There is no
transaction or rollback: earlier successful writes remain after a later failure.
Preview again after failures; newly created HetrixTools monitors can be adopted
if ownership tagging failed, and Tailscale can retry unfinished host approvals.
Grafana marks a folder before writing its rules, so another preview/apply can
finish a partial apply without recreating successful resources. It includes
`resourceVersion` on updates and version/identity preconditions on rule deletes.
A conflict stops the run; preview again to review the intervening changes.
Runtime status and server metadata are excluded from configuration comparisons.

HTTP 429 retries indefinitely using the advertised cooldown, or 60 seconds when
none is usable. GET requests receiving 502/503/504 retry at most twice, provided
the delay is at most 30 seconds. Other failures stop the run. Ctrl-C prints
`Interrupted.` and exits with status 130.

[Providers](remote_sync/providers/) expose resource collections through
`collections(owner=..., declarations=..., adoptions=...)`. The default supplies
one unnamed collection; [Grafana](remote_sync/providers/grafana/) supplies named
folder/alert and inhibition collections, and routes explicit adoptions to folders.
Each collection pairs a resource handler with its declarations and adoptions.
Handlers perform API discovery and writes;
[RemoteResource](remote_sync/resources.py) supplies ownership and normalization.
The [core adapters](remote_sync/core.py) match resources and prepare settings for
DiffSync. The [CLI](remote_sync/cli.py) selects the provider, computes and displays
every plan and summary, then applies changes when requested.

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
the configured app is `sync-tailscale-svc`. Builds and flake checks run Python tests
with fake APIs and real DiffSync; they do not test live API access or alert delivery.
