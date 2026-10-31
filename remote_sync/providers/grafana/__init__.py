from .. import Collection
from .folders import Folders
from .inhibitions import Inhibitions


class Grafana:
    """Expose folders/alerts and inhibition rules through one service entry point."""

    def __init__(self, **settings):
        self.settings = settings

    def collections(self, *, owner, declarations, adoptions) -> dict[str, Collection]:
        assert declarations.keys() == {"folders", "inhibitionRules"}, (
            "Grafana resources must contain folders and inhibitionRules. Regenerate the configuration with the current Nix module."
        )
        return {
            "folders": Collection(Folders(**self.settings), declarations["folders"], adoptions),
            # Names are immutable, so inhibition rules cannot be adopted.
            "inhibitionRules": Collection(Inhibitions(owner=owner, **self.settings), declarations["inhibitionRules"]),
        }
