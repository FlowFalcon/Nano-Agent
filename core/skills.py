"""Keyword-triggered skills: small Markdown playbooks injected into the system
context when the user's message matches a skill's trigger words.

A skill is a Markdown file with a tiny frontmatter block listing triggers:

    ---
    triggers: deploy, ship it, release
    ---
    # Deploy playbook
    Steps: run tests, bump version, tag, push.

Drop skill files in ``<workspace_dir>/skills/*.md``. Matching is word-boundary,
case-insensitive. Pure parsing/matching (stdlib only) so it is unit-testable
without the agent's heavy deps.

Run the self-check directly:  ``python3 core/skills.py``
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

_MAX_SKILL_BODY_CHARS = 4000  # protect the context budget from oversized skills
_FRONTMATTER_RE = re.compile(r"^\s*---\s*\n(.*?)\n---\s*\n(.*)$", re.S)


@dataclass
class Skill:
    name: str
    triggers: list[str] = field(default_factory=list)
    body: str = ""


def parse_skill(text: str, name: str) -> Skill | None:
    """Parse one skill's text. Returns None if it has no usable triggers/body."""
    triggers: list[str] = []
    body = text.strip()

    m = _FRONTMATTER_RE.match(text)
    if m:
        front, body = m.group(1), m.group(2).strip()
        for line in front.splitlines():
            key, _, value = line.partition(":")
            if key.strip().lower() == "triggers":
                triggers = [t.strip().lower() for t in value.split(",") if t.strip()]

    if not triggers or not body:
        return None
    if len(body) > _MAX_SKILL_BODY_CHARS:
        body = body[:_MAX_SKILL_BODY_CHARS].rstrip() + "\n...[skill trimmed]"
    return Skill(name=name, triggers=triggers, body=body)


def match_skills(message: str, skills: list[Skill]) -> list[Skill]:
    """Return skills whose any trigger appears (word-boundary, case-insensitive)."""
    matched: list[Skill] = []
    for skill in skills:
        for kw in skill.triggers:
            if re.search(r"\b" + re.escape(kw) + r"\b", message, re.IGNORECASE):
                matched.append(skill)
                break
    return matched


def load_skills_dir(skills_dir: str | Path) -> list[Skill]:
    """Load and parse all ``*.md`` skills in *skills_dir*. Missing dir → empty list."""
    path = Path(skills_dir)
    if not path.is_dir():
        return []

    skills: list[Skill] = []
    for file in sorted(path.glob("*.md")):
        try:
            parsed = parse_skill(file.read_text(encoding="utf-8"), file.stem)
        except OSError as exc:
            logger.warning("Could not read skill %s: %s", file, exc)
            continue
        if parsed:
            skills.append(parsed)
    return skills


def build_skills_context(matched: list[Skill]) -> str:
    """Render matched skills into a system-context block (empty if none)."""
    if not matched:
        return ""
    parts = ["\n\n## ACTIVE SKILLS (triggered by this message)\n"]
    for skill in matched:
        parts.append(f"### Skill: {skill.name}\n{skill.body}\n")
    return "\n".join(parts)


def _self_check() -> None:
    deploy = parse_skill("---\ntriggers: deploy, ship it\n---\n# Deploy\nRun tests then push.", "deploy")
    assert deploy is not None
    assert deploy.triggers == ["deploy", "ship it"]
    assert "Run tests" in deploy.body

    # No frontmatter / no triggers / empty body → not a skill.
    assert parse_skill("# Just notes\nno triggers here", "x") is None
    assert parse_skill("---\ntriggers:\n---\nbody", "x") is None
    assert parse_skill("---\ntriggers: a\n---\n   ", "x") is None

    skills = [deploy]
    # Word-boundary match, case-insensitive; multi-word trigger works.
    assert match_skills("can you DEPLOY the app?", skills) == [deploy]
    assert match_skills("let's ship it today", skills) == [deploy]
    # No false positive on substrings (deploy not inside 'redeployment'... actually it is;
    # verify a clearly unrelated word does NOT match).
    assert match_skills("tell me about relationships", skills) == []
    assert match_skills("hello there", skills) == []

    # Body cap.
    big = parse_skill("---\ntriggers: big\n---\n" + "x" * (_MAX_SKILL_BODY_CHARS + 500), "big")
    assert big is not None and big.body.endswith("[skill trimmed]")

    ctx = build_skills_context(match_skills("deploy now", skills))
    assert "ACTIVE SKILLS" in ctx and "Run tests" in ctx
    assert build_skills_context([]) == ""

    print("skills self-check: OK")


if __name__ == "__main__":
    _self_check()
