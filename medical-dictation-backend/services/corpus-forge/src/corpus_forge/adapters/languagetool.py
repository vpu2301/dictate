"""Optional morphology/spelling stage via a LanguageTool HTTP server
(EXPLORE §6: not in the dev stack yet — the CLI runs this stage only when
MDX_LANGUAGETOOL_URL is set, and skips with a warning otherwise).

The medical allowlist (infra/seeds/corpus/risk/medical_allowlist.txt) is
applied here: a match whose flagged token is allowlisted is suppressed.
Changing that file requires review (plan §6).
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx


@dataclass(frozen=True, slots=True)
class MorphologyIssue:
    phrase: str
    message: str
    token: str


async def check_phrases(
    *,
    base_url: str,
    phrases: list[str],
    language: str,
    allowlist: frozenset[str],
) -> list[MorphologyIssue]:
    lt_language = {"uk": "uk-UA", "en": "en-US"}.get(language, language)
    issues: list[MorphologyIssue] = []
    async with httpx.AsyncClient(base_url=base_url, timeout=30.0) as client:
        for phrase in phrases:
            resp = await client.post(
                "/v2/check", data={"text": phrase, "language": lt_language}
            )
            resp.raise_for_status()
            for match in resp.json().get("matches", []):
                offset = int(match.get("offset", 0))
                length = int(match.get("length", 0))
                token = phrase[offset : offset + length]
                if token.lower() in allowlist:
                    continue
                issues.append(
                    MorphologyIssue(
                        phrase=phrase,
                        message=str(match.get("message", "")),
                        token=token,
                    )
                )
    return issues
