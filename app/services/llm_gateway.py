"""Cascading LLM Gateway & Fallback Router for SDOC.

Architecture:
- Fallback Sequence (cascade mode): Gemini -> Zhipu AI -> Alibaba DashScope -> Ollama (local) -> Rules/OCR.
- Fallback Sequence (ollama mode): Ollama (local) -> Rules/OCR. No cloud key needed.
- Every consumer sees which engine answered via the "source" field of the result.
- Modality-Based Model Routing:
  * Multimodal / Vision (Images, rotated scans, noisy PDFs) -> Big Vision Models
    (Gemini 2.5 Flash, GLM-4.6v, Qwen-VL-Max)
  * Pure Text / NLP (Email intent classification, HITL draft reply) -> Small Cost-Effective Models
    (GLM-4.6v-flash, Qwen-Plus, Gemini 2.5 Flash)

Addresses:
1. OCR-resistant images/scans (rotation, stamps, distortion).
2. Ambiguous email intent classification.
3. Precise field extraction from unstructured free-form text.
4. Human-In-The-Loop (HITL) smart discrepancy attribution & auto-draft replies.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import socket
import time
from typing import Any, Optional
import urllib.error
import urllib.request

from app.config import get_settings

log = logging.getLogger(__name__)

# Standard 7 fields for shipping document completeness & comparison
COMPLETENESS_FIELDS = [
    "shipper",
    "consignee",
    "notify_party",
    "port_of_loading",
    "port_of_discharge",
    "container_count",
    "gross_weight_kg",
]


def _is_timeout(exc: BaseException) -> bool:
    """True when the failure was the clock running out, not an answer."""
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return True
    return isinstance(getattr(exc, "reason", None), (TimeoutError, socket.timeout))


class LLMGateway:
    """Industrial multi-provider cascading LLM gateway with graceful fallback."""

    def __init__(self):
        self.settings = get_settings()

    def _get_keys(self) -> dict[str, str]:
        """Fetch keys from settings or environment dynamically."""
        return {
            "gemini": self.settings.gemini_api_key or os.getenv("GEMINI_API_KEY", "").strip(),
            "zhipu": self.settings.zhipuai_api_key or os.getenv("ZHIPUAI_API_KEY", "").strip(),
            "dashscope": self.settings.dashscope_api_key or os.getenv("DASHSCOPE_API_KEY", "").strip(),
        }

    def check_ollama_status(self, timeout: Optional[float] = None) -> dict[str, Any]:
        """Check if local or remote Ollama GPU instance is online and what models are ready.

        Without an explicit `timeout` the probe uses the quick budget first and
        retries once on the cold-start budget, so a tunnel that is still coming
        up, or a host that is waking, is not mistaken for an offline GPU. The
        reply carries `cold_start` / `probe_seconds` so callers can see which
        budget answered.
        """
        settings = self.settings
        url = f"{settings.ollama_base_url.rstrip('/')}/api/tags"
        headers = {}
        if settings.ollama_api_key:
            headers["Authorization"] = f"Bearer {settings.ollama_api_key}"

        if timeout is not None:
            budgets = [float(timeout)]
        else:
            budgets = [settings.ollama_probe_timeout_seconds,
                       settings.ollama_cold_start_timeout_seconds]
            # A misconfigured pair should not make the same wait twice.
            budgets = [b for i, b in enumerate(budgets) if b > 0 and b not in budgets[:i]]

        last_error: Optional[BaseException] = None
        for index, budget in enumerate(budgets):
            started = time.time()
            try:
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=budget) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    models = [m.get("name") for m in data.get("models", [])]
                    has_target = settings.ollama_model in models or any(
                        settings.ollama_model.split(":")[0] in m for m in models)
                    return {
                        "online": True,
                        "models": models,
                        "current_model": settings.ollama_model,
                        "model_ready": has_target,
                        "endpoint": settings.ollama_base_url,
                        "cold_start": index > 0,
                        "probe_seconds": round(time.time() - started, 2),
                    }
            except Exception as exc:
                last_error = exc
                if index + 1 < len(budgets):
                    # Only wait longer when the endpoint is silent; a refusal or
                    # an HTTP error answers immediately and will not improve.
                    if not _is_timeout(exc):
                        break
                    log.info(
                        "Ollama probe timed out after %.1fs; retrying on the cold-start budget (%.1fs)",
                        budget, budgets[index + 1])
        return {
            "online": False,
            "models": [],
            "current_model": settings.ollama_model,
            "model_ready": False,
            "endpoint": settings.ollama_base_url,
            "error": str(last_error),
        }

    def _call_ollama(self, prompt: str, system_prompt: str = "", images_b64: list[str] = None) -> Optional[str]:
        """Invoke Ollama (qwen2.5vl:7b) via local or remote GPU endpoint."""
        url = f"{self.settings.ollama_base_url.rstrip('/')}/api/chat"
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        user_msg: dict[str, Any] = {"role": "user", "content": prompt}
        if images_b64:
            user_msg["images"] = images_b64
        messages.append(user_msg)

        payload = {
            "model": self.settings.ollama_model,
            "messages": messages,
            "stream": False,
            "options": {"temperature": 0.1},
        }
        headers = {"Content-Type": "application/json"}
        if self.settings.ollama_api_key:
            headers["Authorization"] = f"Bearer {self.settings.ollama_api_key}"

        req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers)
        # Loading a cold model into VRAM can take minutes; OLLAMA_REQUEST_TIMEOUT_SECONDS
        # is the budget for the whole call, not just the transfer.
        with urllib.request.urlopen(req, timeout=self.settings.ollama_request_timeout_seconds) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data.get("message", {}).get("content")

    def call_vision_ollama(self, image_bytes: bytes, mime_type: str, prompt: str) -> Optional[dict]:
        """Local Ollama VLM (qwen2.5vl:7b) vision extraction. No cloud key needed."""
        try:
            b64 = base64.b64encode(image_bytes).decode("utf-8")
            content = self._call_ollama(prompt, system_prompt="", images_b64=[b64])
            if content:
                return {"provider": f"ollama-{self.settings.ollama_model}", "content": content}
        except Exception as exc:
            log.warning("Ollama vision failed: %s", exc)
        return None

    def call_text_ollama(self, prompt: str, system_prompt: str = "") -> Optional[dict]:
        """Local Ollama VLM text completion. No cloud key needed."""
        try:
            content = self._call_ollama(prompt, system_prompt=system_prompt)
            if content:
                return {"provider": f"ollama-{self.settings.ollama_model}", "content": content}
        except Exception as exc:
            log.warning("Ollama text failed: %s", exc)
        return None

    # =========================================================================
    # LOW-LEVEL CALLERS: Vision (Big Models) & Text (Small Models)
    # =========================================================================

    def call_vision_cascade(self, image_bytes: bytes, mime_type: str, prompt: str) -> Optional[dict]:
        """Cascade: Gemini 2.5 Flash -> Zhipu GLM-4.6v -> Qwen VL -> None (Local OCR)."""
        keys = self._get_keys()
        b64_data = base64.b64encode(image_bytes).decode("utf-8")

        # 1. Tier 1: Google Gemini 2.5 Flash
        if keys["gemini"]:
            try:
                log.info("Attempting Vision with Gemini 2.5 Flash...")
                res = self._call_gemini_vision(keys["gemini"], b64_data, mime_type, prompt)
                if res:
                    return {"provider": "gemini-2.5-flash", "content": res}
            except Exception as exc:
                log.warning("Gemini vision failed: %s; falling back to Zhipu", exc)

        # 2. Tier 2: Zhipu AI GLM-4.6v
        if keys["zhipu"]:
            try:
                log.info("Attempting Vision with Zhipu GLM-4.6v...")
                res = self._call_zhipu_vision(keys["zhipu"], b64_data, mime_type, prompt)
                if res:
                    return {"provider": "zhipu-glm-4.6v", "content": res}
            except Exception as exc:
                log.warning("Zhipu vision failed: %s; falling back to DashScope", exc)

        # 3. Tier 3: Alibaba DashScope Qwen-VL
        if keys["dashscope"]:
            try:
                log.info("Attempting Vision with DashScope Qwen-VL...")
                res = self._call_dashscope_vision(keys["dashscope"], b64_data, mime_type, prompt)
                if res:
                    return {"provider": "dashscope-qwen-vl", "content": res}
            except Exception as exc:
                log.warning("DashScope vision failed: %s; falling back to Local OCR", exc)

        # 4. Tier 4 (local, free): Ollama VLM, used when cloud keys are absent
        #    or all cloud providers failed. Gracefully skipped if Ollama is down.
        if self.check_ollama_status().get("online"):
            try:
                log.info("Attempting Vision with local Ollama %s...", self.settings.ollama_model)
                res = self.call_vision_ollama(image_bytes, mime_type, prompt)
                if res:
                    return res
            except Exception as exc:
                log.warning("Ollama vision failed: %s; falling back to Local OCR", exc)

        return None

    def call_text_cascade(self, prompt: str, system_prompt: str = "") -> Optional[dict]:
        """Cascade: Gemini 2.5 Flash -> Zhipu (GLM-4.6v-flash/GLM-4v-flash) -> Qwen-Plus -> Local Rules."""
        keys = self._get_keys()

        # 1. Tier 1: Google Gemini 2.5 Flash (Ultra-fast, 1M context, high free RPM)
        if keys["gemini"]:
            try:
                log.info("Attempting Text with Gemini 2.5 Flash...")
                res = self._call_gemini_text(keys["gemini"], prompt, system_prompt)
                if res:
                    return {"provider": "gemini-2.5-flash", "content": res}
            except Exception as exc:
                log.warning("Gemini text failed: %s; falling back to Zhipu", exc)

        # 2. Tier 2: Zhipu Small Model (glm-4.6v-flash with fallback to glm-4v-flash/glm-4-flash)
        if keys["zhipu"]:
            for model_name in ("glm-4.6v-flash", "glm-4v-flash", "glm-4-flash"):
                try:
                    log.info("Attempting Text with Zhipu %s...", model_name)
                    res = self._call_zhipu_chat(keys["zhipu"], model_name, prompt, system_prompt)
                    if res:
                        return {"provider": f"zhipu-{model_name}", "content": res}
                except Exception as exc:
                    log.warning("Zhipu %s failed: %s", model_name, exc)
                    time.sleep(0.3)

        # 3. Tier 3: Alibaba DashScope Qwen-Plus
        if keys["dashscope"]:
            try:
                log.info("Attempting Text with DashScope Qwen-Plus...")
                res = self._call_dashscope_chat(keys["dashscope"], "qwen-plus", prompt, system_prompt)
                if res:
                    return {"provider": "dashscope-qwen-plus", "content": res}
            except Exception as exc:
                log.warning("DashScope text failed: %s", exc)

        # 4. Tier 4 (local, free): Ollama VLM text, no cloud key needed.
        if self.check_ollama_status().get("online"):
            try:
                log.info("Attempting Text with local Ollama %s...", self.settings.ollama_model)
                res = self.call_text_ollama(prompt, system_prompt)
                if res:
                    return res
            except Exception as exc:
                log.warning("Ollama text failed: %s", exc)

        return None

    # =========================================================================
    # CORE CAPABILITY 1: OCR-Resistant Vision Document Extraction
    # =========================================================================

    def extract_from_image(self, image_bytes: bytes, filename: str, doc_type: str = "BL", backend: str = "cascade") -> dict[str, Any]:
        """Extract shipping fields from images (handles inverted, rotated, or noisy scans)."""
        prompt = (
            f"You are an expert shipping document auditor analyzing a {doc_type} (Bill of Lading or Shipping Instruction).\n"
            "This document might be rotated, inverted, or contain messy stamps/tables.\n"
            "Analyze the image and accurately extract these 7 standard fields into JSON format:\n"
            "1. shipper (Full name of consignor/shipper)\n"
            "2. consignee (Full name of receiver/consignee)\n"
            "3. notify_party (Party to notify, or 'SAME AS CONSIGNEE')\n"
            "4. port_of_loading (POL port name, e.g. 'SHANGHAI, CHINA')\n"
            "5. port_of_discharge (POD destination port name)\n"
            "6. container_count (Integer number of containers, or null if unmentioned)\n"
            "7. gross_weight_kg (Total gross weight in numeric kg, e.g. 18500.0, or null)\n\n"
            "Return ONLY a valid JSON object with keys: shipper, consignee, notify_party, "
            "port_of_loading, port_of_discharge, container_count, gross_weight_kg. "
            "Do not include markdown backticks or commentary."
        )

        ext = filename.lower().split(".")[-1]
        mime_map = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "webp": "image/webp", "tif": "image/tiff", "tiff": "image/tiff"}
        mime_type = mime_map.get(ext, "image/jpeg")

        resp = (self.call_vision_ollama(image_bytes, mime_type, prompt)
                if backend == "ollama" else self.call_vision_cascade(image_bytes, mime_type, prompt))
        if resp and resp.get("content"):
            parsed = self._extract_json_from_text(resp["content"])
            if parsed:
                return {
                    "doc_type": doc_type,
                    "fields": {k: parsed.get(k) for k in COMPLETENESS_FIELDS if parsed.get(k) is not None},
                    "readable": True,
                    "source": f"llm-vision ({resp['provider']})"
                }

        # Fallback to local RapidOCR
        try:
            from app.services.ocr import ocr_image
            from app.services import extractor
            text = ocr_image(image_bytes, filename)
            if text:
                res = extractor.extract_fields(text, doc_type)
                res_dict = res.to_dict()
                res_dict["source"] = "image-ocr"
                return res_dict
        except Exception as e:
            log.warning("Local RapidOCR fallback failed: %s", e)

        return {"doc_type": doc_type, "fields": {}, "readable": False, "missing": COMPLETENESS_FIELDS, "source": "unreadable"}

    # =========================================================================
    # CORE CAPABILITY 2: Ambiguous Email Intent Classification
    # =========================================================================

    def classify_ambiguous_email(self, email: dict, backend: str = "cascade") -> dict[str, Any]:
        """Classify ambiguous or non-standard emails into 5 standard shipping categories."""
        subject = email.get("subject", "")
        body = email.get("body", "")
        sender = email.get("from", "")
        attachments = [a.get("filename", "") if isinstance(a, dict) else str(a) for a in email.get("attachments", [])]

        system_prompt = (
            "You are an AI email classifier for an international maritime logistics company.\n"
            "Classify the incoming email into exactly one of these 5 categories:\n"
            "- BL_COMPARISON: Email contains or discusses a Draft Bill of Lading (B/L) needing verification or comparison with Shipping Instruction (SI).\n"
            "- SI_REQUEST: Email requests Shipping Instructions, booking confirmations, or initial shipping order details.\n"
            "- INVOICE_QUERY: Email concerns freight charges, billing, sea-waybill invoices, or payment queries.\n"
            "- GENERAL: General operational correspondence, customer inquiries, updates, or acknowledgments.\n"
            "- SPAM: Phishing, unsolicited marketing, dangerous attachments, or completely irrelevant mail.\n\n"
            "Output JSON with keys: category (one of the 5), confidence (0.0 to 1.0), reason (short explanation)."
        )

        prompt = (
            f"Sender: {sender}\n"
            f"Subject: {subject}\n"
            f"Attachments: {', '.join(attachments)}\n"
            f"Body:\n{body[:1500]}\n\n"
            "Provide strict JSON response."
        )

        resp = (self.call_text_ollama(prompt, system_prompt)
                if backend == "ollama" else self.call_text_cascade(prompt, system_prompt))
        if resp and resp.get("content"):
            parsed = self._extract_json_from_text(resp["content"])
            if parsed and parsed.get("category") in {"BL_COMPARISON", "SI_REQUEST", "INVOICE_QUERY", "GENERAL", "SPAM"}:
                return {
                    "category": parsed["category"],
                    "confidence": float(parsed.get("confidence", 0.95)),
                    "reason": parsed.get("reason", "Classified by cascading AI"),
                    "source": f"llm-text ({resp['provider']})"
                }

        # Fallback to local rule engine
        from app.services.classifier import classify as rule_classify
        rc = rule_classify(email)
        return {
            "category": rc.category,
            "confidence": rc.confidence,
            "reason": rc.reason,
            "source": "rule-classifier"
        }

    # =========================================================================
    # CORE CAPABILITY 3: Unstructured Free-Form Field Extraction
    # =========================================================================

    def extract_from_unstructured_text(self, text: str, doc_type: str = "BL", backend: str = "cascade") -> dict[str, Any]:
        """Extract the 7 shipping fields from unstructured or non-standard document text."""
        system_prompt = (
            f"You are an expert shipping document parser extracting fields from {doc_type} text.\n"
            "Extract these 7 standard fields into JSON:\n"
            "- shipper\n- consignee\n- notify_party\n- port_of_loading\n- port_of_discharge\n"
            "- container_count (integer or null)\n- gross_weight_kg (float or null)\n"
            "Return JSON only."
        )

        resp = (self.call_text_ollama(text[:3000], system_prompt)
                if backend == "ollama" else self.call_text_cascade(text[:3000], system_prompt))
        if resp and resp.get("content"):
            parsed = self._extract_json_from_text(resp["content"])
            if parsed:
                fields = {k: parsed.get(k) for k in COMPLETENESS_FIELDS if parsed.get(k) is not None}
                from app.services import extractor
                missing = extractor.missing_of(fields)
                return {
                    "doc_type": doc_type,
                    "fields": fields,
                    "readable": True,
                    "missing": missing,
                    "source": f"llm-text ({resp['provider']})"
                }

        from app.services import extractor
        local = extractor.extract_fields(text, doc_type)
        return local.to_dict()

    # =========================================================================
    # CORE CAPABILITY 4: HITL Smart Attribution & Auto-Draft Email Reply
    # =========================================================================

    def generate_hitl_draft(self, email: dict, discrepancies: list[dict], extra_notes: str = "", backend: str = "cascade") -> dict[str, Any]:
        """Generate smart discrepancy attribution explanation and professional draft reply."""
        subject = email.get("subject", "")
        sender = email.get("from", "")
        
        disc_text = ""
        for i, d in enumerate(discrepancies, 1):
            disc_text += (
                f"{i}. Field '{d.get('field', 'Unknown')}': "
                f"Expected (SI)='{d.get('expected')}', Found in B/L='{d.get('actual')}'. "
                f"Issue: {d.get('discrepancy_type', 'mismatch')}\n"
            )

        system_prompt = (
            "You are an AI maritime documentation specialist assisting human operations staff (HITL).\n"
            "Analyze the verified discrepancies between the draft Bill of Lading (B/L) and Shipping Instruction (SI).\n"
            "Provide two things in strict JSON:\n"
            "1. 'attribution': A clear, concise root-cause analysis (e.g., potential customs risk, port detention penalty, digit transposition).\n"
            "2. 'draft_subject': Professional email subject line for replying to the carrier/customer (including B/L or Booking reference if present).\n"
            "3. 'draft_body': A polite, clear, and actionable English email draft detailing exactly what corrections are required, and requesting an amended B/L before shipping cutoff.\n\n"
            "Output JSON format:\n"
            "{\n"
            "  \"attribution\": \"...\",\n"
            "  \"draft_subject\": \"...\",\n"
            "  \"draft_body\": \"...\"\n"
            "}"
        )

        prompt = (
            f"Original Email Subject: {subject}\n"
            f"Original Sender: {sender}\n"
            f"Discrepancies Found:\n{disc_text or 'No direct discrepancies (document completeness issue)'}\n"
            f"Additional Notes: {extra_notes}\n\n"
            "Generate attribution and draft reply."
        )

        resp = (self.call_text_ollama(prompt, system_prompt)
                if backend == "ollama" else self.call_text_cascade(prompt, system_prompt))
        if resp and resp.get("content"):
            parsed = self._extract_json_from_text(resp["content"])
            if parsed and parsed.get("draft_body"):
                return {
                    "attribution": parsed.get("attribution", "Discrepancy identified between Draft B/L and SI."),
                    "draft_subject": parsed.get("draft_subject", f"Re: {subject} - Amendment Required"),
                    "draft_body": parsed["draft_body"],
                    "source": f"llm-hitl ({resp['provider']})"
                }

        # Deterministic professional fallback template
        attribution = "Discrepancies identified between submitted Draft B/L and confirmed Shipping Instructions."
        draft_subject = f"Urgent: B/L Draft Amendment Required - {subject}"
        draft_lines = [
            f"Dear Shipping Team,",
            "",
            "Thank you for submitting the draft documentation. Upon automated verification against our Shipping Instructions, the following discrepancies were detected:",
            "",
        ]
        for d in discrepancies:
            draft_lines.append(f"- {d.get('field', '').replace('_', ' ').title()}: SI states '{d.get('expected')}', but Draft B/L indicates '{d.get('actual')}'.")
        draft_lines.extend([
            "",
            "To avoid customs clearance issues or carrier amendment penalties, please issue an amended Draft B/L reflecting the correct information at your earliest convenience.",
            "",
            "Best regards,",
            "Shipping Documentation Team",
        ])

        return {
            "attribution": attribution,
            "draft_subject": draft_subject,
            "draft_body": "\n".join(draft_lines),
            "source": "deterministic-template"
        }

    # =========================================================================
    # INTERNAL API CLIENT IMPLEMENTATIONS
    # =========================================================================

    def _call_gemini_vision(self, key: str, b64_data: str, mime_type: str, prompt: str) -> Optional[str]:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={key}"
        payload = {
            "contents": [{
                "parts": [
                    {"text": prompt},
                    {"inline_data": {"mime_type": mime_type, "data": b64_data}}
                ]
            }]
        }
        req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data["candidates"][0]["content"]["parts"][0]["text"]

    def _call_zhipu_vision(self, key: str, b64_data: str, mime_type: str, prompt: str) -> Optional[str]:
        url = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
        payload = {
            "model": "glm-4.6v",
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{b64_data}"}}
                ]
            }],
            "max_tokens": 1024
        }
        req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data["choices"][0]["message"]["content"]

    def _call_dashscope_vision(self, key: str, b64_data: str, mime_type: str, prompt: str) -> Optional[str]:
        url = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
        payload = {
            "model": "qwen-vl-max",
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{b64_data}"}}
                ]
            }],
            "max_tokens": 1024
        }
        req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data["choices"][0]["message"]["content"]

    def _call_zhipu_chat(self, key: str, model: str, prompt: str, system_prompt: str) -> Optional[str]:
        url = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})
        payload = {"model": model, "messages": messages, "max_tokens": 1024}
        req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data["choices"][0]["message"]["content"]

    def _call_dashscope_chat(self, key: str, model: str, prompt: str, system_prompt: str) -> Optional[str]:
        url = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})
        payload = {"model": model, "messages": messages, "max_tokens": 1024}
        req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data["choices"][0]["message"]["content"]

    def _call_gemini_text(self, key: str, prompt: str, system_prompt: str) -> Optional[str]:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={key}"
        full_text = f"System: {system_prompt}\n\nUser: {prompt}" if system_prompt else prompt
        payload = {"contents": [{"parts": [{"text": full_text}]}]}
        req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data["candidates"][0]["content"]["parts"][0]["text"]

    def _extract_json_from_text(self, text: str) -> Optional[dict]:
        """Robustly extract JSON object from markdown fences or raw response."""
        if not text:
            return None
        text = text.strip()
        # Remove markdown code blocks ```json ... ```
        if "```" in text:
            match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text)
            if match:
                text = match.group(1)
        # Search for first { to last }
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            text = text[start : end + 1]
        try:
            return json.loads(text)
        except Exception:
            return None


# Global singleton instance
gateway = LLMGateway()
