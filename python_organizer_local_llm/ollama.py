#!/usr/bin/env python3

import logging
from typing import Dict, List, Optional

import requests


class OllamaClient:
    """
    Reusable Ollama HTTP client.

    classifier.py can use this later instead of containing its own
    requests.post() logic.
    """

    def __init__(
        self,
        base_url: str,
        model: str,
        timeout: int = 180,
        temperature: float = 0.1,
    ):
        self.log = logging.getLogger("ollama")
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = int(timeout)
        self.temperature = float(temperature)

        self.session = requests.Session()

    def chat(
        self,
        messages: List[Dict[str, str]],
        *,
        json_format: bool = True,
        temperature: Optional[float] = None,
    ) -> str:
        payload = {
            "model": self.model,
            "stream": False,
            "messages": messages,
            "options": {
                "temperature": (
                    self.temperature
                    if temperature is None
                    else float(temperature)
                )
            },
        }

        if json_format:
            payload["format"] = "json"

        response = self.session.post(
            f"{self.base_url}/api/chat",
            json=payload,
            timeout=self.timeout,
        )
        response.raise_for_status()

        data = response.json()
        content = data.get("message", {}).get("content")

        if not content:
            raise RuntimeError("Ollama returned an empty response.")

        return content

    def generate(
        self,
        prompt: str,
        *,
        json_format: bool = False,
        temperature: Optional[float] = None,
    ) -> str:
        payload = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "options": {
                "temperature": (
                    self.temperature
                    if temperature is None
                    else float(temperature)
                )
            },
        }

        if json_format:
            payload["format"] = "json"

        response = self.session.post(
            f"{self.base_url}/api/generate",
            json=payload,
            timeout=self.timeout,
        )
        response.raise_for_status()

        data = response.json()
        content = data.get("response")

        if not content:
            raise RuntimeError("Ollama returned an empty response.")

        return content

    def health_check(self) -> bool:
        try:
            response = self.session.get(
                f"{self.base_url}/api/tags",
                timeout=min(self.timeout, 10),
            )
            response.raise_for_status()
            return True
        except requests.RequestException as exc:
            self.log.warning("Ollama health check failed: %s", exc)
            return False

    def list_models(self) -> List[str]:
        response = self.session.get(
            f"{self.base_url}/api/tags",
            timeout=self.timeout,
        )
        response.raise_for_status()

        data = response.json()
        return [
            model.get("name", "")
            for model in data.get("models", [])
            if model.get("name")
        ]

