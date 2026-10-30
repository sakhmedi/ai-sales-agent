"""HubSpot CRM client: companies, contacts, deals and notes.

Uses the legacy /crm/v3 endpoints, which HubSpot supports until September 2027.
The migration target is the date-based API (e.g. /crm/objects/2026-09/...), see
https://developers.hubspot.com/docs/api-reference/legacy/migration-guide
"""

import html
import logging
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from types import TracebackType
from typing import Any, Self
from urllib.parse import urlparse

import httpx

from app.config import Settings
from app.models.lead import CompanyInfo, ContactInfo, CrmSaveResult, EmailDraft, Qualification

logger = logging.getLogger(__name__)

OBJECTS_PATH = "/crm/v3/objects"
PROPERTIES_PATH = "/crm/v3/properties"

# HubSpot-defined association type IDs (direction matters: from -> to).
CONTACT_TO_COMPANY = 279
DEAL_TO_COMPANY = 341
DEAL_TO_CONTACT = 3
NOTE_TO_DEAL = 214

SCORE_PROPERTY = "ai_lead_score"
FIT_PROPERTY = "ai_lead_fit"
DEAL_PROPERTY_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": SCORE_PROPERTY,
        "label": "AI lead score",
        "description": "0-100 ICP fit score from the AI SDR agent.",
        "groupName": "dealinformation",
        "type": "number",
        "fieldType": "number",
    },
    {
        "name": FIT_PROPERTY,
        "label": "AI lead fit",
        "description": "hot / warm / cold, from the AI SDR agent.",
        "groupName": "dealinformation",
        "type": "enumeration",
        "fieldType": "select",
        "options": [
            {"label": "Hot", "value": "hot", "displayOrder": 0},
            {"label": "Warm", "value": "warm", "displayOrder": 1},
            {"label": "Cold", "value": "cold", "displayOrder": 2},
        ],
    },
]

RETRYABLE_STATUSES = {429, 500, 502, 503, 504}
BACKOFF_SECONDS = 1.0
MAX_BACKOFF_SECONDS = 30.0

# (object id, association type id)
Association = tuple[str, int]


class HubSpotError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"HubSpot API error {status}: {message}")
        self.status = status


class HubSpotAuthError(HubSpotError):
    """401: the token is missing, invalid or revoked."""


class HubSpotScopeError(HubSpotError):
    """403: the private app lacks a required scope."""


# --- Payload builders (shared by the real sync and the dry-run plan) ---


def company_domain(website: str) -> str:
    """'https://www.Example.dk/about' -> 'example.dk'."""
    url = website if "://" in website else f"https://{website}"
    host = (urlparse(url).hostname or "").lower()
    return host.removeprefix("www.")


def company_properties(lead: CompanyInfo) -> dict[str, str]:
    props = {"name": lead.name, "domain": company_domain(lead.website)}
    if lead.country:
        props["country"] = lead.country
    if lead.employee_count is not None:
        props["numberofemployees"] = str(lead.employee_count)
    if lead.description:
        props["description"] = lead.description
    return props


def contact_properties(contact: ContactInfo) -> dict[str, str]:
    props: dict[str, str] = {}
    if contact.email:
        props["email"] = contact.email
    if contact.name:
        first, _, last = contact.name.partition(" ")
        props["firstname"] = first
        if last:
            props["lastname"] = last
    if contact.job_title:
        props["jobtitle"] = contact.job_title
    return props


def deal_name(lead: CompanyInfo) -> str:
    return f"{lead.name} - AI SDR"


def deal_properties(lead: CompanyInfo, qualification: Qualification) -> dict[str, str]:
    return {
        "dealname": deal_name(lead),
        SCORE_PROPERTY: str(qualification.score),
        FIT_PROPERTY: qualification.fit,
    }


def note_properties(
    email: EmailDraft, qualification: Qualification, now: datetime
) -> dict[str, str]:
    """HubSpot renders notes as HTML, so escape the text and keep line breaks."""
    reasons = "<br>".join(html.escape(r) for r in qualification.reasons)
    body = html.escape(email.body).replace("\n", "<br>")
    return {
        "hs_timestamp": now.isoformat(),
        "hs_note_body": (
            f"<b>AI SDR email draft</b><br><b>Subject:</b> {html.escape(email.subject)}<br><br>"
            f"{body}<br><br><b>Qualification ({qualification.score}/100, {qualification.fit}):"
            f"</b><br>{reasons}"
        ),
    }


def build_plan(
    lead: CompanyInfo,
    qualification: Qualification,
    email: EmailDraft | None,
    contact: ContactInfo | None,
    pipeline: str,
    deal_stage: str,
    now: datetime,
) -> dict[str, Any]:
    """Describe what sync_lead would write. Used for DRY_RUN logging."""
    deal = deal_properties(lead, qualification) | {"pipeline": pipeline, "dealstage": deal_stage}
    return {
        "company": {"find_by_domain": company_domain(lead.website), **company_properties(lead)},
        "contact": contact_properties(contact) if contact and contact.email else None,
        "deal": deal,
        "note": note_properties(email, qualification, now) if email else None,
    }


# --- Client ---


class HubSpotClient:
    """Thin synchronous wrapper around the HubSpot CRM API with retries."""

    def __init__(
        self,
        token: str,
        *,
        base_url: str = "https://api.hubapi.com",
        timeout: float = 10.0,
        max_retries: int = 3,
        pipeline: str = "default",
        deal_stage: str = "appointmentscheduled",
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._http = httpx.Client(
            base_url=base_url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
        )
        self._max_retries = max_retries
        self._pipeline = pipeline
        self._deal_stage = deal_stage
        self._sleep = sleep
        self._properties_ready = False

    @classmethod
    def from_settings(cls, settings: Settings) -> Self:
        return cls(
            settings.hubspot_token.get_secret_value(),
            base_url=settings.hubspot_base_url,
            timeout=settings.http_timeout_seconds,
            max_retries=settings.hubspot_max_retries,
            pipeline=settings.hubspot_pipeline,
            deal_stage=settings.hubspot_deal_stage,
        )

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._http.close()

    # --- High level ---

    def sync_lead(
        self,
        lead: CompanyInfo,
        qualification: Qualification,
        email: EmailDraft | None,
        contact: ContactInfo | None,
    ) -> CrmSaveResult:
        """Upsert company, contact and deal, then attach the email as a note."""
        self.ensure_deal_properties()

        domain = company_domain(lead.website)
        company_id = self.search("companies", "domain", domain)
        created_company = company_id is None
        if company_id is None:
            company_id = self.create("companies", company_properties(lead))
        logger.info("HubSpot company %s (%s, created=%s)", company_id, domain, created_company)

        contact_id = None
        if contact and contact.email:
            contact_id = self._upsert_contact(contact, contact.email, company_id)
        else:
            logger.info("No contact email found; skipping contact")

        deal_id, created_deal = self._upsert_deal(lead, qualification, company_id, contact_id)

        note_id = None
        if email:
            note_id = self.create(
                "notes",
                note_properties(email, qualification, datetime.now(UTC)),
                [(deal_id, NOTE_TO_DEAL)],
            )

        return CrmSaveResult(
            status="saved",
            message=f"Saved '{lead.name}' to HubSpot (deal {deal_id}).",
            company_id=company_id,
            contact_id=contact_id,
            deal_id=deal_id,
            note_id=note_id,
            created_company=created_company,
            created_deal=created_deal,
        )

    def _upsert_contact(self, contact: ContactInfo, email: str, company_id: str) -> str:
        contact_id = self.search("contacts", "email", email)
        if contact_id is None:
            try:
                contact_id = self.create(
                    "contacts",
                    contact_properties(contact),
                    [(company_id, CONTACT_TO_COMPANY)],
                )
                logger.info("HubSpot contact %s created", contact_id)
                return contact_id
            except HubSpotError as e:
                # 409: created by someone else since our search. Reuse it.
                if e.status != 409:
                    raise
                contact_id = self.search("contacts", "email", email)
                if contact_id is None:
                    raise
        logger.info("HubSpot contact %s already exists; linking to company", contact_id)
        self.associate("contacts", contact_id, "companies", company_id, CONTACT_TO_COMPANY)
        return contact_id

    def _upsert_deal(
        self,
        lead: CompanyInfo,
        qualification: Qualification,
        company_id: str,
        contact_id: str | None,
    ) -> tuple[str, bool]:
        props = deal_properties(lead, qualification)
        deal_id = self.search("deals", "dealname", props["dealname"])
        if deal_id is not None:
            self.update("deals", deal_id, props)
            if contact_id:
                self.associate("deals", deal_id, "contacts", contact_id, DEAL_TO_CONTACT)
            logger.info("HubSpot deal %s updated (score=%s)", deal_id, qualification.score)
            return deal_id, False

        associations: list[Association] = [(company_id, DEAL_TO_COMPANY)]
        if contact_id:
            associations.append((contact_id, DEAL_TO_CONTACT))
        props |= {"pipeline": self._pipeline, "dealstage": self._deal_stage}
        deal_id = self.create("deals", props, associations)
        logger.info("HubSpot deal %s created (score=%s)", deal_id, qualification.score)
        return deal_id, True

    def ensure_deal_properties(self) -> None:
        """Create the custom deal properties for score and fit if they don't exist yet."""
        if self._properties_ready:
            return
        for definition in DEAL_PROPERTY_DEFINITIONS:
            path = f"{PROPERTIES_PATH}/deals/{definition['name']}"
            try:
                self._request("GET", path)
            except HubSpotError as e:
                if e.status != 404:
                    raise
                logger.info("Creating HubSpot deal property %s", definition["name"])
                try:
                    self._request("POST", f"{PROPERTIES_PATH}/deals", json=definition)
                except HubSpotError as create_error:
                    if create_error.status != 409:  # created concurrently
                        raise
        self._properties_ready = True

    # --- Low level ---

    def search(self, object_type: str, prop: str, value: str) -> str | None:
        """Return the id of the first record where `prop` equals `value`, or None."""
        body = {
            "filterGroups": [
                {"filters": [{"propertyName": prop, "operator": "EQ", "value": value}]}
            ],
            "properties": [prop],
            "limit": 1,
        }
        data = self._request("POST", f"{OBJECTS_PATH}/{object_type}/search", json=body).json()
        results = data.get("results", [])
        return str(results[0]["id"]) if results else None

    def create(
        self,
        object_type: str,
        properties: dict[str, str],
        associations: Sequence[Association] = (),
    ) -> str:
        body: dict[str, Any] = {"properties": properties}
        if associations:
            body["associations"] = [
                {
                    "to": {"id": to_id},
                    "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": t}],
                }
                for to_id, t in associations
            ]
        data = self._request("POST", f"{OBJECTS_PATH}/{object_type}", json=body).json()
        return str(data["id"])

    def update(self, object_type: str, object_id: str, properties: dict[str, str]) -> None:
        self._request(
            "PATCH", f"{OBJECTS_PATH}/{object_type}/{object_id}", json={"properties": properties}
        )

    def associate(
        self, from_type: str, from_id: str, to_type: str, to_id: str, type_id: int
    ) -> None:
        self._request(
            "PUT", f"{OBJECTS_PATH}/{from_type}/{from_id}/associations/{to_type}/{to_id}/{type_id}"
        )

    def _request(self, method: str, path: str, json: Any = None) -> httpx.Response:
        """Send a request, retrying 429 and 5xx with exponential backoff."""
        attempt = 0
        while True:
            try:
                response = self._http.request(method, path, json=json)
            except httpx.TransportError as e:
                raise HubSpotError(0, f"network error: {e}") from e

            if response.status_code not in RETRYABLE_STATUSES or attempt >= self._max_retries:
                break
            delay = _retry_delay(response, attempt)
            attempt += 1
            logger.warning(
                "HubSpot %s %s -> %d, retry %d/%d in %.1fs",
                method,
                path,
                response.status_code,
                attempt,
                self._max_retries,
                delay,
            )
            self._sleep(delay)

        _raise_for_status(response)
        return response


def _retry_delay(response: httpx.Response, attempt: int) -> float:
    """Use Retry-After if HubSpot sent it, else 1s, 2s, 4s, ... (capped)."""
    retry_after = response.headers.get("Retry-After")
    if retry_after:
        try:
            return min(float(retry_after), MAX_BACKOFF_SECONDS)
        except ValueError:
            pass
    return min(BACKOFF_SECONDS * 2.0**attempt, MAX_BACKOFF_SECONDS)


def _raise_for_status(response: httpx.Response) -> None:
    status = response.status_code
    if status < 400:
        return
    try:
        message = str(response.json().get("message", response.text))
    except ValueError:
        message = response.text
    if status == 401:
        raise HubSpotAuthError(status, "invalid or expired token. Check HUBSPOT_TOKEN.")
    if status == 403:
        raise HubSpotScopeError(
            status, f"{message} The private app is probably missing a scope (see README)."
        )
    raise HubSpotError(status, message)
