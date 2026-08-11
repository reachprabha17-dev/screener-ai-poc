"""HTTP client for the control plane (spec 2, decision #11).

**This is the only way the UI reaches anything.** No database driver, no
`screener.storage`, no `screener.pipeline` — the boundary between the client and
the data is a process boundary, not a convention.

> A caveat worth stating, because 3.1 overstates it: `sqlite3` is in the Python
> standard library, so "the `ui` extra installs no database driver" cannot make
> the database *unreachable*. It removes the temptation and the connection
> string, but the boundary is ultimately enforced by `tests/test_layering.py`,
> which fails if anything under `ui/` imports `sqlite3` or `screener.storage`.
> Discipline plus a test, not the dependency graph alone.

**Every call can fail, and none of them may raise into a Streamlit render.** The
API is a separate process that gets restarted, and a traceback in the middle of a
page is both useless to a recruiter and indistinguishable from a bug in the
screening itself. Failures come back as `ApiError` with a sentence a person can
act on.
"""

from dataclasses import dataclass
from typing import Any

import httpx

DEFAULT_TIMEOUT = 30.0
# Rubric extraction is a live LLM call — ~5 s typical, but a cold model load can
# take considerably longer, and timing out mid-generation looks like a failure
# when it was only slow.
EXTRACT_TIMEOUT = 180.0


class ApiError(RuntimeError):
    """Something the user needs told, phrased for a person rather than a log."""


@dataclass(frozen=True)
class ApiClient:
    base_url: str
    actor: str

    # --- transport -----------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> Any:  # noqa: ANN401 — the API returns several unrelated shapes
        try:
            response = httpx.request(
                method,
                f"{self.base_url.rstrip('/')}{path}",
                json=json,
                headers={"X-Actor": self.actor},
                timeout=timeout,
            )
        except httpx.ConnectError as exc:
            raise ApiError(
                f"Cannot reach the screener API at {self.base_url}. Is the API service running?"
            ) from exc
        except httpx.TimeoutException as exc:
            raise ApiError(
                f"The API did not respond within {timeout:.0f}s. It may be busy loading the model."
            ) from exc
        except httpx.RequestError as exc:
            raise ApiError(
                f"Cannot reach the screener API at {self.base_url}. Is the API service running?"
            ) from exc



        if response.status_code >= 400:
            raise ApiError(_explain(response))

        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    # --- positions -----------------------------------------------------------

    def list_positions(self) -> list[dict[str, Any]]:
        return list(self._request("GET", "/positions"))

    def create_position(self, reference: str, title: str, jd_text: str) -> dict[str, Any]:
        return dict(
            self._request(
                "POST",
                "/positions",
                json={"reference": reference, "title": title, "jd_text": jd_text},
            )
        )

    # --- rubrics -------------------------------------------------------------

    def extract_rubric(self, position_id: str) -> dict[str, Any]:
        return dict(
            self._request(
                "POST", f"/positions/{position_id}/rubric/extract", timeout=EXTRACT_TIMEOUT
            )
        )

    def save_rubric(self, position_id: str, criteria: list[dict[str, Any]]) -> dict[str, Any]:
        return dict(
            self._request("PUT", f"/positions/{position_id}/rubric", json={"criteria": criteria})
        )

    def approve_rubric(self, rubric_id: str) -> dict[str, Any]:
        return dict(self._request("POST", f"/rubrics/{rubric_id}/approve"))

    def get_latest_rubric(self, position_id: str) -> dict[str, Any] | None:
        res = self._request("GET", f"/positions/{position_id}/rubric")
        return dict(res) if res else None

    def get_approved_rubric(self, position_id: str) -> dict[str, Any] | None:
        res = self._request("GET", f"/positions/{position_id}/rubric/approved")
        return dict(res) if res else None

    # --- runs ----------------------------------------------------------------

    def list_runs(self) -> list[dict[str, Any]]:

        return list(self._request("GET", "/runs"))

    def create_run(self, position_id: str, rubric_id: str) -> dict[str, Any]:

        return dict(
            self._request(
                "POST", "/runs", json={"position_id": position_id, "rubric_id": rubric_id}
            )
        )

    def start_run(self, run_id: str) -> int:
        return int(self._request("POST", f"/runs/{run_id}/start")["count"])

    def rescan_run(self, run_id: str) -> int:
        return int(self._request("POST", f"/runs/{run_id}/rescan")["count"])

    def abort_run(self, run_id: str) -> None:
        self._request("POST", f"/runs/{run_id}/abort")

    def run_status(self, run_id: str) -> dict[str, Any]:
        return dict(self._request("GET", f"/runs/{run_id}/status"))

    def list_candidates(self, run_id: str) -> dict[str, Any]:
        return dict(self._request("GET", f"/runs/{run_id}/candidates"))

    def sign_off(self, run_id: str) -> None:
        self._request("POST", f"/runs/{run_id}/sign-off")

    # --- candidates ----------------------------------------------------------

    def decide(self, candidate_id: int, decision: str, reason: str) -> None:
        self._request(
            "POST",
            f"/candidates/{candidate_id}/decision",
            json={"decision": decision, "reason": reason},
        )

    def decide_bulk(
        self, candidate_ids: list[int], decision: str, reason: str
    ) -> dict[str, list[int]]:
        return dict(
            self._request(
                "POST",
                "/candidates/decisions",
                json={"candidate_ids": candidate_ids, "decision": decision, "reason": reason},
            )
        )

    def file_url(self, candidate_id: int) -> str:
        """A URL for the browser to follow, not a body for this process to hold.

        The original document is served as a stream; pulling it through here to
        hand to Streamlit would put every 5 MB PDF a reviewer opens into the UI
        process's memory for no purpose.
        """
        return f"{self.base_url.rstrip('/')}/candidates/{candidate_id}/file"

    def purge(self, file_sha256: str) -> int:
        return int(self._request("DELETE", f"/candidates/{file_sha256}")["count"])

    # --- ops -----------------------------------------------------------------

    def health(self) -> dict[str, Any]:
        return dict(self._request("GET", "/health"))


def _explain(response: httpx.Response) -> str:
    """Turn an HTTP error into something a recruiter can act on.

    A 409 in particular has one cause in this system and a clear next step, and
    saying so beats echoing a status code at someone who is trying to fill a
    vacancy.
    """
    try:
        detail = response.json().get("detail")
    except ValueError:
        detail = None

    if response.status_code == 404:
        return "Not found — it may have been deleted, or the id is wrong."
    if response.status_code == 409:
        return "That rubric has not been approved yet. Approve it before starting a run."
    if response.status_code == 422:
        return f"The request was rejected as invalid: {detail or response.text[:200]}"
    if response.status_code == 503:
        return "The screener is not ready — check the model, disk space and migrations."
    if isinstance(detail, str):
        return detail
    return f"The API returned {response.status_code}."
