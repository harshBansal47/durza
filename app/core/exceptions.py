class EntityNotFoundError(Exception):
    """Raised by services when a referenced entity (e.g. a foreign-key target) does not exist."""
