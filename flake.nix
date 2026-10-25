{
  description = "Declarative remote resource reconciliation";

  inputs = {
    systems.url = "github:nix-systems/default";
    flake-parts.url = "github:hercules-ci/flake-parts";
    flake-parts.inputs.nixpkgs-lib.follows = "nixpkgs";
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  };

  outputs = inputs@{ flake-parts, systems, ... }:
    flake-parts.lib.mkFlake { inherit inputs; } {
      systems = import systems;

      flake.flakeModules.default = ./flake-module.nix;

      perSystem = { config, pkgs, ... }: {
        packages.remote-sync = pkgs.python3Packages.callPackage ./package.nix {};
        packages.default = config.packages.remote-sync;
        checks.remote-sync = config.packages.remote-sync;

        devShells.default = pkgs.mkShell {
          inputsFrom = [ config.packages.remote-sync ];
          packages = [ pkgs.nixd pkgs.python3Packages.pytest ];
        };
      };
    };
}
