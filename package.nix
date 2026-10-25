{
  lib,
  buildPythonApplication,
  setuptools,
  diffsync,
  requests,
  structlog,
  pytestCheckHook
}:

buildPythonApplication {
  pname = "remote-sync";
  version = "0.1.0";
  pyproject = true;
  src = lib.cleanSource ./.;
  allowSubstitutes = false;
  preferLocalBuild = true;
  build-system = [ setuptools ];
  dependencies = [ diffsync requests structlog ];
  nativeCheckInputs = [ pytestCheckHook ];
  pythonImportsCheck = [ "remote_sync.cli" ];
  meta.mainProgram = "remote-sync";
}
