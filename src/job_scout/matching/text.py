"""Small, explicit phrase vocabulary; no semantic or remote inference."""

import re

ALIASES = {
    "c++": ["c++", "cpp"],
    "machine learning": ["machine learning", "ml"],
    "algorithms": ["algorithms", "algorithm"],
    "data structures": ["data structures", "data structure"],
    "quantitative developer": ["quantitative developer", "quant developer"],
    "c++ software engineer": ["c++ software engineer", "c++ engineer", "c++ developer"],
    "low-latency engineer": ["low latency engineer", "low latency developer"],
    "linux/infrastructure engineer": ["linux engineer", "infrastructure engineer"],
    "embedded/driver software engineer": [
        "embedded engineer",
        "embedded software engineer",
        "driver engineer",
        "driver developer",
    ],
    "machine learning engineer": ["machine learning engineer", "ml engineer"],
    "software engineer": ["software engineer", "software developer"],
}


def normalized(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[-_–—]", " ", value.casefold())).strip()


def contains(text: str, phrase: str) -> bool:
    return (
        re.search(r"(?<![\w+])" + re.escape(normalized(phrase)) + r"(?![\w+])", normalized(text))
        is not None
    )


def mentions(text: str, term: str) -> bool:
    return any(contains(text, alias) for alias in ALIASES.get(term.casefold(), [term]))


def role_type(title: str, employment_type: str | None = None) -> str | None:
    text = f"{title} {employment_type or ''}"
    for label, phrases in [
        ("internship", ["intern", "internship"]),
        ("graduate", ["graduate", "new grad", "new graduate"]),
        ("entry_level", ["entry level", "junior"]),
    ]:
        if any(contains(text, phrase) for phrase in phrases):
            return label
    return None
