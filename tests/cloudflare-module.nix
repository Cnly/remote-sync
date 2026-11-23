{ lib, pkgs }:
let
  cfg = (lib.evalModules {
    modules = [
      ../flake-module.nix
      {
        options.perSystem = lib.mkOption { type = lib.types.raw; };
        config.cloudflare = {
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
  }).config;
  rules = cfg.cloudflare.zones."example.com".rules.http_request_cache_settings;
  appCfg = (lib.evalModules {
    modules = [
      { options.apps = lib.mkOption { type = lib.types.attrsOf lib.types.anything; }; }
      (cfg.perSystem { inherit pkgs; })
    ];
  }).config;
in
assert rules.early.order == 20 && rules.files.order == 100 && rules.files.enabled;
assert rules.files.action_parameters.cache && rules.files.description == "Files";
pkgs.runCommand "cloudflare-module-check" {} ''
  ${appCfg.apps.sync-cloudflare.program} --help > "$out" 2>launcher.log
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
