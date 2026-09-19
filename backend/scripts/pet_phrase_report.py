"""Pet-phrase audit: find phrases the agent leans on ACROSS conversations.

A pet phrase isn't high raw count — it's high *document frequency*: a phrase
that shows up in many different conversations, even just once each. This
scans the flow journal's agent turns, extracts 2–4-gram phrases, and ranks
them by the share of conversations they appear in, split by prompt version
(older entries without a version field are grouped as "pre-v4").

Usage (from backend/):
    .venv/Scripts/python scripts/pet_phrase_report.py [--min-share 0.25]
"""

from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.config import settings  # noqa: E402

# Conversational scaffolding that recurs naturally and isn't a tell.
BORING = {
    "what are you", "are you hoping", "you hoping to", "hoping to do",
    "to do with", "do with the", "do with this", "with the space",
    "with this space", "and what s", "what s your", "s your first",
    "your first name", "first name", "the space", "this room", "the room",
    "if you want", "would you like", "do you want", "let me know",
    "square feet", "sq ft", "in the", "of the", "on the", "for the",
    "to the", "and the", "with a", "you can", "i can", "it s", "that s",
    "you re", "i d", "we can", "want me to", "me to put", "when you re",
    "you re ready", "once you re",
}

WORD = re.compile(r"[a-z']+")


def normalize(text: str) -> list[str]:
    return WORD.findall(text.lower().replace("’", "'"))


def ngrams(words: list[str], n: int):
    for i in range(len(words) - n + 1):
        yield " ".join(words[i : i + n])


def is_boring(gram: str) -> bool:
    if gram in BORING:
        return True
    tokens = gram.split()
    # All-stopword grams are scaffolding, not style.
    stop = {"the", "a", "an", "and", "or", "to", "of", "in", "on", "for",
            "with", "your", "you", "it", "this", "that", "is", "are", "i",
            "s", "d", "re", "ll", "t", "what", "so", "at", "as", "be",
            "can", "do", "me", "my", "we"}
    return all(t in stop for t in tokens)


def main() -> None:
    min_share = 0.25
    if "--min-share" in sys.argv:
        min_share = float(sys.argv[sys.argv.index("--min-share") + 1])
    path = Path(settings.storage_dir) / "flow_journal" / "journal.jsonl"
    if not path.exists():
        print(f"no journal at {path}")
        sys.exit(1)

    # conversations[version][thread_id] = concatenated agent text
    conversations: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        text = record.get("agentText") or ""
        if not text or record.get("usedFallback"):
            continue
        version = record.get("promptVersion") or "pre-v4"
        conversations[version][record.get("threadId", "?")].append(text)

    for version in sorted(conversations):
        threads = conversations[version]
        n_threads = len(threads)
        if n_threads < 3:
            print(f"\n=== {version}: only {n_threads} conversations, skipping ===")
            continue
        doc_freq: dict[str, int] = defaultdict(int)
        for thread_texts in threads.values():
            words_all = normalize(" ".join(thread_texts))
            seen: set[str] = set()
            for n in (2, 3, 4):
                for gram in ngrams(words_all, n):
                    if gram not in seen and not is_boring(gram):
                        seen.add(gram)
                        doc_freq[gram] += 1
        ranked = sorted(
            ((g, c) for g, c in doc_freq.items() if c / n_threads >= min_share),
            key=lambda x: (-x[1], -len(x[0])),
        )
        # Collapse sub-grams contained in an equally-frequent longer gram.
        kept: list[tuple[str, int]] = []
        for gram, count in ranked:
            if any(gram in longer and count <= c2 for longer, c2 in kept):
                continue
            kept.append((gram, count))
        print(f"\n=== {version}: {n_threads} conversations ===")
        print(f"{'phrase':<44}{'convs':>6}{'share':>8}")
        print("-" * 58)
        for gram, count in kept[:20]:
            print(f"{gram:<44}{count:>4}/{n_threads:<3}{count / n_threads:>6.0%}")
        if not kept:
            print("(no phrase clears the threshold)")


main()
