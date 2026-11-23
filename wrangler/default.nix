{ lib, config, inputs, ... }: let

  cfg = config.cloudflare;
  inherit (lib) mkOption;
  inherit (lib.types) attrsOf deferredModule json nullOr path submodule;

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
      };
    };
    config.settings.name = lib.mkDefault name;
  };

in {

  options.cloudflare = mkOption {
    type = nullOr (submodule ({ config, ... }: {
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
    }));
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
    in lib.nameValuePair "sync-worker-${name}" (mkApp "sync-worker-${name}" {
      WORKER_NAME = name;
      WORKER_SOURCE = "${worker.source}";
      WORKER_CONFIG = (jsonFormat.generate "wrangler-${name}.json" worker.settings)
        .overrideAttrs (_: localBuildAttrs);
      WORKER_RELATIVE_SOURCE = lib.optionalString (lib.hasPrefix flakePrefix source)
        (lib.removePrefix flakePrefix source);
    })) cfg.workers;

  in {
    apps = lib.optionalAttrs (cfg != null) ({ wrangler = mkApp "wrangler" {}; } // workerApps);
  };

}
