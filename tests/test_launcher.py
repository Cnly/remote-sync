import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize(("provider", "token_name"), [
    ("hetrixtools", "HETRIXTOOLS_API_TOKEN"), ("healthchecks", "HEALTHCHECKS_API_KEY"),
    ("tailscale", "TAILSCALE_OAUTH_CLIENT_SECRET"),
    ("grafana", "GRAFANA_SERVICE_ACCOUNT_TOKEN"),
])
def test_launcher_preserves_arguments_and_environment_with_sops_overrides(tmp_path, provider, token_name):
    sops = tmp_path / "sops"
    sops.write_text(f"""#!{sys.executable}
import os, sys
env = {{}} if '--pristine' in sys.argv else dict(os.environ)
env['HETRIXTOOLS_CONTACT_LIST_ID'] = 'from-sops'
os.execve('/bin/sh', ['sh', '-c', sys.argv[-1]], env)
""")
    sops.chmod(0o755)
    program = tmp_path / "remote sync's program"
    program.write_text(f"""#!{sys.executable}
import json, os, sys
print(json.dumps({{
    'args': sys.argv[1:],
    'token': os.environ.get('{token_name}'),
    'contact_list': os.environ.get('HETRIXTOOLS_CONTACT_LIST_ID'),
}}))
""")
    program.chmod(0o755)
    unexpected = tmp_path / "unexpected"
    arguments = ["spaces and 'quotes'", 'double "quotes"', "", "line\nbreak", "café",
                 f"$(touch {unexpected})", f"`touch {unexpected}`", "*", "back\\slash"]
    config = str(tmp_path / "config 'file'.json")
    result = subprocess.run(
        ["bash", str(Path(__file__).parents[1] / "launcher.sh"), *arguments],
        env=os.environ | {
            "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"],
            "REMOTE_SYNC": str(program), "REMOTE_SYNC_PROVIDER": provider,
            "REMOTE_SYNC_CONFIG_FILE": config, "SECRETS_FILE": str(tmp_path / "secrets.yaml"),
            token_name: "ambient-token", "HETRIXTOOLS_CONTACT_LIST_ID": "ambient-list",
        },
        text=True, capture_output=True, check=True,
    )
    assert json.loads(result.stdout) == {
        "args": [provider, "--config-file", config, *arguments],
        "token": "ambient-token", "contact_list": "from-sops",
    }
    assert f"Using config file: {config}" in result.stderr
    assert f"Using secrets file: {tmp_path / 'secrets.yaml'}" in result.stderr
    assert not unexpected.exists()
