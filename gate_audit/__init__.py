"""独立展馆分区 / 门段目录与路线核查事实库。

公开入口：
* :class:`Catalog` —— 平面/门版本目录与独立共边门查询；
* :class:`AuditStore` —— 整条路线核查、报告复读/异文拒绝、通行凭据。
"""
from .catalog import Catalog, CatalogError, Gate, PlanVersion
from .geometry import Point, Rect
from .reports import AuditStore, ReportError
from .verifier import RouteResult, verify_route

__all__ = [
    "Catalog", "CatalogError", "Gate", "PlanVersion",
    "Point", "Rect", "AuditStore", "ReportError",
    "RouteResult", "verify_route",
]
