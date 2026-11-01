{ lib, config, ... }: let

  inherit (lib) mkOption;
  inherit (lib.types) anything attrsOf bool deferredModule int listOf nullOr path str submodule;

  grafanaCfg = config.grafana;
  hetrixCfg = config.hetrixtools;
  healthchecksCfg = config.healthchecks;
  tailscaleCfg = config.tailscale;

  alertRuleModule = { name, ... }: {
    # Keep API query models and optional rule fields extensible. Native module
    # merging still applies to expressions, labels, annotations, and defaults.
    freeformType = attrsOf anything;
    options = {
      uid = mkOption {
        type = str;
        default = name;
        description = "Globally unique Grafana rule UID. Keep it stable to preserve rule identity.";
      };
      title = mkOption {
        type = str;
        default = name;
        description = "Human-readable alert rule title.";
      };
      trigger.interval = mkOption {
        type = str;
        default = "1m";
        description = "Evaluation interval as a positive Grafana duration in whole seconds, such as 1m.";
      };
      expressions = mkOption {
        description = "v1beta1 expressions keyed by refId. Exactly one must set source = true.";
        type = attrsOf (submodule {
          options = {
            model = mkOption {
              type = attrsOf anything;
              description = "Data source query or Grafana expression model.";
            };
            datasourceUID = mkOption {
              type = str;
              default = "__expr__";
              description = "Data source UID; defaults to Grafana's expression engine.";
            };
            queryType = mkOption {
              type = str;
              default = "";
              description = "Query type, if required by the data source.";
            };
            relativeTimeRange = mkOption {
              default = {};
              type = submodule {
                options = lib.genAttrs [ "from" "to" ] (field: mkOption {
                  type = str;
                  default = "0s";
                  description = "${field} offset from now as a Grafana duration.";
                });
              };
              description = "Query time range as duration strings; data source queries need from greater than to.";
            };
            source = mkOption {
              type = bool;
              default = false;
              description = "Use this expression as the final alert condition.";
            };
          };
        });
      };
      for = mkOption {
        type = str;
        default = "0s";
        description = "Pending duration before firing, in Grafana duration syntax.";
      };
      keepFiringFor = mkOption {
        type = str;
        default = "0s";
        description = "Duration to keep firing after the condition clears.";
      };
      noDataState = mkOption {
        type = lib.types.enum [ "NoData" "Ok" "Alerting" "KeepLast" ];
        default = "NoData";
        description = "Grafana state when the query returns no data.";
      };
      execErrState = mkOption {
        type = lib.types.enum [ "Error" "Ok" "Alerting" "KeepLast" ];
        default = "Error";
        description = "Grafana state when evaluating the query fails.";
      };
      paused = mkOption {
        type = bool;
        default = false;
        description = "Whether evaluation of this rule is paused.";
      };
      labels = mkOption {
        type = attrsOf str;
        default = {};
        description = "Alert labels, including labels used by notification policies.";
      };
      annotations = mkOption {
        type = attrsOf str;
        default = {};
        description = "Alert annotations, such as summary and description.";
      };
    };
  };

  alertFolderModule = { config, name, ... }: {
    options = {
      uid = mkOption {
        type = str;
        default = name;
        description = "Stable Grafana folder UID; matching uses this UID, not the title.";
      };
      title = mkOption {
        type = str;
        default = name;
        description = "Grafana folder title.";
      };
      rules = mkOption {
        type = attrsOf (submodule [ alertRuleModule config.ruleDefaults ]);
        default = {};
        description = "Complete set of AlertRule resources in this dedicated folder. Omitted rules are removed; recording rules are not managed.";
      };
      ruleDefaults = mkOption {
        type = deferredModule;
        default = {};
        description = "Module applied to every alert rule in the folder; use mkDefault for overridable defaults.";
      };
    };
  };

  inhibitionRuleModule = {
    options = (lib.genAttrs [ "source_matchers" "target_matchers" ] (field: mkOption {
      description = "${field} for the InhibitionRule; matchers within each list are ANDed.";
      type = listOf (submodule {
        options = {
          label = mkOption { type = str; description = "Alert label to match."; };
          type = mkOption {
            type = lib.types.enum [ "=" "!=" "=~" "!~" ];
            default = "=";
            description = "Matcher operator; regular expressions use RE2 syntax.";
          };
          value = mkOption { type = str; description = "Label value or regular expression."; };
        };
      });
    })) // {
      equal = mkOption {
        type = listOf str;
        default = [];
        description = "Labels that must match between source and target alerts. Missing and empty values are equivalent; an empty list applies across all label values.";
      };
    };
  };

  serviceModule = { name, ... }: {
    options = {
      name = mkOption {
        type = str;
        default = "svc:${name}";
        description = "Tailscale service identity, including svc:. Matching uses this name; changing it declares a different service.";
      };
      displayName = mkOption {
        type = str;
        default = "";
        description = "Optional human-readable label; an empty string leaves the display name blank.";
      };
      ports = mkOption {
        type = listOf str;
        default = [];
        description = "Service protocol:port entries, such as tcp:443; accepted values are defined by the Tailscale API.";
      };
      tags = mkOption {
        type = listOf str;
        default = [];
        description = "Tags assigned to the service, such as tag:internal.";
      };
      devices = mkOption {
        type = listOf str;
        default = [];
        description = "Devices to approve as hosts for this service, selected by hostname, MagicDNS name, device ID, or stable node ID. Missing devices are skipped with a warning; run sync again after they join the tailnet. Other approvals are preserved.";
      };
    };
  };

  checkModule = { config, name, ... }: {
    options = {
      name = mkOption {
        type = str;
        default = config.slug;
        defaultText = lib.literalExpression "config.slug";
        description = "Initial display name for new checks, defaulting to the slug; existing names are preserved during adoption and later syncs.";
      };
      slug = mkOption {
        type = str;
        default = name;
        description = "Check identity and slug used in the host's ping URL. Matching uses this slug; changing it declares a different check and pauses the old one.";
      };
      timeout = mkOption {
        type = int;
        description = "Expected interval between pings, in seconds.";
      };
      grace = mkOption {
        type = int;
        default = 60;
        description = "Additional seconds to wait for a late ping before alerting.";
      };
    };
  };

  monitorModule = { name, ... }: {
    options = {
      name = mkOption {
        type = str;
        default = name;
        description = ''
          Readable monitor name shown in reports and status pages; defaults to the
          declaration key. Adoption matches target and HTTP method independently
          of this name. Ownership is stored separately in private notes.
        '';
      };
      target = mkOption {
        type = str;
        description = "Public HTTP URL to monitor. An unowned monitor with exactly this target and http_method is adopted on apply.";
      };
      http_method = mkOption {
        type = str;
        default = "GET";
        description = "HTTP request method used to check the target (GET or HEAD).";
      };
      keyword = mkOption {
        type = str;
        default = "";
        description = "Required response-body substring; an empty string clears the check.";
      };
      accepted_http_codes = mkOption {
        type = listOf int;
        default = [ 200 ];
        description = "HTTP status codes that count as an online response.";
      };
      timeout = mkOption {
        type = int;
        default = 10;
        description = "Time in seconds allowed for the target to load before a check fails.";
      };
      check_frequency = mkOption {
        type = int;
        default = 1;
        description = "Interval in minutes between uptime checks.";
      };
      number_of_tries = mkOption {
        type = int;
        default = 3;
        description = "Failed attempts per location before it considers the target offline.";
      };
      triggering_locations = mkOption {
        type = nullOr int;
        default = null;
        description = "Number of failing locations before alerting; null uses a majority of configured locations.";
      };
      locations = mkOption {
        type = listOf str;
        default = [];
        description = "Selected v3 location names (for example new_york); the adapter translates them to v2 write codes.";
      };
      contact_list_id = mkOption {
        type = nullOr str;
        default = null;
        description = "Contact list ID. Null reads HETRIXTOOLS_CONTACT_LIST_ID from sopsSecretsFile; an explicit empty string disables notifications.";
      };
      category = mkOption {
        type = str;
        default = "";
        description = "Monitor category; an empty string leaves the monitor uncategorized.";
      };
      alert_after_minutes = mkOption {
        type = int;
        default = 0;
        description = "Minutes of downtime before alerting; zero adds no delay. Must be a multiple of check_frequency.";
      };
      repeat_alert_times = mkOption {
        type = int;
        default = 0;
        description = "Extra downtime alerts while the monitor stays offline; zero disables repeats.";
      };
      repeat_alert_frequency = mkOption {
        type = int;
        default = 0;
        description = ''
          Minutes between repeated downtime alerts; requires repeat_alert_times > 0.
          Zero disables repeats. Must be a multiple of check_frequency.
        '';
      };
      public_report = mkOption {
        type = bool;
        default = false;
        description = "Whether anyone can view this monitor's uptime report.";
      };
      public_target = mkOption {
        type = bool;
        default = false;
        description = "Whether the uptime report displays the monitored URL.";
      };
      verify_ssl_certificate = mkOption {
        type = bool;
        default = true;
        description = "Whether HTTPS checks require a valid TLS certificate.";
      };
      verify_ssl_hostname = mkOption {
        type = bool;
        default = true;
        description = "Whether HTTPS checks require the certificate to match the target hostname.";
      };
      max_redirects = mkOption {
        type = int;
        default = 5;
        description = "Maximum HTTP redirects to follow; zero disables following redirects.";
      };
    };
  };

in {

  options = {
    grafana = mkOption {
      default = null;
      description = "Grafana alert-folder and notification-inhibition reconciliation; configure this to expose the sync-grafana app.";
      type = nullOr (submodule ({ config, ... }: {
        options = {
          owner = mkOption {
            type = str;
            description = "Stable namespace stored in the folder's remote-sync/owner annotation.";
          };
          url = mkOption {
            type = str;
            description = "Grafana instance URL, including any subpath; this is not the Prometheus remote-write URL.";
          };
          namespace = mkOption {
            type = str;
            default = "default";
            description = "Grafana API namespace: default for the first OSS organization, stacks-<STACK_ID> for Grafana Cloud.";
          };
          sopsSecretsFile = mkOption {
            type = path;
            description = "SOPS-encrypted YAML file supplying GRAFANA_SERVICE_ACCOUNT_TOKEN at runtime.";
          };
          folders = mkOption {
            type = attrsOf (submodule [ alertFolderModule config.folderDefaults ]);
            default = {};
            description = "Dedicated alert folders matched by UID. Removing a declaration clears its alerts but retains the folder and dashboards.";
          };
          folderDefaults = mkOption {
            type = deferredModule;
            default = {};
            description = "Module applied to every folder; use mkDefault for overridable defaults.";
          };
          inhibitionRules = mkOption {
            type = attrsOf (submodule [ inhibitionRuleModule config.inhibitionRuleDefaults ]);
            default = {};
            description = "Notification inhibition rules managed alongside alert folders by the sync-grafana app. Keys determine immutable names nix-sync-<owner>--<key>, which must be lowercase DNS names of at most 40 characters. Removing a declaration deletes its owned rule; unowned and externally provisioned rules are preserved.";
          };
          inhibitionRuleDefaults = mkOption {
            type = deferredModule;
            default = {};
            description = "Module applied to each inhibition rule; matcher and equal lists concatenate unless overridden with module priorities.";
          };
        };
      }));
    };
    tailscale = mkOption {
      default = null;
      description = "Tailscale reconciliation; currently manages Services and their host approvals.";
      type = nullOr (submodule ({ config, ... }: {
        options = {
          owner = mkOption {
            type = str;
            description = "Stable namespace in nix-sync-<owner> markers stored as separate lines in service comments.";
          };
          sopsSecretsFile = mkOption {
            type = path;
            description = "SOPS-encrypted YAML file supplying TAILSCALE_OAUTH_CLIENT_SECRET at runtime. The OAuth client selects the tailnet.";
          };
          services = mkOption {
            type = attrsOf (submodule [ serviceModule config.serviceDefaults ]);
            default = {};
            description = "Tailscale Services matched by API name. Attribute keys organize declarations and supply the default name.";
          };
          serviceDefaults = mkOption {
            type = deferredModule;
            default = {};
            description = "Module applied to every service; use mkDefault for overridable defaults.";
          };
        };
      }));
    };
    healthchecks = mkOption {
      default = null;
      description = "Healthchecks.io reconciliation; configure this to expose the sync-healthchecks app.";
      type = nullOr (submodule ({ config, ... }: {
        options = {
          owner = mkOption {
            type = str;
            description = "Stable namespace in nix-sync-<owner> ownership tags.";
          };
          sopsSecretsFile = mkOption {
            type = path;
            description = "SOPS-encrypted YAML file supplying HEALTHCHECKS_API_KEY at runtime.";
          };
          checks = mkOption {
            type = attrsOf (submodule [ checkModule config.checkDefaults ]);
            default = {};
            description = "Heartbeat checks matched by unique slug. Attribute keys organize declarations and supply the default slug.";
          };
          checkDefaults = mkOption {
            type = deferredModule;
            default = {};
            description = "Module applied to every check; use mkDefault for overridable defaults.";
          };
        };
      }));
    };
    hetrixtools = mkOption {
      default = {};
      type = submodule ({ config, ... }: {
        options = {
          owner = mkOption {
            type = str;
            description = ''
              Stable namespace in HetrixTools private-note markers, using
              nix-sync-<owner>--<resource-key>. Changing this value does not
              transfer ownership; adopt resources explicitly instead.
            '';
          };
          sopsSecretsFile = mkOption {
            type = path;
            description = "Required SOPS-encrypted YAML file supplying credentials at runtime, like octoDNS.";
          };
          httpMonitors = mkOption {
            type = attrsOf (submodule [ monitorModule config.httpDefaults ]);
            default = {};
            description = "HTTP monitors keyed by stable resource names.";
          };
          httpDefaults = mkOption {
            type = deferredModule;
            default = {};
            description = "Module applied to each HTTP monitor; use mkDefault for overridable defaults.";
          };
        };
      });
    };
  };

  config.perSystem = { pkgs, ... }: let

    jsonFormat = pkgs.formats.json {};

    localDrvArgs = {
      allowSubstitutes = false;
      preferLocalBuild = true;
    };

    remote-sync = pkgs.python3Packages.callPackage ./package.nix {};

    mkLauncherApp = {
      provider, owner, resources, sopsFile, settings ? {},
    }: {
      type = "app";
      program = lib.getExe (pkgs.writeShellApplication {
        name = "sync-${provider}";
        derivationArgs = localDrvArgs;
        runtimeInputs = [ pkgs.sops pkgs.bash ];
        runtimeEnv = {
          REMOTE_SYNC = lib.getExe remote-sync;
          REMOTE_SYNC_PROVIDER = provider;
          REMOTE_SYNC_CONFIG_FILE = (jsonFormat.generate "${provider}.json" {
            inherit owner resources settings;
          }).overrideAttrs (_: localDrvArgs);
          SECRETS_FILE = sopsFile;
        };
        text = builtins.readFile ./launcher.sh;
      });
    };

  in {

    apps.sync-grafana = lib.mkIf (grafanaCfg != null) (mkLauncherApp {
      provider = "grafana";
      inherit (grafanaCfg) owner;
      settings = { inherit (grafanaCfg) url namespace; };
      resources = {
        folders = lib.mapAttrs (_: folder: {
          inherit (folder) uid title;
          rules = lib.attrValues folder.rules;
        }) grafanaCfg.folders;
        inherit (grafanaCfg) inhibitionRules;
      };
      sopsFile = grafanaCfg.sopsSecretsFile;
    });

    apps.sync-tailscale-svc = lib.mkIf (tailscaleCfg != null) (mkLauncherApp {
      provider = "tailscale";
      inherit (tailscaleCfg) owner;
      resources = tailscaleCfg.services;
      sopsFile = tailscaleCfg.sopsSecretsFile;
    });

    apps.sync-hetrixtools = mkLauncherApp {
      provider = "hetrixtools";
      inherit (hetrixCfg) owner;
      resources = hetrixCfg.httpMonitors;
      sopsFile = hetrixCfg.sopsSecretsFile;
    };

    apps.sync-healthchecks = mkLauncherApp {
      provider = "healthchecks";
      inherit (healthchecksCfg) owner;
      resources = healthchecksCfg.checks;
      sopsFile = healthchecksCfg.sopsSecretsFile;
    };

  };

}
