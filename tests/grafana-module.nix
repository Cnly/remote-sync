{ lib, pkgs }:
let
  cfg = (lib.evalModules {
    modules = [
      ../flake-module.nix
      ({ lib, ... }: {
        options.perSystem = lib.mkOption { type = lib.types.raw; };
        config.grafana = {
          owner = "test";
          url = "https://example.grafana.net";
          namespace = "stacks-123";
          sopsSecretsFile = ../README.md; # Never executed; only build the launcher.
          folderDefaults.ruleDefaults = {
            for = lib.mkDefault "5m";
            trigger.interval = lib.mkDefault "2m";
            labels.managed_by = "nix";
            expressions.A = {
              datasourceUID = "prometheus";
              relativeTimeRange.from = "10m";
              model = { expr = "up"; instant = true; };
            };
          };
          folders.infrastructure = {
            ruleDefaults.labels.severity = lib.mkDefault "warning";
            rules = {
              host-missing = {
                for = "10m";
                trigger.interval = "30s";
                labels.severity = "critical";
                expressions.A.model.expr = lib.mkForce "absent(up)";
                expressions.B = {
                  source = true;
                  model = { type = "math"; expression = "$A > 0"; };
                };
                notificationSettings = { type = "SimplifiedRouting"; receiver = "email"; };
              };
              another.expressions.A.source = true;
              disabled = lib.mkIf false { title = "Not declared"; };
            };
          };
          inhibitionRuleDefaults = {
            source_matchers = [{ label = "managed_by"; value = "nix"; }];
            target_matchers = [{ label = "managed_by"; value = "nix"; }];
            equal = lib.mkDefault [ "host" ];
          };
          inhibitionRules = {
            unit-failed = {
              source_matchers = [{ label = "alertname"; value = "Unit failed"; }];
              target_matchers = [{ label = "alertname"; type = "=~"; value = "Unit inactive|Unit restarting"; }];
              equal = [ "host" "container" "name" ];
            };
            host-missing = {};
            disabled = lib.mkIf false {};
          };
        };
      })
    ];
  }).config;
  folder = cfg.grafana.folders.infrastructure;
  first = folder.rules.host-missing;
  second = folder.rules.another;
  appCfg = (lib.evalModules {
    modules = [
      { options.apps = lib.mkOption { type = lib.types.attrsOf lib.types.anything; }; }
      (cfg.perSystem { inherit pkgs; })
    ];
  }).config;
in
assert folder.uid == "infrastructure";
assert builtins.attrNames folder.rules == [ "another" "host-missing" ];
assert first.uid == "host-missing" && first.for == "10m" && first.trigger.interval == "30s";
assert first.labels == { managed_by = "nix"; severity = "critical"; };
assert first.expressions.A.model == { expr = "absent(up)"; instant = true; };
assert first.expressions.A.relativeTimeRange == { from = "10m"; to = "0s"; };
assert !first.expressions.A.source && first.expressions.B.source;
assert first.expressions.B.datasourceUID == "__expr__";
assert first.notificationSettings == { type = "SimplifiedRouting"; receiver = "email"; };
assert second.for == "5m" && second.trigger.interval == "2m";
assert second.labels.severity == "warning" && second.expressions.A.source;
assert second.noDataState == "NoData" && second.keepFiringFor == "0s" && !second.paused;
assert builtins.attrNames cfg.grafana.inhibitionRules == [ "host-missing" "unit-failed" ];
assert cfg.grafana.inhibitionRules.host-missing.equal == [ "host" ];
assert cfg.grafana.inhibitionRules.unit-failed.equal == [ "host" "container" "name" ];
assert builtins.length cfg.grafana.inhibitionRules.unit-failed.source_matchers == 2;
assert builtins.all (matcher: matcher.type == "=") cfg.grafana.inhibitionRules.unit-failed.source_matchers;
# Building the real launcher also checks that deferred ruleDefaults modules
# are excluded from the generated JSON. Do not run SOPS or contact Grafana.
pkgs.runCommand "grafana-module-check" {} ''
  test -x ${appCfg.apps.sync-grafana.program}
  touch "$out"
''
