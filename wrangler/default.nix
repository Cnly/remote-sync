{ lib, config, inputs, ... }: let

  cfg = config.cloudflare;
  inherit (lib) mkOption;
  inherit (lib.types) attrsOf bool deferredModule json listOf nullOr path str submodule;

  domainModule.options = {
    enabled = mkOption { type = bool; default = true; description = "Route this hostname to the Worker."; };
    previews_enabled = mkOption { type = bool; default = false; description = "Enable preview subdomains."; };
    zone_name = mkOption { type = nullOr str; default = null; description = "Optional containing zone name."; };
    zone_id = mkOption { type = nullOr str; default = null; description = "Optional containing zone ID."; };
  };

  workerModule = { name, ... }: {
    options = {
      source = mkOption {
        type = path;
        description = "Worker directory, linked as source/ in the generated Wrangler project.";
      };
      settings = mkOption {
        type = attrsOf json;
        default = {};
        description = "Wrangler configuration. Source paths start with source/.";
        apply = settings:
          if lib.any (s: s ? route || s ? routes) ([ settings ] ++ lib.attrValues (settings.env or {}))
          then throw "Move Wrangler route/routes settings to cloudflare.workers.${name}.zoneRoutes or customDomains."
          else settings;
      };
      customDomains = mkOption {
        type = attrsOf (submodule domainModule);
        default = {};
        description = "Complete set of Custom Domains for this Worker, keyed by hostname.";
      };
      zoneRoutes = mkOption {
        type = attrsOf (listOf str);
        default = {};
        description = "Complete set of ordinary routes for this Worker, keyed by DNS zone name.";
      };
    };
    config.settings.name = lib.mkDefault name;
  };

in {

  options.cloudflare = mkOption {
    type = submodule ({ config, ... }: {
      options = {
        workerDefaults = mkOption {
          type = deferredModule;
          default = {};
          description = "Module applied to every declared Worker.";
        };
        workers = mkOption {
          type = attrsOf (submodule [ workerModule config.workerDefaults ]);
          default = {};
          description = "Cloudflare Workers keyed by the name of their sync-worker-<name> app.";
        };
      };
    });
  };

  config.perSystem = { pkgs, ... }: let

    localBuildAttrs = {
      allowSubstitutes = false;
      preferLocalBuild = true;
    };
    jsonFormat = pkgs.formats.json {};
    mkApp = name: environment: let
      launcher = pkgs.writeShellApplication {
        inherit name;
        derivationArgs = localBuildAttrs;
        runtimeInputs = [ pkgs.bash pkgs.coreutils pkgs.sops ];
        runtimeEnv = {
          WRANGLER_BIN = lib.getExe pkgs.wrangler;
          WRANGLER_SECRETS_FILE = "${cfg.sopsSecretsFile}";
        } // environment;
        text = builtins.readFile ./launcher.sh;
      };
    in {
      type = "app";
      program = lib.getExe launcher;
    };

    workerApps = lib.mapAttrs' (name: worker: let
      source = toString worker.source;
      flakePrefix = "${inputs.self.outPath}/";
      routes = lib.mapAttrsToList (pattern: domain:
        lib.filterAttrs (_: v: v != null) domain // { inherit pattern; custom_domain = true; }
      ) worker.customDomains ++ lib.concatLists (lib.mapAttrsToList (zone_name: patterns:
        map (pattern: { inherit pattern zone_name; }) patterns
      ) worker.zoneRoutes);
      # Wrangler defaults workers_dev from its route list. Production routing is
      # published by sync-cloudflare, while dev can still infer a local host.
      deploySettings = lib.optionalAttrs (routes != []) { workers_dev = false; } // worker.settings;
      configFile = suffix: settings: (jsonFormat.generate "wrangler-${name}${suffix}.json" settings)
        .overrideAttrs (_: localBuildAttrs);
    in lib.nameValuePair "sync-worker-${name}" (mkApp "sync-worker-${name}" {
      WORKER_NAME = name;
      WORKER_SOURCE = "${worker.source}";
      WORKER_CONFIG = configFile "" deploySettings;
      WORKER_DEV_CONFIG = configFile "-dev" (worker.settings // lib.optionalAttrs (routes != []) { inherit routes; });
      WORKER_RELATIVE_SOURCE = lib.optionalString (lib.hasPrefix flakePrefix source)
        (lib.removePrefix flakePrefix source);
    })) cfg.workers;

  in {
    apps = { wrangler = mkApp "wrangler" {}; } // workerApps;
  };

}
