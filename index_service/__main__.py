"""``python -m index_service <material.json>`` runs a material fixture."""

from .runner import main

if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
