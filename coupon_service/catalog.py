"""Template catalog: stable ids, immutable versions, current pointers.

Only the *qualification* side reads the catalog.  Existing redemptions never
depend on ``current_templates``: their effective specs live in
``redemption_uses`` and ``redemptions.qualification_json``.

The chemistry is additive:

* ``coupon_templates`` already stores every ``(template_id, version)`` with a
  frozen ``spec_json``.  A version is immutable: re-publishing a version with a
  different spec is refused rather than silently changing eligibility.
* ``current_templates`` points at the revision used for *new* redemptions.
  Revising a template inserts a new version row and moves the pointer.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from . import errors
from .models import TemplateSpec


@dataclass(frozen=True)
class TemplateVersion:
    template_id: str
    version: int
    spec: TemplateSpec
    spec_json: str

    def as_dict(self) -> dict[str, object]:
        return {
            "template_id": self.template_id,
            "version": self.version,
            "spec": self.spec.as_dict(),
        }


def _row_to_version(row: sqlite3.Row) -> TemplateVersion:
    raw = row["spec_json"]
    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        raise errors.RequestError(
            f"stored template {row['template_id']} v{row['version']} has invalid spec_json",
            reason="unknown_template_version",
        ) from exc
    return TemplateVersion(
        template_id=row["template_id"],
        version=row["version"],
        spec=TemplateSpec.parse(parsed),
        spec_json=raw,
    )


def get_version(
    con: sqlite3.Connection, template_id: str, version: int
) -> TemplateVersion | None:
    row = con.execute(
        "SELECT template_id, version, spec_json FROM coupon_templates "
        "WHERE template_id=? AND version=?",
        (template_id, version),
    ).fetchone()
    return _row_to_version(row) if row else None


def get_current(con: sqlite3.Connection, template_id: str) -> TemplateVersion | None:
    row = con.execute(
        "SELECT t.template_id, t.version, t.spec_json "
        "FROM current_templates c JOIN coupon_templates t "
        "  ON t.template_id=c.template_id AND t.version=c.version "
        "WHERE c.template_id=?",
        (template_id,),
    ).fetchone()
    return _row_to_version(row) if row else None


def list_current(con: sqlite3.Connection) -> dict[str, TemplateVersion]:
    rows = con.execute(
        "SELECT t.template_id, t.version, t.spec_json "
        "FROM current_templates c JOIN coupon_templates t "
        "  ON t.template_id=c.template_id AND t.version=c.version "
        "ORDER BY t.template_id"
    ).fetchall()
    return {row["template_id"]: _row_to_version(row) for row in rows}


def publish(
    con: sqlite3.Connection,
    template_id: str,
    version: int,
    spec: TemplateSpec,
    *,
    make_current: bool = True,
) -> TemplateVersion:
    """Insert an immutable template version and optionally make it current.

    Must be called inside a write transaction.  Re-publishing the same version
    with identical content is idempotent; with different content it fails.
    """

    if not template_id:
        raise ValueError("template_id must be non-empty")
    if version < 1:
        raise ValueError("version must be >= 1")

    spec_json = json.dumps(
        spec.as_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    existing = con.execute(
        "SELECT spec_json FROM coupon_templates WHERE template_id=? AND version=?",
        (template_id, version),
    ).fetchone()
    if existing is not None:
        # Compare by parsed value, not text: historical rows may serialise keys
        # in any order, and a version is immutable regardless of formatting.
        if TemplateSpec.parse(json.loads(existing["spec_json"])) != spec:
            raise ValueError(
                f"template {template_id} v{version} already exists with a different spec"
            )
    else:
        con.execute(
            "INSERT INTO coupon_templates(template_id, version, spec_json) VALUES(?,?,?)",
            (template_id, version, spec_json),
        )

    if make_current:
        con.execute(
            "INSERT INTO current_templates(template_id, version) VALUES(?,?) "
            "ON CONFLICT(template_id) DO UPDATE SET version=excluded.version",
            (template_id, version),
        )
    return TemplateVersion(template_id, version, spec, spec_json)


def revise(
    con: sqlite3.Connection, template_id: str, version: int, spec: TemplateSpec
) -> TemplateVersion:
    """Publish a new current version.

    Strictly newer revisions move forward and stay traceable.  Re-applying a
    revision that is already current with identical content is a no-op, which
    makes replaying an operations file safe.
    """

    current = get_current(con, template_id)
    if current is not None:
        if version == current.version:
            if current.spec != spec:
                raise ValueError(
                    f"template {template_id} v{version} is current with a different spec"
                )
            return current
        if version < current.version:
            raise ValueError(
                f"revision version {version} is older than current "
                f"{current.version} for {template_id}"
            )
    return publish(con, template_id, version, spec, make_current=True)


def load_pins(
    con: sqlite3.Connection, template_id: str, version: int
) -> TemplateVersion | None:
    return get_version(con, template_id, version)
