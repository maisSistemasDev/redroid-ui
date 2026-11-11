"""Cliente mínimo para HeroSMS (protocolo compatível com SMS-Activate)."""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


class HeroSMSError(RuntimeError):
    pass


def _default_base_url() -> str:
    return (
        os.environ.get("HERO_SMS_API_URL", "").strip()
        or "https://hero-sms.com/stubs/handler_api.php"
    )


def _request(base_url: str, params: dict[str, Any]) -> str:
    q = {k: str(v) for k, v in params.items() if v is not None}
    url = f"{base_url}?{urllib.parse.urlencode(q)}"
    # O Cloudflare da HeroSMS bloqueia o User-Agent padrão "Python-urllib" (erro 1010).
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) redroid-ui"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.read().decode("utf-8", errors="replace").strip()
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise HeroSMSError(body or f"HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise HeroSMSError(str(exc.reason)) from exc


def public_catalog() -> dict[str, list[dict[str, Any]]]:
    """Serviços e países da HeroSMS; não exigem api_key."""
    base = _default_base_url()
    try:
        services_raw = json.loads(_request(base, {"action": "getServicesList", "lang": "en"}))
        countries_raw = json.loads(_request(base, {"action": "getCountries"}))
    except ValueError as exc:
        raise HeroSMSError(f"Resposta inválida da HeroSMS: {exc}") from exc
    services = [
        {"code": str(s["code"]), "name": str(s.get("name") or s["code"])}
        for s in services_raw.get("services") or []
        if s.get("code")
    ]
    countries = [
        {"id": int(c["id"]), "name": str(c.get("eng") or c["id"])}
        for c in (countries_raw.values() if isinstance(countries_raw, dict) else countries_raw)
        if isinstance(c, dict) and "id" in c and c.get("visible", 1)
    ]
    countries.sort(key=lambda c: c["name"])
    return {"services": services, "countries": countries}


class HeroSMSClient:
    def __init__(self, api_key: str | None = None, base_url: str | None = None) -> None:
        self.api_key = (api_key or os.environ.get("HERO_SMS_API_KEY", "")).strip()
        self.base_url = (base_url or _default_base_url()).strip()
        if not self.api_key:
            raise HeroSMSError("HERO_SMS_API_KEY não configurada.")

    def _call(self, **params: str | int) -> str:
        return _request(self.base_url, {"api_key": self.api_key, **params})

    def _v1(self, method: str, path: str, query: dict[str, Any] | None = None, body: dict[str, Any] | None = None) -> Any:
        """API REST v1: único caminho que aceita verificationType=call (flash call)."""
        base = os.environ.get("HERO_SMS_API_V1_URL", "").strip() or "https://hero-sms.com/api/v1"
        url = base + path
        if query:
            url += "?" + urllib.parse.urlencode({k: str(v) for k, v in query.items() if v is not None})
        req = urllib.request.Request(
            url,
            method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) redroid-ui",
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Authorization": f"ApiKey {self.api_key}",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            try:
                err = json.loads(raw)
                raw = err.get("details") or err.get("message") or err.get("title") or raw
            except (ValueError, AttributeError):
                pass
            raise HeroSMSError(f"HTTP {exc.code}: {raw}") from exc
        except urllib.error.URLError as exc:
            raise HeroSMSError(str(exc.reason)) from exc
        try:
            return json.loads(raw)
        except ValueError:
            raise HeroSMSError(f"Resposta inválida da HeroSMS: {raw[:200]}") from None

    def get_offers(self, service: str, verification_type: str = "call") -> dict[str, dict[str, Any]]:
        """{country_id: {"cost": float, "count": int}} das ofertas por tipo de verificação."""
        data = self._v1("GET", f"/activations/offers/{verification_type}", {"services": service.strip()})
        by_country = ((data or {}).get("data") or {}).get(service.strip()) or {}
        out: dict[str, dict[str, Any]] = {}
        for country_id, info in by_country.items():
            if not isinstance(info, dict):
                continue
            prices = info.get("prices") or {}
            try:
                out[str(country_id)] = {
                    "cost": float(prices.get("default") or prices.get("min") or 0),
                    "count": int((info.get("counts") or {}).get("total") or 0),
                }
            except (TypeError, ValueError):
                continue
        return out

    def buy_activation(
        self, service: str, country: int, max_price: float | None = None, verification_type: str = "sms"
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "service": service.strip(),
            "country": int(country),
            "amount": 1,
            "verificationType": verification_type,
        }
        if max_price is not None:
            body["maxPrice"] = max_price
        data = self._v1("POST", "/activations", body=body)
        items = (data or {}).get("data") or []
        if not items:
            raise HeroSMSError(f"Compra sem ativação: {json.dumps(data)[:200]}")
        item = items[0]
        return {"activation_id": str(item["id"]), "phone": str(item["phone"])}

    def get_status_v2(self, activation_id: str) -> dict[str, Any]:
        """Como get_status, mas devolve também phoneFrom (em flash call, o número que ligou)."""
        raw = self._call(action="getStatusV2", id=activation_id)
        if raw == "STATUS_CANCEL":
            return {"status": "cancelled", "raw": raw}
        try:
            data = json.loads(raw)
        except ValueError:
            self._fail(raw)
            return {"status": "waiting", "raw": raw}
        otp = data.get("data") if isinstance(data, dict) else None
        if isinstance(otp, dict) and (otp.get("code") or otp.get("phoneFrom")):
            return {
                "status": "ok",
                "code": str(otp.get("code") or ""),
                "phone_from": str(otp.get("phoneFrom") or ""),
                "type": otp.get("type") or (data.get("verificationType") if isinstance(data, dict) else ""),
                "raw": raw[:500],
            }
        return {"status": "waiting", "raw": raw[:500]}

    def get_prices(self, service: str) -> dict[str, dict[str, Any]]:
        """{country_id: {"cost": float, "count": int}} para o serviço."""
        raw = self._call(action="getPrices", service=service.strip())
        try:
            data = json.loads(raw)
        except ValueError:
            self._fail(raw)
            raise HeroSMSError(raw) from None
        if not isinstance(data, dict) or "title" in data:
            raise HeroSMSError(raw)
        out: dict[str, dict[str, Any]] = {}
        for country_id, services in data.items():
            info = (services or {}).get(service.strip()) if isinstance(services, dict) else None
            if not isinstance(info, dict):
                continue
            try:
                out[str(country_id)] = {
                    "cost": float(info.get("cost") or 0),
                    "count": int(info.get("count") or 0),
                }
            except (TypeError, ValueError):
                continue
        return out

    @staticmethod
    def _fail(raw: str) -> None:
        if raw.startswith("ACCESS_") or raw.startswith("STATUS_"):
            return
        if raw in ("NO_NUMBERS", "NO_BALANCE", "BAD_KEY", "BAD_ACTION", "BAD_SERVICE"):
            raise HeroSMSError(raw)
        if raw.startswith("ERROR"):
            raise HeroSMSError(raw)
        if ":" not in raw and raw.isupper():
            raise HeroSMSError(raw)

    def get_balance(self) -> float:
        raw = self._call(action="getBalance")
        self._fail(raw)
        if raw.startswith("ACCESS_BALANCE:"):
            return float(raw.split(":", 1)[1])
        raise HeroSMSError(raw)

    def get_number(self, service: str, country: int, max_price: float | None = None) -> dict[str, Any]:
        params: dict[str, str | int] = {
            "action": "getNumber",
            "service": service.strip(),
            "country": int(country),
        }
        if max_price is not None:
            params["maxPrice"] = max_price
        raw = self._call(**params)
        self._fail(raw)
        if raw.startswith("ACCESS_NUMBER:"):
            _, activation_id, phone = raw.split(":", 2)
            return {"activation_id": activation_id, "phone": phone}
        raise HeroSMSError(raw)

    def set_status(self, activation_id: str, status: int) -> str:
        raw = self._call(action="setStatus", id=activation_id, status=status)
        self._fail(raw)
        return raw

    def mark_ready(self, activation_id: str) -> str:
        return self.set_status(activation_id, 1)

    def complete(self, activation_id: str) -> str:
        return self.set_status(activation_id, 6)

    def cancel(self, activation_id: str) -> str:
        return self.set_status(activation_id, 8)

    def get_status(self, activation_id: str) -> dict[str, Any]:
        raw = self._call(action="getStatus", id=activation_id)
        if raw.startswith("STATUS_OK:"):
            code = raw.split(":", 1)[1]
            return {"status": "ok", "code": code, "raw": raw}
        if raw == "STATUS_WAIT_CODE":
            return {"status": "waiting", "raw": raw}
        if raw == "STATUS_WAIT_RETRY":
            return {"status": "wait_retry", "raw": raw}
        if raw == "STATUS_WAIT_RESEND":
            return {"status": "wait_resend", "raw": raw}
        if raw == "STATUS_CANCEL":
            return {"status": "cancelled", "raw": raw}
        self._fail(raw)
        return {"status": "unknown", "raw": raw}
