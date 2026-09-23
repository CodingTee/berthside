"""Test script for the Cascading LLM Gateway (4 Core Capabilities).

Runs:
1. Problem 1: Vision document extraction (Gemini > Zhipu > Qwen > Local OCR)
2. Problem 2: Ambiguous email intent classification
3. Problem 3: Unstructured shipping text field extraction
4. Problem 4: Human-in-the-loop (HITL) discrepancy attribution & auto draft reply
"""
import os
import sys
import io
import json
from pathlib import Path
from PIL import Image, ImageDraw

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

# Ensure backend root is in sys.path
backend_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(backend_root))

from app.services.llm_gateway import gateway

def create_sample_bl_image() -> bytes:
    """Create a sample synthetic Bill of Lading image with stamps/text."""
    img = Image.new("RGB", (800, 600), color=(250, 250, 250))
    draw = ImageDraw.Draw(img)
    lines = [
        "OCEAN BILL OF LADING",
        "B/L NO: MEDU12345678",
        "SHIPPER: ACME LOGISTICS SHENZHEN LTD",
        "CONSIGNEE: PACIFIC TRADING CORP LOS ANGELES",
        "NOTIFY PARTY: SAME AS CONSIGNEE",
        "PORT OF LOADING: SHENZHEN, CHINA",
        "PORT OF DISCHARGE: LOS ANGELES, USA",
        "CONTAINER: 2X40HQ (2 CONTAINERS)",
        "GROSS WEIGHT: 24,500.0 KGS",
    ]
    y = 50
    for line in lines:
        draw.text((60, y), line, fill=(20, 20, 20))
        y += 45
    # Add a mock stamp
    draw.rectangle([450, 350, 720, 520], outline=(180, 40, 40), width=3)
    draw.text((480, 420), "CARRIER VERIFIED", fill=(180, 40, 40))
    
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()

def test_problem_1_vision():
    print("\n" + "=" * 60)
    print("TEST 1: Challenging Vision Document Extraction (Image/Scan)")
    print("=" * 60)
    img_bytes = create_sample_bl_image()
    res = gateway.extract_from_image(img_bytes, "sample_bl.png", "BL")
    print("Result Source:", res.get("source"))
    print("Extracted Fields:", json.dumps(res.get("fields", {}), indent=2))
    assert res.get("readable") is True

def test_problem_2_intent():
    print("\n" + "=" * 60)
    print("TEST 2: Ambiguous/Complex Email Intent Classification")
    print("=" * 60)
    ambiguous_email = {
        "from": "forwarding_ops@fastfreight.com",
        "subject": "Fwd: Urgent confirmation needed for Pacific Voyage 402W",
        "body": (
            "Hi Team,\n\n"
            "Carrier just sent over the draft bill of lading for booking #BK-99812. "
            "Please cross-check all party details, weight totals, and discharge port against the approved SI ASAP. "
            "The vessel departs tomorrow morning and documentation cutoff is 5 PM.\n\n"
            "Thanks,\nSarah"
        ),
        "attachments": ["Draft_BL_BK99812.pdf"]
    }
    res = gateway.classify_ambiguous_email(ambiguous_email)
    print("Result Source:", res.get("source"))
    print(f"Category: {res.get('category')} (Confidence: {res.get('confidence')})")
    print("Reason:", res.get("reason"))
    assert res.get("category") == "BL_COMPARISON"

def test_problem_3_unstructured_text():
    print("\n" + "=" * 60)
    print("TEST 3: Precise Field Extraction from Unstructured Text")
    print("=" * 60)
    unstructured_text = """
    *** BOOKING SHIPPING ADVICE ***
    Hey everyone, please note the cargo details for our export shipment below:
    Shipper is Global Food Imports LLC based in Rotterdam.
    Consignee on delivery will be Tokyo Wholesale Foods Inc, Tokyo Japan.
    Please list Notify as Same as Consignee.
    We are loading at Rotterdam Port and sailing directly into Tokyo Port.
    Total cargo loaded into 3 containers with measured gross mass of 41,200.50 KGS.
    """
    res = gateway.extract_from_unstructured_text(unstructured_text, "SI")
    print("Result Source:", res.get("source"))
    print("Extracted Fields:", json.dumps(res.get("fields", {}), indent=2))
    assert len(res.get("fields", {})) >= 5

def test_problem_4_hitl_draft():
    print("\n" + "=" * 60)
    print("TEST 4: HITL Smart Attribution & Auto-Draft Email Reply")
    print("=" * 60)
    email = {
        "from": "carrier_docs@oceanline.com",
        "subject": "Draft B/L for Booking MSC-887123",
        "body": "Please find attached the draft B/L for your review."
    }
    discrepancies = [
        {
            "field": "gross_weight_kg",
            "expected": 18500.0,
            "actual": 15800.0,
            "discrepancy_type": "mismatch"
        },
        {
            "field": "port_of_discharge",
            "expected": "PORT KLANG, MALAYSIA",
            "actual": "SINGAPORE",
            "discrepancy_type": "mismatch"
        }
    ]
    res = gateway.generate_hitl_draft(email, discrepancies)
    print("Result Source:", res.get("source"))
    print("\n--- AI Root-Cause Attribution ---")
    print(res.get("attribution"))
    print("\n--- Generated Reply Subject ---")
    print(res.get("draft_subject"))
    print("\n--- Generated Reply Body ---")
    print(res.get("draft_body"))
    assert bool(res.get("draft_body"))

if __name__ == "__main__":
    test_problem_1_vision()
    test_problem_2_intent()
    test_problem_3_unstructured_text()
    test_problem_4_hitl_draft()
    print("\n" + "=" * 60)
    print("All 4 Core Capabilities Verified Successfully!")
    print("=" * 60)
