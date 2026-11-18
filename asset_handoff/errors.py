"""Domain errors shared by the catalog and the handoff warehouse."""


class AssetHandoffError(Exception):
    """Base class for every error raised by this package."""


class UnknownAssetError(AssetHandoffError):
    """An ``asset_id`` is not present in the catalog."""

    def __init__(self, asset_id):
        super().__init__(f"unknown asset_id: {asset_id!r}")
        self.asset_id = asset_id


class DuplicateAssetError(AssetHandoffError):
    """An ``asset_id`` is registered twice (in the store or one batch)."""

    def __init__(self, asset_id):
        super().__init__(f"duplicate asset_id: {asset_id!r}")
        self.asset_id = asset_id


class DuplicateHandoffError(AssetHandoffError):
    """A handoff id is already used by a prepared or committed handoff."""

    def __init__(self, handoff_id):
        super().__init__(f"duplicate handoff_id: {handoff_id!r}")
        self.handoff_id = handoff_id


class InvalidParentError(AssetHandoffError):
    """A parent does not exist or is not a ``CONTAINER``."""


class CycleError(AssetHandoffError):
    """A batch change would make the parent forest contain a cycle."""


class DepthExceededError(AssetHandoffError):
    """A batch change would make a root-to-node chain longer than the limit."""


class CustodyError(AssetHandoffError):
    """Base class for custody / handoff failures."""


class CustodianMismatchError(CustodyError):
    """At least one expanded member is not currently held by ``from_party``.

    The whole handoff request is rejected; nothing is transferred. ``mismatches``
    lists ``(asset_id, actual_custodian_or_None)`` for every offending member.
    """

    def __init__(self, expected, mismatches):
        self.expected = expected
        self.mismatches = list(mismatches)
        detail = ", ".join(
            f"{asset_id} held by {actual!r}" for asset_id, actual in self.mismatches
        )
        super().__init__(
            f"custodian mismatch (expected {expected!r}): {detail}"
        )


class PreparedStaleError(CustodyError):
    """A prepared handoff can no longer be committed because the world moved on.

    Triggered by a catalog revision change or any custody change after
    ``prepare``. The frozen preparation itself remains readable as history of
    the attempt, but it must be prepared again against the current state.
    """


class HandoffNotFoundError(CustodyError):
    """A handoff id is unknown to the warehouse."""

    def __init__(self, handoff_id):
        super().__init__(f"unknown handoff_id: {handoff_id!r}")
        self.handoff_id = handoff_id
