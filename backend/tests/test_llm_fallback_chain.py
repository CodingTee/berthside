"""Fallback chain: cloud LLM -> local Ollama -> rules (cascade mode),
Ollama -> rules (ollama mode). Every result carries a "source" marker so the
operator can tell which engine answered.
"""
from app.services import extractor
from app.services.llm_gateway import gateway

SAMPLE = {
    "subject": "Draft BL for your check",
    "from": "ops@carrier.com",
    "body": "Please compare the draft bill of lading with the shipping instruction.",
    "attachments": ["si.pdf", "draft_bl.pdf"],
}

OLLAMA_RESP = {
    "provider": "ollama-qwen2.5vl:7b",
    "content": '{"category": "SPAM", "confidence": 0.9, "reason": "junk mail"}',
}


def test_cascade_cloud_fail_falls_to_ollama(monkeypatch):
    # no cloud keys configured: the real cascade tiers all skip, Ollama answers
    monkeypatch.setattr(gateway, "_get_keys",
                        lambda: {"gemini": "", "zhipu": "", "dashscope": ""})
    monkeypatch.setattr(gateway, "check_ollama_status",
                        lambda: {"online": True, "model_ready": True})
    monkeypatch.setattr(gateway, "call_text_ollama", lambda *a, **k: OLLAMA_RESP)
    out = gateway.classify_ambiguous_email(SAMPLE, backend="cascade")
    assert out["category"] == "SPAM"
    assert "ollama" in out["source"]


def test_cascade_cloud_and_ollama_down_falls_to_rules(monkeypatch):
    monkeypatch.setattr(gateway, "call_text_cascade", lambda *a, **k: None)
    monkeypatch.setattr(gateway, "check_ollama_status", lambda: {"online": False})
    out = gateway.classify_ambiguous_email(SAMPLE, backend="cascade")
    assert out["source"] == "rule-classifier"
    assert out["category"] in {"BL_COMPARISON", "SI_REQUEST", "INVOICE_QUERY",
                               "GENERAL", "SPAM"}


def test_ollama_mode_down_falls_to_rules(monkeypatch):
    monkeypatch.setattr(gateway, "call_text_ollama", lambda *a, **k: None)
    out = gateway.classify_ambiguous_email(SAMPLE, backend="ollama")
    assert out["source"] == "rule-classifier"


def test_ollama_mode_garbage_json_falls_to_rules(monkeypatch):
    monkeypatch.setattr(gateway, "call_text_ollama",
                        lambda *a, **k: {"provider": "ollama-qwen2.5vl:7b",
                                         "content": "not json at all"})
    out = gateway.classify_ambiguous_email(SAMPLE, backend="ollama")
    assert out["source"] == "rule-classifier"


def test_vision_cascade_falls_to_rapidocr_rules(monkeypatch):
    """Cloud vision and Ollama both down: RapidOCR + rule extraction must run.

    Regression: the fallback crashed on ExtractionResult.to_dict, so a failed
    vision call returned unreadable instead of the OCR reading.
    """
    monkeypatch.setattr(gateway, "call_vision_cascade", lambda *a, **k: None)
    monkeypatch.setattr(gateway, "check_ollama_status", lambda: {"online": False})

    def fake_ocr(image_bytes, filename):
        return "Shipper: PACIFIC ASIA TRADING"

    monkeypatch.setattr("app.services.ocr.ocr_image", fake_ocr)
    local = extractor.ExtractionResult(
        doc_type="BL",
        fields={"shipper": "PACIFIC ASIA TRADING"},
        missing=["consignee"],
        readable=True,
        source="pdf-ocr",
    )
    monkeypatch.setattr(extractor, "extract_fields", lambda text, doc_type: local)
    out = gateway.extract_from_image(b"fake-png-bytes", "fax_bl.png", "BL")
    assert out["readable"] is True
    assert out["source"] == "local-rapidocr"
    assert out["fields"]["shipper"] == "PACIFIC ASIA TRADING"


def test_extraction_result_to_dict_roundtrip():
    res = extractor.ExtractionResult(doc_type="BL", fields={"shipper": "ACME"},
                                     missing=["consignee"], readable=True,
                                     source="pdf")
    d = res.to_dict()
    assert d["fields"] == {"shipper": "ACME"}
    assert d["missing"] == ["consignee"]
    assert d["readable"] is True
    assert d["source"] == "pdf"
    # mutating the dict must not touch the dataclass
    d["fields"]["shipper"] = "CHANGED"
    assert res.fields["shipper"] == "ACME"
