# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0
"""Hussh One PKM Ground-Truth Source Extractor.

Extracts structured KYC/KYB attributes from primary source documents (PDFs, high-resolution
image scans) using the active model provider (Gemini / Vertex / OpenAI), preserving exact
field-level provenance without heuristic guessing or low-resolution thumbnail downscaling.
"""

from __future__ import annotations

import base64
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

_DEFAULT_MODEL = "gemini-3.7-flash"


def resolve_active_model() -> str:
    """Resolve the active model name from environment or config, defaulting to gemini-3.7-flash."""
    return os.getenv("AGENT_GEMINI_MODEL") or os.getenv("HERMES_MODEL") or _DEFAULT_MODEL


def extract_jpeg_from_pdf(pdf_path: Path) -> Optional[bytes]:
    """Extract raw high-resolution JPEG streams directly from scanned PDFs."""
    if not pdf_path.exists():
        return None
    content = pdf_path.read_bytes()
    start = content.find(b"\xff\xd8\xff")
    if start != -1:
        end = content.find(b"\xff\xd9", start)
        if end != -1:
            return content[start : end + 2]
    return None


def extract_attributes_with_active_model(
    *,
    file_bytes: bytes,
    mime_type: str,
    extraction_prompt: str,
    api_key: str,
    model: Optional[str] = None,
    api_base: Optional[str] = None,
) -> Dict[str, Any]:
    """Submit high-resolution document data directly to the multimodal model provider."""
    active_model = model or resolve_active_model()
    # Normalize model prefix for Google endpoint
    model_name = active_model.split("/")[-1]
    endpoint = api_base or f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent"

    b64_data = base64.b64encode(file_bytes).decode("utf-8")
    payload = {
        "contents": [
            {
                "parts": [
                    {"text": extraction_prompt},
                    {
                        "inline_data": {
                            "mime_type": mime_type,
                            "data": b64_data,
                        }
                    },
                ]
            }
        ],
        "generationConfig": {"temperature": 0.0},
    }

    url = f"{endpoint}?key={api_key}"
    response = httpx.post(url, json=payload, timeout=60)
    response.raise_for_status()

    result_json = response.json()
    candidates = result_json.get("candidates", [])
    if not candidates:
        raise RuntimeError("No generation candidate returned by the model provider.")

    raw_text = candidates[0].get("content", {}).get("parts", [{}])[0].get("text", "").strip()
    return {
        "model": active_model,
        "raw_text": raw_text,
    }


def parse_extracted_json(raw_text: str) -> Dict[str, Any]:
    """Parse JSON blocks returned from model extraction."""
    clean = raw_text.strip()
    if clean.startswith("```"):
        clean = re.sub(r"^```(?:json)?\n", "", clean)
        clean = re.sub(r"\n```$", "", clean)
    try:
        return json.loads(clean)
    except json.JSONDecodeError:
        return {"unstructured_text": clean}
