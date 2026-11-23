{ lib, pkgs }:
let
  evalConfig = modules: (lib.evalModules {
    specialArgs.inputs.self.outPath = ../.;
    modules = [
      ../flake-module.nix
      { options.perSystem = lib.mkOption { type = lib.types.deferredModule; }; }
    ] ++ modules;
  }).config;
  appsFor = cfg: (lib.evalModules {
    specialArgs = { inherit pkgs; };
    modules = [
      { options.apps = lib.mkOption { type = lib.types.lazyAttrsOf lib.types.anything; }; }
      cfg.perSystem
    ];
  }).config.apps;
  cfg = evalConfig [
    {
      cloudflare = {
        owner = "site";
        sopsSecretsFile = ../README.md; # The help path skips SOPS.
        zones."example.com".rules.http_request_cache_settings = {
          files = {
            action = "set_cache_settings";
            expression = "true";
            action_parameters.cache = true;
          };
          early = { order = 20; action = "set_cache_settings"; expression = "true"; };
          z-first = { order = 2; action = "set_cache_settings"; expression = "true"; };
          unused = lib.mkIf false {};
        };
        zones."example.com".rules.http_request_dynamic_redirect = {};
        zones."example.com".rules.http_config_settings = lib.mkIf false {};
      };
    }
    {
      cloudflare.zones."example.com".rules.http_request_cache_settings = {
        files.description = "Files";
        a-tied = { order = 100; action = "set_cache_settings"; expression = "true"; };
      };
    }
  ];
  rules = cfg.cloudflare.zones."example.com".rules.http_request_cache_settings;
  workersOnly = evalConfig [{
    cloudflare = {
      sopsSecretsFile = ../README.md;
      workerDefaults.settings = {
        main = "source/index.js";
        workers_dev = lib.mkDefault false;
      };
      workers.example = {
        source = ./fixtures;
        settings.workers_dev = true;
      };
    };
  }];
  workerApps = appsFor workersOnly;
  disabledApps = appsFor (evalConfig []);
in
assert rules.early.order == 20 && rules.files.order == 100 && rules.files.enabled;
assert rules.files.action_parameters.cache && rules.files.description == "Files";
assert workersOnly.cloudflare.workers.example.settings == {
  name = "example";
  main = "source/index.js";
  workers_dev = true;
};
assert workerApps ? wrangler && workerApps ? sync-worker-example && !(workerApps ? sync-cloudflare);
assert lib.hasSuffix "/bin/sync-worker-example" workerApps.sync-worker-example.program;
assert !(disabledApps ? wrangler) && !(disabledApps ? sync-cloudflare);
pkgs.runCommand "cloudflare-module-check" {} ''
  ${(appsFor cfg).sync-cloudflare.program} --help > "$out" 2>launcher.log
  config=$(sed -n 's/^Using config file: //p' launcher.log)
  ${lib.getExe pkgs.jq} -e '
    .resources["example.com"].phases |
    keys == ["http_request_cache_settings", "http_request_dynamic_redirect"] and
    .http_request_dynamic_redirect.rules == [] and
    (.http_request_cache_settings.rules |
      map(.key) == ["z-first", "early", "a-tied", "files"] and
      all(.[]; has("order") | not) and last.description == "Files")
  ' "$config"
''
