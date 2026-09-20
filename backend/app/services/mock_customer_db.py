"""Tiny mock customer document store for the simulated Gmail demo.

This is deliberately outside the verification engine. It represents an
external customer/ERP/portal system that can return a missing document when the
ShipSync side panel asks for it.
"""
from __future__ import annotations

from app.schemas import AttachmentPayload


MOCK_DOCUMENTS: dict[str, dict[str, AttachmentPayload]] = {
    "SHP-001": {
        "BL": AttachmentPayload(
            # The filename carries the `_BL` marker the engine's doc-type
            # detection expects, so the returned document flows straight into
            # the SI ↔ BL comparison.
            filename="SHP-001_BL.txt",
            content_text="""BILL OF LADING (DRAFT)
Shipper: EAST BRIGHT ENTERPRISE CO LTD
Consignee: OCEANIC LOGISTICS GMBH
Notify Party: PACIFIC FREIGHT SERVICES
Port of Loading: NANTONG, CHINA (CNNTG)
Port of Discharge: ROTTERDAM, NETHERLANDS (NLRTM)
Total Containers: 4 x 40'HC
Gross Weight: 22,000 KG
Vessel: EVER GIVEN V.012
B/L No.: BL-SHP001
""",
        ),
    },
    "SHP-002": {
        "BL": AttachmentPayload(
            filename="SHP-002_BL.txt",
            content_text="""BILL OF LADING (DRAFT)
Shipper: ACME TRADING LTD
Consignee: GLOBAL BUYER INC
Notify Party: GLOBAL BUYER INC
Port of Loading: SHANGHAI, CHINA (CNSHA)
Port of Discharge: PORT KLANG, MALAYSIA (MYPKG)
Total Containers: 2 x 40HC
Gross Weight: 22.5 MT
B/L No.: BL-SHP002
""",
        ),
    },
}


def request_document(shipment_id: str, document_type: str) -> AttachmentPayload | None:
    """Return a mock document attachment, or None if the store lacks it."""
    shipment = MOCK_DOCUMENTS.get(str(shipment_id).upper())
    if shipment is None:
        return None
    return shipment.get(str(document_type).upper())
