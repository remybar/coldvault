class ColdVaultError(Exception):
    """Échec explicite : l'opération ne doit pas être déclarée validée."""


class UnsafePath(ColdVaultError):
    pass


class SourceChanged(ColdVaultError):
    pass


class Cancelled(ColdVaultError):
    pass


class Conflict(ColdVaultError):
    pass
