{
  lib,
  buildPythonApplication,
  fetchFromGitHub,
  setuptools,
  certifi,
  diffsync,
  requests,
  structlog,
  urllib3,
  pytestCheckHook
}:

let

  # Keep these security floors aligned with pyproject.toml until Nixpkgs catches up.
  atLeast = package: version: source:
    if lib.versionAtLeast package.version version then
      lib.warn "remote-sync: Nixpkgs provides ${package.pname} ${package.version} (>= ${version}); remove its security override from package.nix." package
    else package.overridePythonAttrs (_: {
      inherit version;
      src = fetchFromGitHub (source // { tag = version; });
    });

  certifiFixed = atLeast certifi "2026.10.19" {
    owner = "certifi";
    repo = "python-certifi";
    hash = "sha256-eiNK0yRcaOr0HW/kCc+0oOUXOl0jh+2rT41qBdDrVK8=";
  };
  urllib3Fixed = atLeast urllib3 "2.8.0" {
    owner = "urllib3";
    repo = "urllib3";
    hash = "sha256-/EABIEAZDr5936K5Utjlx0nsGHWsHoimqC6dGDuOJAo=";
  };
  requestsFixed = requests.override {
    certifi = certifiFixed;
    urllib3 = urllib3Fixed;
  };

in

buildPythonApplication {
  pname = "remote-sync";
  version = "0.1.0";
  pyproject = true;
  src = lib.cleanSource ./.;
  allowSubstitutes = false;
  preferLocalBuild = true;
  build-system = [ setuptools ];
  dependencies = [ certifiFixed diffsync requestsFixed structlog urllib3Fixed ];
  nativeCheckInputs = [ pytestCheckHook ];
  pythonImportsCheck = [ "remote_sync.cli" ];
  meta.mainProgram = "remote-sync";
}
