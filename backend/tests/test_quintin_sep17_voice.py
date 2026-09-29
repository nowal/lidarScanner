"""The agent stays the subject; the people behind it are not the story.

Quintin, Sep 17 (#101). The first fix removed the scripted "a person on my
team reviews every request" lines, but the system prompt still *instructed*
the model to say "my team" whenever work went to a human — so the model went
on volunteering it ("so my team can get pricing back to you", observed in
the simulator on Sep 18). This pins the prompt itself.
"""

from __future__ import annotations

from app.flow_quotes import EXPECTATIONS
from app.home_guide_prompt import HOME_GUIDE_PROMPT_VERSION, build_home_guide_system_prompt


def test_the_prompt_never_tells_the_model_to_say_my_team():
    prompt = build_home_guide_system_prompt("control")
    # The phrase may appear only as an example of what NOT to say.
    for line in prompt.splitlines():
        if "my team" in line:
            assert "not" in line.lower(), f"prompt still endorses 'my team': {line.strip()}"


def test_the_prompt_keeps_the_agent_as_the_subject():
    prompt = build_home_guide_system_prompt("control")
    assert "Keep YOURSELF the subject" in prompt
    assert "do not narrate them" in prompt


def test_the_honesty_rules_survive_the_rewrite():
    """Dropping the team-talk must not drop what keeps the agent honest."""
    prompt = build_home_guide_system_prompt("control")
    assert "Never say or imply you are a human" in prompt
    assert "that you are a person" in prompt
    assert "a price you produced" in prompt


def test_the_i_cant_rule_no_longer_routes_to_a_person():
    prompt = build_home_guide_system_prompt("control")
    assert 'Never tell the homeowner "I can\'t"' in prompt
    assert "you will\n  find out and come back to them" in prompt


def test_the_prompt_version_moved_with_the_wording():
    assert HOME_GUIDE_PROMPT_VERSION == "home-guide-v11"


def test_the_submission_copy_still_matches_the_prompt_direction():
    assert "my team" not in EXPECTATIONS["copy"]
    assert EXPECTATIONS["copy"].startswith("Sounds good — I'm getting your request")
